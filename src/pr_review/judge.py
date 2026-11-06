"""Claim judge (item 7): does a location-matched finding name the right defect?

The scorer matches on location only (same file, ±3 lines), so a finding on the
right lines for the wrong reason still counts as a true positive. The owner
chose not to hand-label findings, so a model from another family than the
reviewer stands in: for each location-matched finding on a bug case it reads

- the buggy diff (the PR under review),
- the fix diff, which is the reverse of the buggy diff (the case reverts a
  maintainer's fix, so undoing the revert is the fix), and
- the finding's location and text (the agent's claim and evidence, or
  CodeRabbit's comment prose),

and answers "does this finding describe the same defect as the ground-truth
fix?" with yes/no and a one-line reason. Findings on clean cases are false
positives whatever their reason, so they aren't judged.

From the verdicts each row gets a right-reason rate (confirmed / judged
location-matched findings) and claim-level precision, recall and F1, where a
finding counts as matched only if it is location-matched *and* confirmed.
Verdicts are cached per (judge arm config, prompt version, case, finding,
sample), so rows that share a finding (CodeRabbit's all-comments and
Major/Critical rows, or one finding repeated across runs) share its verdict,
and a rerun costs nothing. `sample` 1 is a second, independent verdict on a
seeded subset, for the self-consistency check. The judge has not been checked
against human labels.
"""

import hashlib
import json
import random
import statistics
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, ValidationError

from pr_review.arms import Arm
from pr_review.ask import ASK_VERSION, RETRY_PROMPT, MalformedReply, ask_json, load_json
from pr_review.llm import Budget, Cancelled, make_client
from pr_review.models import Bug, Case, CaseKind, Finding
from pr_review.scoring import Score, matches, score_case
from pr_review.stages import Candidate, Stage, apply

JUDGE_PROMPT = """\
You check whether a code-review finding on a Go pull request names a known bug.

You get the pull request's diff, the fix the maintainers later made for a bug \
in it, and one finding a reviewer left at lines near that fix. Decide whether \
the finding describes the same defect the fix addresses: the same root cause \
and the same wrong behaviour.

- Yes: it identifies that defect, even in other words, or with a different \
suggested fix, or with extra remarks.
- No: it is about something else at those lines (another bug, a style or \
naming point, a generic concern), or it is too vague to tell which defect it \
means, or it gets the defect wrong.

The fix may also carry unrelated edits; judge only against the defect it fixes.

Reply with one JSON object and nothing else:
{"reason": "<one short sentence>", "same_defect": <true or false>}\
"""

JUDGE_VERSION = hashlib.sha256(
    (JUDGE_PROMPT + RETRY_PROMPT + ASK_VERSION).encode()
).hexdigest()[:12]
STAGE = f"judge-{JUDGE_VERSION}"
REPEAT_SEED = 7


# --- inputs -----------------------------------------------------------------


def reverse_diff(diff: str) -> str:
    """The reverse of a unified diff: what undoes it.

    Swaps the `---`/`+++` headers, the old and new ranges in each hunk header,
    and `-`/`+` on changed lines. Context lines and `\\ No newline` markers stay.
    """
    out: list[str] = []
    lines = diff.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if line.startswith("--- ") and nxt.startswith("+++ "):
            out += ["--- " + nxt[4:], "+++ " + line[4:]]
            i += 2
            continue
        if line.startswith("@@"):
            parts = line.split(" ", 3)
            if len(parts) >= 3 and parts[1][:1] == "-" and parts[2][:1] == "+":
                rest = f" {parts[3]}" if len(parts) > 3 else ""
                line = f"@@ -{parts[2][1:]} +{parts[1][1:]}{rest}"
        elif line.startswith("-"):
            line = "+" + line[1:]
        elif line.startswith("+"):
            line = "-" + line[1:]
        out.append(line)
        i += 1
    return "\n".join(out) + ("\n" if diff.endswith("\n") else "")


@dataclass(frozen=True)
class Item:
    """One finding in one review, with the text the judge reads as its claim."""

    case_id: str
    run_index: int
    finding: Finding
    text: str

    @property
    def key(self) -> tuple[str, str, int, int, str]:
        """Identity across runs and rows: the same finding gets the same verdict."""
        f = self.finding
        return (self.case_id, f.file, f.lines.start, f.lines.end, self.text)


