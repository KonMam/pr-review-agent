"""Stage 1, generate: one model call per review, no tool loop.

The model sees only the PR: the unified diff and the full text of each changed
file at the base commit. It never sees the case's ground truth, bug class, or
provenance (eval leakage).
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pr_review.arms import Arm
from pr_review.llm import Budget, chat
from pr_review.models import Finding, LineRange

SYSTEM_PROMPT = """\
You review Go pull requests for bugs. Report only defects this diff introduces \
that would make the code behave wrongly at runtime. Look hardest at what linters \
and `go vet` miss:
- nil dereferences and writes to nil maps
- ignored or shadowed `err`
- goroutine leaks and missing cancellation
- data races and mutex misuse
- `defer` inside loops
- slice aliasing after `append`
- wrong `context` propagation
- integer overflow and off-by-one
- unclosed resources (files, response bodies, rows)

Never comment on style, naming, formatting, docs or readability. If you find no \
real bug, return an empty list: a wrong finding costs more than a missed one.

Reply with one JSON object and nothing else:
{"findings": [{"file": "<path as in the diff>", "start_line": <int>, \
"end_line": <int>, "category": "<one of: nil_deref, ignored_err, goroutine_leak, \
data_race, defer_in_loop, slice_aliasing, context_propagation, \
overflow_off_by_one, unclosed_resource, other_bug>", "claim": "<what is wrong>", \
"evidence": "<the code that shows it>"}]}
Line numbers refer to the file after the diff is applied.\
"""

RETRY_PROMPT = """\
Your reply was not valid. Error: {error}
Reply again with only the JSON object described above.\
"""

# Changes whenever the prompt or the parsing contract changes, so cached
# replies from an older prompt aren't reused.
PROMPT_VERSION = hashlib.sha256((SYSTEM_PROMPT + RETRY_PROMPT).encode()).hexdigest()[
    :12
]


class MalformedReply(ValueError):
    """The model's reply isn't the JSON shape the prompt asks for."""


@dataclass(frozen=True)
class ReviewInput:
    """Everything the model is allowed to see for one review."""

    diff: str
    base_files: dict[str, str]  # path -> full text at the base commit


@dataclass(frozen=True)
class GenerateResult:
    findings: list[Finding]
    malformed: bool  # still malformed after the one retry
    attempts: int
    retries: int  # 429/5xx retries across attempts
    retry_wait_s: float  # their failed attempts and backoff, not in latency_s
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_s: float


class _ReplyFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    category: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    evidence: str = ""

    def to_finding(self) -> Finding:
        start, end = sorted((self.start_line, self.end_line))
        return Finding(
            file=self.file,
            lines=LineRange(start=start, end=end),
            category=self.category,
            claim=self.claim,
            evidence=self.evidence,
        )


class _Reply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    findings: list[_ReplyFinding]


_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def parse_reply(content: str) -> list[Finding]:
    """Validate a reply into findings. Raises `MalformedReply`."""
    text = content.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise MalformedReply(f"not JSON: {e.msg} at char {e.pos}") from e
    try:
        reply = _Reply.model_validate(data)
    except ValidationError as e:
        raise MalformedReply(str(e)) from e
    return [f.to_finding() for f in reply.findings]


def build_messages(review: ReviewInput) -> list[ChatCompletionMessageParam]:
    parts = ["## Diff\n", "```diff\n", review.diff.rstrip("\n"), "\n```\n"]
    if review.base_files:
        parts.append("\n## Changed files at the base commit\n")
        for path, text in sorted(review.base_files.items()):
            parts += [f"\n### {path}\n", "```go\n", text.rstrip("\n"), "\n```\n"]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "".join(parts)},
    ]


def generate(
    client: OpenAI, arm: Arm, review: ReviewInput, budget: Budget
) -> GenerateResult:
    """One review: a call, and one retry if the reply is malformed."""
    messages = build_messages(review)
    prompt_tokens = completion_tokens = retries = 0
    cost = latency = retry_wait = 0.0
    for attempt in (1, 2):
        reply = chat(client, arm, messages, budget)
        prompt_tokens += reply.prompt_tokens
        completion_tokens += reply.completion_tokens
        cost += reply.cost_usd
        latency += reply.latency_s
        retries += reply.retries
        retry_wait += reply.retry_wait_s
        try:
            findings = parse_reply(reply.content)
        except MalformedReply as e:
            if attempt == 2:
                findings, malformed = [], True
                break
            messages = [
                *messages,
                {"role": "assistant", "content": reply.content},
                {"role": "user", "content": RETRY_PROMPT.format(error=str(e)[:500])},
            ]
            continue
        malformed = False
        break
    return GenerateResult(
        findings=findings,
        malformed=malformed,
        attempts=attempt,
        retries=retries,
        retry_wait_s=retry_wait,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        latency_s=latency,
    )


# --- Building a ReviewInput from a case -------------------------------------

_DIFF_OLD = re.compile(r"^--- (?:a/)?(.+?)\s*$")


def changed_base_paths(diff: str) -> list[str]:
    """Paths the diff changes that exist at base (new files have `--- /dev/null`)."""
    paths: list[str] = []
    for line in diff.splitlines():
        m = _DIFF_OLD.match(line)
        if m and m.group(1) != "/dev/null" and m.group(1) not in paths:
            paths.append(m.group(1))
    return paths


def read_base_files(base_dir: Path, paths: list[str]) -> dict[str, str]:
    """Each path's text under a case's offline `base/` directory.

    `make_case` stores the touched files there (the fix commit minus comments
    the fix added), so reading them needs no clone and no network.
    """
    files: dict[str, str] = {}
    for path in paths:
        f = base_dir / path
        if not f.resolve().is_relative_to(base_dir.resolve()):
            raise ValueError(f"{path}: diff path escapes {base_dir}")
        if not f.is_file():
            raise FileNotFoundError(f"{base_dir}: missing base file {path}")
        files[path] = f.read_bytes().decode("utf-8", errors="replace")
    return files
