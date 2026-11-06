"""`pr-review eval`: run every case N times on one arm, score, and summarise.

Replies are cached by (case, arm config, stage, run index); the stage key
includes the prompt version, so a prompt change re-runs and nothing else does.
Cached reviews cost nothing this run but keep their original cost and latency,
so the summary describes the reviews, not this invocation's spend.

Uncached reviews run on a thread pool (`concurrency`); the budget reserves each
call's worst case, so running calls together can't overshoot it. Reviews are
written in (run index, manifest order) whatever order they finish in.
"""

import hashlib
import json
import math
import statistics
import threading
import time
from collections.abc import Sequence
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, ConfigDict

from pr_review.arms import Arm
from pr_review.generate import (
    PROMPT_VERSION,
    GenerateResult,
    ReviewInput,
    changed_base_paths,
    generate,
    read_base_files,
)
from pr_review.llm import Budget, Cancelled, make_client
from pr_review.models import Case, Finding
from pr_review.scoring import Score, score_case

STAGE = f"generate-{PROMPT_VERSION}"
DEFAULT_CONCURRENCY = 8


class CachedReview(BaseModel):
    """One stored review. Also the line format of `reviews.jsonl`."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    run_index: int
    findings: list[Finding]
    malformed: bool
    attempts: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_s: float
    http_retries: int = 0  # 429/5xx retries
    retry_wait_s: float = 0.0  # their failed attempts and backoff, not in latency_s

    @classmethod
    def from_result(
        cls, case_id: str, run_index: int, r: GenerateResult
    ) -> "CachedReview":
        return cls(
            case_id=case_id,
            run_index=run_index,
            findings=r.findings,
            malformed=r.malformed,
            attempts=r.attempts,
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            cost_usd=r.cost_usd,
            latency_s=r.latency_s,
            http_retries=r.retries,
            retry_wait_s=r.retry_wait_s,
        )


class Cache:
    """One JSON file per (case, arm config, stage, run index).

    The case part of the key is its id plus a hash of what the model is shown
    (diff and base files), so regenerating a case's diff or moving its base
    re-runs it instead of reusing a reply to the old input.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(
        self, case_id: str, review: ReviewInput, arm: Arm, stage: str, run_index: int
    ) -> Path:
        return (
            self.root
            / arm.config_hash()
            / stage
            / case_id
            / input_hash(review)
            / f"run-{run_index}.json"
        )

    def get(
        self, case_id: str, review: ReviewInput, arm: Arm, stage: str, run_index: int
    ) -> CachedReview | None:
        p = self.path(case_id, review, arm, stage, run_index)
        if not p.exists():
            return None
        return CachedReview.model_validate_json(p.read_text(encoding="utf-8"))

    def put(
        self, review_input: ReviewInput, arm: Arm, stage: str, review: CachedReview
    ) -> None:
        p = self.path(review.case_id, review_input, arm, stage, review.run_index)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(review.model_dump_json(), encoding="utf-8")
        tmp.replace(p)


@dataclass(frozen=True)
class Spread:
    mean: float
    min: float
    max: float


def spread(values: Sequence[float]) -> Spread:
    return Spread(mean=statistics.fmean(values), min=min(values), max=max(values))


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile, q in (0, 100]."""
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def input_hash(review: ReviewInput) -> str:
    blob = json.dumps(
        {"diff": review.diff, "base_files": review.base_files}, sort_keys=True
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def base_dir(case: Case, root: Path) -> Path:
    """A case's offline base files: `base/` next to its diff, as `make_case` writes it."""
    return (root / case.diff_path).parent / "base"


def review_input(case: Case, root: Path) -> ReviewInput:
    """What the model may see for `case`: its diff and changed files at base. Nothing else."""
    diff = (root / case.diff_path).read_text(encoding="utf-8")
    base_files = read_base_files(base_dir(case, root), changed_base_paths(diff))
    return ReviewInput(diff=diff, base_files=base_files)