# A row: every review, keyed by (case id, run index), with its findings.
Row = dict[tuple[str, int], list[Item]]


def agent_text(f: Finding) -> str:
    return f"{f.claim}\nEvidence: {f.evidence}" if f.evidence else f.claim


def load_agent_row(path: Path, *, without_grade: bool = False) -> Row:
    """A `reviews.jsonl` from `pr-review eval`: the findings each review posted.

    `without_grade` rebuilds a graded run's single-pass row instead: every
    candidate, scores dropped, through dedup and suppress only. That is what
    the same replies give without the grade stage.
    """
    row: Row = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        cid, run = rec["case_id"], int(rec["run_index"])
        if without_grade:
            cands = [
                Candidate(Finding.model_validate(c["finding"]))
                for c in rec["candidates"]
            ]
            found = [c.finding for c in apply(cands, [Stage.DEDUP, Stage.SUPPRESS])]
        else:
            found = [Finding.model_validate(f) for f in rec["findings"]]
        row[(cid, run)] = [Item(cid, run, f, agent_text(f)) for f in found]
    return row


def load_coderabbit_row(path: Path, serious_only: bool) -> Row:
    """`results/coderabbit/findings.json`: located comments, 1 run (index 0).

    The judge reads each comment's prose (`detail`), or its title if there is
    none. Comments with no line are not scored, as in `coderabbit.score_arm`.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    row: Row = {}
    for cid, comments in data.items():
        items = []
        for c in comments:
            if c["finding"] is None:
                continue
            if serious_only and c["severity"] not in ("Major", "Critical"):
                continue
            f = Finding.model_validate(c["finding"])
            items.append(Item(cid, 0, f, c.get("detail") or f.claim))
        row[(cid, 0)] = items
    return row


def location_matched(item: Item, bugs: Sequence[Bug]) -> bool:
    return any(matches(item.finding, b) for b in bugs)


# --- one verdict ---------------------------------------------------------------


class Verdict(BaseModel):
    """One stored judge reply. `same_defect` is None if still malformed."""

    model_config = ConfigDict(extra="forbid")

    same_defect: bool | None
    reason: str
    attempts: int
    truncated: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_s: float


class _Reply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    same_defect: bool
    reason: str = ""


def parse_verdict(content: str) -> tuple[bool, str]:
    """(same defect, reason). Raises `MalformedReply`."""
    try:
        r = _Reply.model_validate(load_json(content))
    except ValidationError as e:
        raise MalformedReply(str(e)) from e
    return r.same_defect, r.reason.strip()


def build_messages(diff: str, item: Item) -> list[ChatCompletionMessageParam]:
    f = item.finding
    loc = f"{f.file}, lines {f.lines.start}-{f.lines.end}"
    user = (
        "## The pull request (diff under review)\n\n"
        f"```diff\n{diff.rstrip()}\n```\n\n"
        "## The maintainers' fix for the bug in it\n\n"
        f"```diff\n{reverse_diff(diff).rstrip()}\n```\n\n"
        f"## The finding\n\nLocation: {loc}\n\n{item.text}\n"
    )
    return [
        {"role": "system", "content": JUDGE_PROMPT},
        {"role": "user", "content": user},
    ]


def judge_one(
    client: OpenAI, arm: Arm, diff: str, item: Item, budget: Budget
) -> Verdict:
    r = ask_json(client, arm, build_messages(diff, item), budget, parse_verdict)
    same, reason = r.value if r.value is not None else (None, "")
    return Verdict(
        same_defect=same,
        reason=reason,
        attempts=r.attempts,
        truncated=r.truncated,
        prompt_tokens=r.prompt_tokens,
        completion_tokens=r.completion_tokens,
        reasoning_tokens=r.reasoning_tokens,
        cost_usd=r.cost_usd,
        latency_s=r.latency_s,
    )


class VerdictCache:
    """One JSON file per (judge arm config, prompt version, case, finding, sample)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, arm: Arm, diff: str, item: Item, sample: int) -> Path:
        blob = json.dumps([diff, *item.key[1:]], ensure_ascii=False)
        h = hashlib.sha256(blob.encode()).hexdigest()[:16]
        return (
            self.root / arm.config_hash() / STAGE / item.case_id / h
        ) / f"sample-{sample}.json"

    def get(self, arm: Arm, diff: str, item: Item, sample: int) -> Verdict | None:
        p = self.path(arm, diff, item, sample)
        if not p.exists():
            return None
        return Verdict.model_validate_json(p.read_text(encoding="utf-8"))

    def put(self, arm: Arm, diff: str, item: Item, sample: int, v: Verdict) -> None:
        p = self.path(arm, diff, item, sample)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(v.model_dump_json(), encoding="utf-8")
        tmp.replace(p)


