"""Stage 2, grade: a second model call scores each candidate finding from 0 to 1.

The score answers "is this a real defect that this diff introduces". One call
grades all of a review's candidates, so a review costs at most two calls
(plus their retries) whatever it found. The grader sees what the generator saw
(the diff and the changed files at base) plus the candidates, and nothing from
the case's ground truth. Its arm is configurable and defaults to the
generator's.

Candidates are dropped below a threshold afterwards (`stages.py`), not here, so
the stored scores support a threshold sweep without paying again.
"""

import hashlib
import json
from dataclasses import dataclass

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pr_review.arms import Arm
from pr_review.ask import ASK_VERSION, RETRY_PROMPT, MalformedReply, ask_json, load_json
from pr_review.generate import ReviewInput, pr_text
from pr_review.llm import Budget
from pr_review.models import Finding

GRADE_PROMPT = """\
You check another reviewer's findings on a Go pull request. For each numbered \
finding, decide whether it describes a real defect that THIS diff introduces and \
that would make the code behave wrongly at runtime, at the lines it names.

Score each finding from 0 to 1:
- 1.0: certain. The diff clearly introduces this bug and the claim is correct.
- 0.7: likely, but it depends on code you can't see.
- 0.3: doubtful: speculative, already present before the diff, or the claim \
does not match the code at those lines.
- 0.0: wrong, not a runtime defect (style, naming, docs, readability), or \
not caused by this diff.

Judge each finding on its own. A wrong finding costs more than a missed one.

Reply with one JSON object and nothing else, with one entry per finding:
{"grades": [{"index": <int>, "reason": "<one short sentence>", "score": <0 to 1>}]}\
"""

GRADE_VERSION = hashlib.sha256(
    (GRADE_PROMPT + RETRY_PROMPT + ASK_VERSION).encode()
).hexdigest()[:12]
STAGE = f"grade-{GRADE_VERSION}"


@dataclass(frozen=True)
class GradeResult:
    scores: list[float] | None  # one per candidate, in order; None if malformed
    attempts: int
    truncated: int
    retries: int
    retry_wait_s: float
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_s: float


class _Grade(BaseModel):
    model_config = ConfigDict(extra="ignore")

    index: int
    score: float = Field(ge=0, le=1)


class _Reply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    grades: list[_Grade]


def parse_grades(content: str, n: int) -> list[float]:
    """Scores for candidates 0..n-1, in order. Raises `MalformedReply`."""
    try:
        reply = _Reply.model_validate(load_json(content))
    except ValidationError as e:
        raise MalformedReply(str(e)) from e
    scores: dict[int, float] = {}
    for g in reply.grades:
        if not 0 <= g.index < n:
            raise MalformedReply(f"index {g.index} is not a finding (0..{n - 1})")
        if g.index in scores:
            raise MalformedReply(f"finding {g.index} graded twice")
        scores[g.index] = g.score
    missing = sorted(set(range(n)) - scores.keys())
    if missing:
        raise MalformedReply(f"no grade for findings {missing}")
    return [scores[i] for i in range(n)]


def build_grade_messages(
    review: ReviewInput, candidates: list[Finding]
) -> list[ChatCompletionMessageParam]:
    pr = pr_text(review)
    listed = [
        {
            "index": i,
            "file": f.file,
            "start_line": f.lines.start,
            "end_line": f.lines.end,
            "category": f.category,
            "claim": f.claim,
            "evidence": f.evidence,
        }
        for i, f in enumerate(candidates)
    ]
    findings = json.dumps(listed, indent=1)
    return [
        {"role": "system", "content": GRADE_PROMPT},
        {
            "role": "user",
            "content": f"{pr}\n## Findings to grade\n\n```json\n{findings}\n```\n",
        },
    ]


def grade(
    client: OpenAI,
    arm: Arm,
    review: ReviewInput,
    candidates: list[Finding],
    budget: Budget,
) -> GradeResult:
    """Score every candidate in one call. No candidates: no call, no cost."""
    if not candidates:
        return GradeResult([], 0, 0, 0, 0.0, 0, 0, 0, 0.0, 0.0)
    n = len(candidates)
    r = ask_json(
        client,
        arm,
        build_grade_messages(review, candidates),
        budget,
        lambda content: parse_grades(content, n),
    )
    return GradeResult(
        scores=r.value,
        attempts=r.attempts,
        truncated=r.truncated,
        retries=r.retries,
        retry_wait_s=r.retry_wait_s,
        prompt_tokens=r.prompt_tokens,
        completion_tokens=r.completion_tokens,
        reasoning_tokens=r.reasoning_tokens,
        cost_usd=r.cost_usd,
        latency_s=r.latency_s,
    )