def run_eval(
    *,
    cases: Sequence[Case],
    arm: Arm,
    runs: int,
    root: Path,
    cache_dir: Path,
    results_dir: Path,
    budget: Budget,
    run_id: str | None = None,
    client: OpenAI | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> Path:
    """Run, score and write `results/<run-id>/`. Returns the summary path.

    A `BudgetExceeded` (or any other error) stops scheduling new reviews; those
    already in flight finish and are cached, so rerunning with a larger budget
    only pays for the rest.
    """
    if runs < 1:
        raise ValueError("runs must be at least 1")
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if not cases:
        raise ValueError("no cases")
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + f"-{arm.name}"
    out_dir = results_dir / run_id
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} already exists")
    cache = Cache(cache_dir)

    inputs = {c.id: review_input(c, root) for c in cases}
    keys = [(run_index, case.id) for run_index in range(runs) for case in cases]
    done: dict[tuple[int, str], CachedReview] = {}
    todo: list[tuple[int, str]] = []
    for run_index, case_id in keys:
        cached = cache.get(case_id, inputs[case_id], arm, STAGE, run_index)
        if cached is None:
            todo.append((run_index, case_id))
        else:
            done[(run_index, case_id)] = cached

    if todo:
        live = client or make_client(arm)

        # Set by the first failing review, before its worker can take another,
        # so no review starts after a failure (with concurrency 1, as before).
        # A failed call also cancels the budget, which stops reviews already
        # waiting to reserve or to retry.
        stop = threading.Event()

        def review(key: tuple[int, str]) -> CachedReview | None:
            if stop.is_set():
                return None
            run_index, case_id = key
            try:
                result = generate(live, arm, inputs[case_id], budget)
                fresh_review = CachedReview.from_result(case_id, run_index, result)
                cache.put(inputs[case_id], arm, STAGE, fresh_review)
            except BaseException:
                stop.set()
                budget.cancel()
                raise
            return fresh_review

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(review, key) for key in todo]
            try:
                wait(futures, return_when=FIRST_EXCEPTION)
            except BaseException:  # Ctrl-C: stop waiting reviews too
                budget.cancel()
                raise
            finally:
                # On a failure or Ctrl-C, drop queued reviews; let running
                # ones finish so what they paid for is cached.
                stop.set()
                pool.shutdown(wait=True, cancel_futures=True)
        # Re-raise the first real failure in schedule order, not finish order;
        # `Cancelled` only reports that another review failed first.
        errors = [
            e for f in futures if not f.cancelled() and (e := f.exception()) is not None
        ]
        if errors:
            raise next((e for e in errors if not isinstance(e, Cancelled)), errors[0])
        for key, future in zip(todo, futures, strict=True):
            result = future.result()
            assert result is not None  # skipped only after a failure
            done[key] = result

    reviews = [done[key] for key in keys]
    fresh = len(todo)

    out_dir.mkdir(parents=True)
    with (out_dir / "reviews.jsonl").open("w", encoding="utf-8") as f:
        for r in reviews:
            f.write(r.model_dump_json() + "\n")

    summary = summarise(
        cases=cases,
        reviews=reviews,
        arm=arm,
        runs=runs,
        run_id=run_id,
        fresh=fresh,
        spent=budget.spent_usd,
        limit=budget.limit_usd,
    )
    path = out_dir / "summary.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path


def summarise(
    *,
    cases: Sequence[Case],
    reviews: Sequence[CachedReview],
    arm: Arm,
    runs: int,
    run_id: str,
    fresh: int,
    spent: float,
    limit: float,
) -> dict[str, Any]:
    bugs = {c.id: c.bugs for c in cases}
    per_run: list[Score] = []
    for run_index in range(runs):
        total = Score()
        for r in reviews:
            if r.run_index == run_index:
                total = total + score_case(r.findings, bugs[r.case_id])
        per_run.append(total)

    costs = [r.cost_usd for r in reviews]
    latencies = [r.latency_s for r in reviews]
    return {
        "run_id": run_id,
        "arm": arm.name,
        "model": arm.model,
        "arm_config_hash": arm.config_hash(),
        "stage": STAGE,
        "cases": len(cases),
        # Compare arms only on identical case sets.
        "case_ids": sorted(c.id for c in cases),
        "runs": runs,
        "reviews": len(reviews),
        "precision": asdict(spread([s.precision for s in per_run])),
        "recall": asdict(spread([s.recall for s in per_run])),
        "f1": asdict(spread([s.f1 for s in per_run])),
        "per_run": [
            {**asdict(s), "precision": s.precision, "recall": s.recall, "f1": s.f1}
            for s in per_run
        ],
        "cost_per_review_usd": statistics.fmean(costs),
        "cost_total_usd": sum(costs),
        "latency_s": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
        },
        "malformed_reviews": sum(r.malformed for r in reviews),
        "http_retries": sum(r.http_retries for r in reviews),
        "retry_wait_s": sum(r.retry_wait_s for r in reviews),
        "fresh_reviews": fresh,
        "spent_this_invocation_usd": spent,
        "budget_usd": limit,
    }