# --- scoring -------------------------------------------------------------------


def claim_score(
    items: Sequence[Item], bugs: Sequence[Bug], confirmed: Sequence[bool]
) -> Score:
    """`score_case`, but a finding matches only if it is located *and* confirmed."""
    kept = [
        it.finding
        for it, ok in zip(items, confirmed, strict=True)
        if ok and location_matched(it, bugs)
    ]
    s = score_case(kept, bugs)
    clean = not bugs
    return Score(
        findings=len(items),
        matched_findings=s.matched_findings,
        bugs=len(bugs),
        matched_bugs=s.matched_bugs,
        clean_reviews=int(clean),
        clean_findings=len(items) if clean else 0,
        flagged_clean_reviews=int(clean and bool(items)),
    )


def _mean(values: Iterable[float]) -> float:
    v = list(values)
    return statistics.fmean(v) if v else 0.0


def score_row(
    row: Row, cases: Mapping[str, Case], verdicts: Mapping[tuple[Any, ...], Verdict]
) -> dict[str, Any]:
    """Location- and claim-level scores for one row, mean over its runs.

    A malformed verdict counts as not confirmed, and is counted.
    """
    runs = sorted({r for _, r in row})
    loc_runs: list[Score] = []
    claim_runs: list[Score] = []
    judged = confirmed = malformed = 0
    for run in runs:
        loc_total, claim_total = Score(), Score()
        for cid, case in cases.items():
            items = row.get((cid, run), [])
            ok = []
            for it in items:
                if case.kind is CaseKind.BUG and location_matched(it, case.bugs):
                    v = verdicts[it.key]
                    judged += 1
                    confirmed += v.same_defect is True
                    malformed += v.same_defect is None
                    ok.append(v.same_defect is True)
                else:
                    ok.append(False)
            loc_total += score_case([it.finding for it in items], case.bugs)
            claim_total += claim_score(items, case.bugs, ok)
        loc_runs.append(loc_total)
        claim_runs.append(claim_total)

    def level(scores: Sequence[Score]) -> dict[str, float]:
        return {
            "precision": _mean(s.precision for s in scores),
            "recall": _mean(s.recall for s in scores),
            "f1": _mean(s.f1 for s in scores),
            "f1_min": min(s.f1 for s in scores),
            "f1_max": max(s.f1 for s in scores),
        }

    return {
        "runs": len(runs),
        "findings": sum(s.findings for s in loc_runs),
        "location": level(loc_runs),
        "claim": level(claim_runs),
        "fp_per_clean_review": _mean(s.fp_per_clean_review for s in loc_runs),
        "location_matched": judged,
        "confirmed": confirmed,
        "malformed_verdicts": malformed,
        "right_reason_rate": confirmed / judged if judged else 0.0,
    }


def agreement(pairs: Sequence[tuple[bool, bool]]) -> dict[str, float]:
    """Raw agreement and Cohen's kappa between two verdicts on the same findings."""
    n = len(pairs)
    if n == 0:
        return {"n": 0, "agreement": 0.0, "kappa": 0.0}
    agree = sum(a == b for a, b in pairs) / n
    pa = sum(a for a, _ in pairs) / n
    pb = sum(b for _, b in pairs) / n
    chance = pa * pb + (1 - pa) * (1 - pb)
    kappa = 1.0 if chance == 1 else (agree - chance) / (1 - chance)
    return {
        "n": n,
        "agreement": agree,
        "kappa": kappa,
        "yes_first": pa,
        "yes_second": pb,
    }


