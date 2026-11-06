"""Review one real PR (item 8): the diff plus a tree of the repo at the PR's base.

`pr-review review` is what CI runs. It uses the same generate prompt and stages
as the eval, so a CI finding comes from the configuration the golden set
scored. Its output is a JSON report that `pr-review post` turns into inline
comments and the findings log.

Only `.go` files are reviewed: the prompt, the bug classes and the golden set
are Go-only, so the diff is cut down to Go files before the model sees it, and
a PR that touches no Go file costs nothing.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from pr_review.arms import Arm
from pr_review.generate import (
    PROMPT_VERSION,
    GenerateResult,
    ReviewInput,
    changed_base_paths,
    generate,
    read_base_files,
)
from pr_review.grade import GRADE_VERSION, grade
from pr_review.llm import Budget, make_client
from pr_review.models import Finding
from pr_review.stages import (
    DEDUP_VERSION,
    SUPPRESS_VERSION,
    Candidate,
    Stage,
    apply,
)
from pr_review.tools import RepoTools, generate_with_tools, tools_version

# Hard spend limit for one CI review. A typical review is ~$0.004 on
# or-deepseek-flash; the limit leaves room for the worst-case reservations of a
# cut-off reply and its retry, or a few tool turns.
DEFAULT_REVIEW_BUDGET_USD = 0.05
# CI default: single-pass plus dedup and suppress. Grade is off: on the
# combined golden set it cost recall for no measured precision (item 4a).
DEFAULT_REVIEW_STAGES = (Stage.DEDUP, Stage.SUPPRESS)

_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")


class ReportFinding(BaseModel):
    """One finding in the review report, with its grade if it was graded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding: Finding
    score: float | None = None


class Report(BaseModel):
    """What `pr-review review` prints and `pr-review post` reads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm: str
    model: str
    prompt_version: str
    stage_versions: dict[str, str]
    findings: list[ReportFinding]
    malformed: bool = False  # the generator's reply stayed malformed after its retry
    go_files: int = Field(ge=0)  # Go files the reviewed diff touches
    cost_usd: float = Field(ge=0)


def _file_of(block: str) -> str:
    """The new-side path of one `diff --git` block (the old path for a deletion)."""
    old = ""
    for line in block.splitlines():
        if line.startswith("--- ") and line[4:].strip() != "/dev/null":
            old = line[4:].strip().removeprefix("a/")
        elif line.startswith("+++ "):
            new = line[4:].strip()
            return old if new == "/dev/null" else new.removeprefix("b/")
    m = _DIFF_HEADER.match(block.split("\n", 1)[0])
    return m.group(2) if m else old


def go_only(diff: str) -> tuple[str, int]:
    """Keep the `diff --git` blocks for `.go` files. Returns the diff and their count.

    A diff without `diff --git` headers (a bare `---`/`+++` patch) is one block.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            blocks.append("".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("".join(current))
    kept = [b for b in blocks if PurePosixPath(_file_of(b)).suffix == ".go"]
    return "".join(kept), len(kept)


@dataclass(frozen=True)
class _Generated:
    result: GenerateResult
    version: str


def review_pr(
    *,
    diff: str,
    base_dir: Path,
    arm: Arm,
    budget: Budget,
    stages: Sequence[Stage] = DEFAULT_REVIEW_STAGES,
    grade_arm: Arm | None = None,
    threshold: float = 0.3,
    tools: bool = False,
    max_tool_turns: int = 4,
) -> Report:
    """Review one PR's diff against `base_dir`, a tree of the repo at the PR's base.

    The model sees the Go part of the diff and each changed Go file's text at
    base. With `tools`, it may also read and grep `base_dir`.
    """
    go_diff, n_files = go_only(diff)
    versions: dict[str, str] = {}
    if not go_diff.strip():
        return Report(
            arm=arm.name,
            model=arm.model,
            prompt_version=PROMPT_VERSION,
            stage_versions=versions,
            findings=[],
            go_files=0,
            cost_usd=0.0,
        )
    review = ReviewInput(
        diff=go_diff,
        base_files=read_base_files(base_dir, changed_base_paths(go_diff)),
    )
    client = make_client(arm)
    if tools:
        gen = _Generated(
            generate_with_tools(
                client, arm, review, budget, RepoTools(base_dir), max_tool_turns
            ),
            f"tools-{tools_version(max_tool_turns)}",
        )
    else:
        gen = _Generated(generate(client, arm, review, budget), PROMPT_VERSION)
    versions["generate"] = gen.version
    cost = gen.result.cost_usd
    candidates = [Candidate(f) for f in gen.result.findings]

    if Stage.GRADE in stages and candidates:
        g_arm = grade_arm or arm
        g = grade(
            make_client(g_arm),
            g_arm,
            review,
            [c.finding for c in candidates],
            budget,
        )
        cost += g.cost_usd
        # A grader reply that stayed malformed leaves every candidate ungraded,
        # and the grade stage then drops them (precision first).
        scores: list[float | None] = (
            list(g.scores) if g.scores else [None] * len(candidates)
        )
        candidates = [
            Candidate(c.finding, s) for c, s in zip(candidates, scores, strict=True)
        ]
    if Stage.GRADE in stages:
        versions["grade"] = f"{GRADE_VERSION}@{threshold}"
    if Stage.DEDUP in stages:
        versions["dedup"] = DEDUP_VERSION
    if Stage.SUPPRESS in stages:
        versions["suppress"] = SUPPRESS_VERSION

    kept = apply(candidates, stages, threshold)
    return Report(
        arm=arm.name,
        model=arm.model,
        prompt_version=PROMPT_VERSION,
        stage_versions=versions,
        findings=[ReportFinding(finding=c.finding, score=c.score) for c in kept],
        malformed=gen.result.malformed,
        go_files=n_files,
        cost_usd=cost,
    )
