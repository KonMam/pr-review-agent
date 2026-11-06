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
from pr_review.grade import STAGE as GRADE_STAGE
from pr_review.grade import GradeResult, grade
from pr_review.llm import Budget, Cancelled, make_client
from pr_review.models import Case, Finding
from pr_review.scoring import Score, score_case
from pr_review.stages import (
    DEFAULT_THRESHOLD,
    ORDER,
    Candidate,
    Stage,
    apply,
    is_suppressed,
)

STAGE = f"generate-{PROMPT_VERSION}"
# Grade thresholds the sweep tries; the spec asks for one chosen for precision.
SWEEP_THRESHOLDS = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
DEFAULT_CONCURRENCY = 8


class CachedReview(BaseModel):
    """One stored generate reply (the cache file for the generate stage)."""

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
    truncated: int = 0  # attempts cut off at max_tokens
    reasoning_tokens: int = 0

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
            truncated=r.truncated,
            reasoning_tokens=r.reasoning_tokens,
        )


class CachedGrade(BaseModel):
    """One stored grade reply: a score per candidate, or None if malformed."""

    model_config = ConfigDict(extra="forbid")

    scores: list[float] | None
    attempts: int
    truncated: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_s: float
    http_retries: int
    retry_wait_s: float

    @classmethod
    def from_result(cls, r: GradeResult) -> "CachedGrade":
        return cls(
            scores=r.scores,
            attempts=r.attempts,
            truncated=r.truncated,
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            reasoning_tokens=r.reasoning_tokens,
            cost_usd=r.cost_usd,
            latency_s=r.latency_s,
            http_retries=r.retries,
            retry_wait_s=r.retry_wait_s,
        )


class ScoredFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding: Finding
    score: float | None


class ReviewRecord(BaseModel):
    """One review through the pipeline. Also the line format of `reviews.jsonl`.

    `findings` is what the configured stages kept; `candidates` is everything
    the generator found, with grades when the grade stage ran. Cost, latency
    and retries are totals over the stages that ran.
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str
    run_index: int
    findings: list[Finding]
    candidates: list[ScoredFinding]
    malformed: bool  # the generate reply
    cost_usd: float
    latency_s: float
    http_retries: int
    retry_wait_s: float
    generate: CachedReview
    grade: CachedGrade | None

    def candidate_list(self) -> list[Candidate]:
        return [Candidate(c.finding, c.score) for c in self.candidates]


class Cache:
    """One JSON file per (case, arm config, stage, run index).

    The case part of the key is its id plus a hash of what the model is shown
    (diff and base files), so regenerating a case's diff or moving its base
    re-runs it instead of reusing a reply to the old input. A grade is also
    keyed by the candidates it grades.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(
        self,
        case_id: str,
        review: ReviewInput,
        arm: Arm,
        stage: str,
        run_index: int,
        extra: str = "",
    ) -> Path:
        p = self.root / arm.config_hash() / stage / case_id / input_hash(review)
        if extra:
            p = p / extra
        return p / f"run-{run_index}.json"

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
        _write(
            self.path(review.case_id, review_input, arm, stage, review.run_index),
            review,
        )

    def get_grade(
        self,
        case_id: str,
        review: ReviewInput,
        arm: Arm,
        run_index: int,
        candidates: Sequence[Finding],
    ) -> CachedGrade | None:
        p = self.path(
            case_id, review, arm, GRADE_STAGE, run_index, findings_hash(candidates)
        )
        if not p.exists():
            return None
        return CachedGrade.model_validate_json(p.read_text(encoding="utf-8"))

    def put_grade(
        self,
        case_id: str,
        review: ReviewInput,
        arm: Arm,
        run_index: int,
        candidates: Sequence[Finding],
        grade: CachedGrade,
    ) -> None:
        _write(
            self.path(
                case_id, review, arm, GRADE_STAGE, run_index, findings_hash(candidates)
            ),
            grade,
        )