class Pacer:
    """Spaces out call starts to at most `rpm` a minute, across threads.

    Some OpenRouter models cap new accounts' requests per minute (20 for the
    judge's model in 2026-11); a run that hits the cap would burn its 429
    retries and stop.
    """

    def __init__(self, rpm: float | None, clock: Any = time.monotonic) -> None:
        self.interval = 60.0 / rpm if rpm else 0.0
        self._clock = clock
        self._next = 0.0
        self._lock = threading.Lock()

    def delay(self) -> float:
        """Reserve the next start slot; return how long to wait for it."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self.interval
            return start - now


# --- the run -------------------------------------------------------------------


def run_judge(
    *,
    rows: Mapping[str, Row],
    cases: Sequence[Case],
    diffs: Mapping[str, str],
    arm: Arm,
    cache_dir: Path,
    budget: Budget,
    repeat: int = 0,
    concurrency: int = 8,
    max_rpm: float | None = None,
    client: OpenAI | None = None,
) -> dict[str, Any]:
    """Judge every location-matched finding in `rows` (once per distinct finding),
    plus a second verdict on `repeat` of them, and score each row.

    `cases` is the case set every row is scored on; reviews of other cases are
    ignored. `diffs` maps case id to its buggy diff. `max_rpm` paces call
    starts for providers with a requests-per-minute cap. Paid verdicts are cached as
    they land, so a run stopped by the budget can be rerun to finish.
    """
    by_id = {c.id: c for c in cases}
    todo: dict[tuple[Any, ...], Item] = {}
    for row in rows.values():
        for (cid, _), items in row.items():
            case = by_id.get(cid)
            if case is None or case.kind is not CaseKind.BUG:
                continue
            for it in items:
                if location_matched(it, case.bugs):
                    todo.setdefault(it.key, it)
    unique = sorted(todo)
    rng = random.Random(REPEAT_SEED)
    resample = sorted(rng.sample(unique, min(repeat, len(unique))))
    jobs = [(k, 0) for k in unique] + [(k, 1) for k in resample]

    cache = VerdictCache(cache_dir)
    results: dict[tuple[tuple[Any, ...], int], Verdict] = {}
    paid: list[tuple[tuple[Any, ...], int]] = []
    for k, s in jobs:
        hit = cache.get(arm, diffs[k[0]], todo[k], s)
        if hit is None:
            paid.append((k, s))
        else:
            results[(k, s)] = hit
    if paid:
        cl = client or make_client(arm)
        pacer = Pacer(max_rpm)

        def work(job: tuple[tuple[Any, ...], int]) -> None:
            k, s = job
            it, diff = todo[k], diffs[k[0]]
            if budget.cancelled.wait(pacer.delay()):
                raise Cancelled("run cancelled while pacing")
            v = judge_one(cl, arm, diff, it, budget)
            cache.put(arm, diff, it, s, v)
            results[job] = v

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(work, j) for j in paid]
            errors = []
            for fut in futures:
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001 - first error re-raised below
                    budget.cancel()
                    errors.append(e)
            if errors:
                # The first real error, not the cancellations it caused.
                raise next(
                    (e for e in errors if not isinstance(e, Cancelled)), errors[0]
                )

    first = {k: results[(k, 0)] for k in unique}
    pairs = [
        (bool(first[k].same_defect), bool(results[(k, 1)].same_defect))
        for k in resample
        if first[k].same_defect is not None and results[(k, 1)].same_defect is not None
    ]
    cost = sum(results[j].cost_usd for j in paid)
    return {
        "judge_arm": arm.name,
        "judge_model": arm.model,
        "stage": STAGE,
        "cases": len(cases),
        "distinct_findings_judged": len(unique),
        "paid_calls": len(paid),
        "cost_usd_this_run": cost,
        "cost_usd_all_verdicts": sum(v.cost_usd for v in results.values()),
        "malformed_verdicts": sum(v.same_defect is None for v in results.values()),
        "rows": {name: score_row(row, by_id, first) for name, row in rows.items()},
        "self_consistency": {
            **agreement(pairs),
            "sampled": len(resample),
            "seed": REPEAT_SEED,
        },
        "verdicts": [
            {
                "case_id": k[0],
                "file": k[1],
                "lines": [k[2], k[3]],
                "text": k[4],
                "same_defect": first[k].same_defect,
                "reason": first[k].reason,
                **(
                    {"second": results[(k, 1)].same_defect} if (k, 1) in results else {}
                ),
            }
            for k in unique
        ],
    }
