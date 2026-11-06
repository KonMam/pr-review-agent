"""`pr-review eval`: run every case N times on one arm, score, and summarise.

Replies are cached by (case, arm config, stage, run index); the stage key
includes the prompt version, so a prompt change re-runs and nothing else does.
Cached reviews cost nothing this run but keep their original cost and latency,
so the summary describes the reviews, not this invocation's spend.
"""

import hashlib
import json
import math
import statistics
import time
from collections.abc import Sequence
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
from pr_review.llm import Budget, make_client
from pr_review.models import Case, Finding
from pr_review.scoring import Score, score_case

STAGE = f"generate-{PROMPT_VERSION}"


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


def review_input(case: Case, root: Path, repos_dir: Path) -> ReviewInput:
    """What the model may see for `case`: its diff and changed files at base. Nothing else."""
    diff = (root / case.diff_path).read_text(encoding="utf-8")
    repo_dir = repos_dir / case.provenance.repo
    base_files = read_base_files(repo_dir, case.base_ref, changed_base_paths(diff))
    return ReviewInput(diff=diff, base_files=base_files)


def run_eval(
    *,
    cases: Sequence[Case],
    arm: Arm,
    runs: int,
    root: Path,
    repos_dir: Path,
    cache_dir: Path,
    results_dir: Path,
    budget: Budget,
    run_id: str | None = None,
    client: OpenAI | None = None,
) -> Path:
    """Run, score and write `results/<run-id>/`. Returns the summary path.

    A `BudgetExceeded` stops the run; replies already paid for stay cached, so
    rerunning with a larger budget only pays for the rest.
    """
    if runs < 1:
        raise ValueError("runs must be at least 1")
    if not cases:
        raise ValueError("no cases")
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + f"-{arm.name}"
    out_dir = results_dir / run_id
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} already exists")
    cache = Cache(cache_dir)

    inputs = {c.id: review_input(c, root, repos_dir) for c in cases}
    reviews: list[CachedReview] = []
    fresh = 0
    for run_index in range(runs):
        for case in cases:
            review = cache.get(case.id, inputs[case.id], arm, STAGE, run_index)
            if review is None:
                if client is None:
                    client = make_client(arm)
                result = generate(client, arm, inputs[case.id], budget)
                review = CachedReview.from_result(case.id, run_index, result)
                cache.put(inputs[case.id], arm, STAGE, review)
                fresh += 1
            reviews.append(review)

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
        "fresh_reviews": fresh,
        "spent_this_invocation_usd": spent,
        "budget_usd": limit,
    }