def _write(p: Path, model: BaseModel) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(model.model_dump_json(), encoding="utf-8")
    tmp.replace(p)


def findings_hash(findings: Sequence[Finding]) -> str:
    blob = json.dumps([f.model_dump() for f in findings], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


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
    stages: Sequence[Stage] = (),
    grade_arm: Arm | None = None,
    grade_client: OpenAI | None = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> Path:
    """Run, score and write `results/<run-id>/`. Returns the summary path.

    `stages` are the ones after generate (grade, dedup, suppress); none is the
    single-pass reviewer. Only generate and grade call a model; the grader
    uses `grade_arm`, the generator's arm by default.

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
    if not 0 <= threshold <= 1:
        raise ValueError("threshold must be in [0, 1]")
    stages = [s for s in ORDER if s in stages]
    grading = Stage.GRADE in stages
    grader = grade_arm or arm
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + f"-{arm.name}"
    out_dir = results_dir / run_id
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} already exists")
    cache = Cache(cache_dir)

    inputs = {c.id: review_input(c, root) for c in cases}
    keys = [(run_index, case.id) for run_index in range(runs) for case in cases]

    def cached(key: tuple[int, str]) -> tuple[CachedReview, CachedGrade | None] | None:
        """Both stages from the cache, or None if any of them must be paid for."""
        run_index, case_id = key
        gen = cache.get(case_id, inputs[case_id], arm, STAGE, run_index)
        if gen is None:
            return None
        if not grading:
            return gen, None
        graded = cache.get_grade(
            case_id, inputs[case_id], grader, run_index, gen.findings
        )
        return None if graded is None else (gen, graded)

    done: dict[tuple[int, str], tuple[CachedReview, CachedGrade | None]] = {}
    todo: list[tuple[int, str]] = []
    for key in keys:
        hit = cached(key)
        if hit is None:
            todo.append(key)
        else:
            done[key] = hit

    if todo:
        clients: dict[str, OpenAI] = {}

        def client_for(a: Arm) -> OpenAI:
            # Built lazily, so a fully cached stage needs no key for its arm.
            if a.name not in clients:
                given = client if a is arm else grade_client
                clients[a.name] = given or make_client(a)
            return clients[a.name]

        # Set by the first failing review, before its worker can take another,
        # so no review starts after a failure (with concurrency 1, as before).
        # A failed call also cancels the budget, which stops reviews already
        # waiting to reserve or to retry.
        stop = threading.Event()
        lock = threading.Lock()

        def review(
            key: tuple[int, str],
        ) -> tuple[CachedReview, CachedGrade | None] | None:
            if stop.is_set():
                return None
            run_index, case_id = key
            review_in = inputs[case_id]
            try:
                gen = cache.get(case_id, review_in, arm, STAGE, run_index)
                if gen is None:
                    with lock:
                        live = client_for(arm)
                    result = generate(live, arm, review_in, budget)
                    gen = CachedReview.from_result(case_id, run_index, result)
                    cache.put(review_in, arm, STAGE, gen)
                if not grading:
                    return gen, None
                graded = cache.get_grade(
                    case_id, review_in, grader, run_index, gen.findings
                )
                if graded is None:
                    with lock:
                        live = client_for(grader)
                    graded = CachedGrade.from_result(
                        grade(live, grader, review_in, gen.findings, budget)
                    )
                    cache.put_grade(
                        case_id, review_in, grader, run_index, gen.findings, graded
                    )
            except BaseException:
                stop.set()
                budget.cancel()
                raise
            return gen, graded

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

    reviews = [record(key, *done[key], stages, threshold) for key in keys]
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
        stages=stages,
        grade_arm=grader if grading else None,
        threshold=threshold,
    )
    path = out_dir / "summary.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return path


def record(
    key: tuple[int, str],
    gen: CachedReview,
    graded: CachedGrade | None,
    stages: Sequence[Stage],
    threshold: float,
) -> ReviewRecord:
    run_index, case_id = key
    scores: Sequence[float | None] = (
        graded.scores
        if graded is not None and graded.scores is not None
        else [None] * len(gen.findings)
    )
    candidates = [Candidate(f, s) for f, s in zip(gen.findings, scores, strict=True)]
    kept = apply(candidates, stages, threshold)
    parts: list[CachedReview | CachedGrade] = [gen] if graded is None else [gen, graded]
    return ReviewRecord(
        case_id=case_id,
        run_index=run_index,
        findings=[c.finding for c in kept],
        candidates=[
            ScoredFinding(finding=c.finding, score=c.score) for c in candidates
        ],
        malformed=gen.malformed,
        cost_usd=sum(p.cost_usd for p in parts),
        latency_s=sum(p.latency_s for p in parts),
        http_retries=sum(p.http_retries for p in parts),
        retry_wait_s=sum(p.retry_wait_s for p in parts),
        generate=gen,
        grade=graded,
    )


def score_runs(
    cases: Sequence[Case],
    findings: Sequence[tuple[int, str, Sequence[Finding]]],
    runs: int,
) -> list[Score]:
    """Micro-averaged score per run index, from (run index, case id, findings)."""
    bugs = {c.id: c.bugs for c in cases}
    per_run = [Score() for _ in range(runs)]
    for run_index, case_id, found in findings:
        per_run[run_index] = per_run[run_index] + score_case(found, bugs[case_id])
    return per_run


def _quality(per_run: Sequence[Score]) -> dict[str, Any]:
    return {
        "precision": asdict(spread([s.precision for s in per_run])),
        "recall": asdict(spread([s.recall for s in per_run])),
        "f1": asdict(spread([s.f1 for s in per_run])),
    }


def _row_label(stages: Sequence[Stage]) -> str:
    return "single-pass" if not stages else "+ " + " + ".join(stages)


def ablation_rows(
    cases: Sequence[Case],
    reviews: Sequence[ReviewRecord],
    runs: int,
    stages: Sequence[Stage],
    threshold: float,
) -> list[dict[str, Any]]:
    """Single-pass, then each enabled stage added in the spec's order.

    Every row is computed from the same stored replies. Cost and latency add
    the grade call from the first row that grades; dedup and suppress are free.
    """
    rows = []
    for n in range(len(stages) + 1):
        enabled = list(stages[:n])
        grading = Stage.GRADE in enabled
        kept = [
            (
                r.run_index,
                r.case_id,
                [c.finding for c in apply(r.candidate_list(), enabled, threshold)],
            )
            for r in reviews
        ]
        costs = [
            r.generate.cost_usd + (r.grade.cost_usd if grading and r.grade else 0.0)
            for r in reviews
        ]
        latencies = [
            r.generate.latency_s + (r.grade.latency_s if grading and r.grade else 0.0)
            for r in reviews
        ]
        rows.append(
            {
                "row": _row_label(enabled),
                "stages": [str(s) for s in enabled],
                **_quality(score_runs(cases, kept, runs)),
                "findings_per_review": statistics.fmean(len(k[2]) for k in kept),
                "cost_per_review_usd": statistics.fmean(costs),
                "latency_s": {
                    "p50": percentile(latencies, 50),
                    "p95": percentile(latencies, 95),
                },
            }
        )
    return rows


def threshold_sweep(
    cases: Sequence[Case],
    reviews: Sequence[ReviewRecord],
    runs: int,
    stages: Sequence[Stage],
) -> dict[str, Any]:
    """P/R/F1 at each grade threshold, with the other enabled stages applied.

    `best_f0_5` is the threshold with the highest mean F0.5 (precision weighted
    twice recall), the highest threshold on a tie. It is chosen on the same
    golden set it's scored on, so it is optimistic; see the spec note.
    """
    rows = []
    for t in SWEEP_THRESHOLDS:
        kept = [
            (
                r.run_index,
                r.case_id,
                [c.finding for c in apply(r.candidate_list(), stages, t)],
            )
            for r in reviews
        ]
        per_run = score_runs(cases, kept, runs)
        rows.append(
            {
                "threshold": t,
                **_quality(per_run),
                "f0_5": statistics.fmean(_f_beta(s, 0.5) for s in per_run),
                "findings_per_review": statistics.fmean(len(k[2]) for k in kept),
            }
        )
    best = max(rows, key=lambda row: (round(row["f0_5"], 12), row["threshold"]))
    return {"rows": rows, "best_f0_5": best["threshold"]}


def _f_beta(s: Score, beta: float) -> float:
    p, r = s.precision, s.recall
    b2 = beta * beta
    return 0.0 if p + r == 0 else (1 + b2) * p * r / (b2 * p + r)


def findings_per_case(
    reviews: Sequence[ReviewRecord],
    stages: Sequence[Stage],
    threshold: float,
) -> dict[str, dict[str, float]]:
    """Mean findings per review for each case, at each ablation row."""
    out: dict[str, dict[str, list[int]]] = {}
    for n in range(len(stages) + 1):
        enabled = list(stages[:n])
        label = _row_label(enabled)
        for r in reviews:
            kept = apply(r.candidate_list(), enabled, threshold)
            out.setdefault(r.case_id, {}).setdefault(label, []).append(len(kept))
    return {
        case: {label: statistics.fmean(v) for label, v in rows.items()}
        for case, rows in out.items()
    }


def summarise(
    *,
    cases: Sequence[Case],
    reviews: Sequence[ReviewRecord],
    arm: Arm,
    runs: int,
    run_id: str,
    fresh: int,
    spent: float,
    limit: float,
    stages: Sequence[Stage] = (),
    grade_arm: Arm | None = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    per_run = score_runs(
        cases, [(r.run_index, r.case_id, r.findings) for r in reviews], runs
    )
    costs = [r.cost_usd for r in reviews]
    latencies = [r.latency_s for r in reviews]
    grades = [r.grade for r in reviews if r.grade is not None]
    summary: dict[str, Any] = {
        "run_id": run_id,
        "arm": arm.name,
        "model": arm.model,
        "arm_config_hash": arm.config_hash(),
        "stage": STAGE,
        "stages": [str(s) for s in stages],
        "cases": len(cases),
        # Compare arms only on identical case sets.
        "case_ids": sorted(c.id for c in cases),
        "runs": runs,
        "reviews": len(reviews),
        **_quality(per_run),
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
        "truncated_attempts": sum(r.generate.truncated for r in reviews),
        "reasoning_tokens": sum(r.generate.reasoning_tokens for r in reviews),
        "suppressed_category_findings": sum(
            is_suppressed(f) for r in reviews for f in r.findings
        ),
        "http_retries": sum(r.http_retries for r in reviews),
        "retry_wait_s": sum(r.retry_wait_s for r in reviews),
        "fresh_reviews": fresh,
        "spent_this_invocation_usd": spent,
        "budget_usd": limit,
    }
    if grade_arm is not None:
        summary["grade"] = {
            "arm": grade_arm.name,
            "model": grade_arm.model,
            "arm_config_hash": grade_arm.config_hash(),
            "stage": GRADE_STAGE,
            "threshold": threshold,
            "reviews_graded": sum(g.attempts > 0 for g in grades),
            "calls": sum(g.attempts for g in grades),
            "malformed": sum(g.scores is None for g in grades),
            "truncated_attempts": sum(g.truncated for g in grades),
        }
        summary["grade_sweep"] = threshold_sweep(cases, reviews, runs, stages)
    summary["ablation"] = ablation_rows(cases, reviews, runs, stages, threshold)
    summary["findings_per_case"] = findings_per_case(reviews, stages, threshold)
    return summary
