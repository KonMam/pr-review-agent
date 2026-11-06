"""Post a review report to a GitHub PR as inline comments, and log each posted finding.

`pr-review post` reads `pr-review review`'s report and the same diff, and makes
one PR review (event COMMENT, never APPROVE or REQUEST_CHANGES) through the REST
API with the token in `GITHUB_TOKEN`. Comments are terse: the category and the
claim. A finding whose lines aren't in the diff can't be an inline comment, so
it goes in the review body instead. Suppressed categories are dropped here too,
whatever stages ran, so no style comment is ever posted.

Every posted finding becomes one line of the JSONL findings log (item 8), the
input to item 9's feedback loop. Its comment id lets item 9 read reactions.
The token is only ever sent in the Authorization header, never printed.
"""

import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pr_review.review import Report, ReportFinding
from pr_review.stages import is_suppressed

DEFAULT_API_URL = "https://api.github.com"
MARKER = "<!-- pr-review -->"  # tags our reviews and comments
MAX_CLAIM_CHARS = 600

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def commentable_lines(diff: str) -> dict[str, list[set[int]]]:
    """For each file, the new-side line numbers GitHub accepts comments on, per hunk.

    Those are the added and context lines of each hunk. A multi-line comment
    must start and end in the same hunk, so the hunks are kept apart. Each
    hunk ends after the line counts in its `@@` header, so a following file's
    `---`/`+++` headers are never read as removed or added lines.
    """
    out: dict[str, list[set[int]]] = {}
    path: str | None = None
    hunk: set[int] = set()
    old_left = new_left = 0
    line_no = 0
    for line in diff.splitlines():
        if old_left > 0 or new_left > 0:
            if line.startswith("\\"):  # "\ No newline at end of file"
                continue
            if line.startswith("+"):
                hunk.add(line_no)
                line_no += 1
                new_left -= 1
            elif line.startswith("-"):
                old_left -= 1
            else:  # context; some tools strip the space of an empty line
                hunk.add(line_no)
                line_no += 1
                old_left -= 1
                new_left -= 1
            continue
        if line.startswith("+++ "):
            new = line[4:].strip()
            path = None if new == "/dev/null" else new.removeprefix("b/")
            continue
        m = _HUNK.match(line)
        if m and path is not None:
            old_left = int(m.group(1) or "1")
            line_no = int(m.group(2))
            new_left = int(m.group(3) or "1")
            hunk = set()
            out.setdefault(path, []).append(hunk)
    return out


@dataclass(frozen=True)
class Anchor:
    path: str
    start_line: int  # == line for a one-line comment
    line: int


def anchor(rf: ReportFinding, lines: dict[str, list[set[int]]]) -> Anchor | None:
    """Where a finding can sit inline: its lines that are in one hunk of the diff.

    Uses the first hunk that overlaps the finding, narrowed to the overlap.
    None when no line of the finding is in the diff.
    """
    f = rf.finding
    wanted = set(range(f.lines.start, f.lines.end + 1))
    for hunk in lines.get(f.file, []):
        hit = wanted & hunk
        if hit:
            return Anchor(f.file, min(hit), max(hit))
    return None


def comment_body(rf: ReportFinding) -> str:
    claim = " ".join(rf.finding.claim.split())
    if len(claim) > MAX_CLAIM_CHARS:
        claim = claim[: MAX_CLAIM_CHARS - 1] + "…"
    return f"**{rf.finding.category}**: {claim}\n\n{MARKER}"


class LogRecord(BaseModel):
    """One line of the findings log: one posted finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repo: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    pr: int = Field(ge=1)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    file: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    category: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    arm: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    stage_versions: dict[str, str]
    grade_score: float | None
    posted: str = Field(pattern=r"^(inline|body)$")  # where the finding was posted
    review_id: int | None
    comment_id: int | None  # the inline comment, for item 9's reactions
    comment_line: int | None  # the line the inline comment sits on


class GitHubError(RuntimeError):
    pass


class GitHub:
    """The few REST calls posting needs, with urllib (no new dependency)."""

    def __init__(self, token: str, api_url: str = DEFAULT_API_URL) -> None:
        self._token = token
        self.api_url = api_url.rstrip("/")

    def _call(self, method: str, path: str, body: Any | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            self.api_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "pr-review-agent",
                **({"Content-Type": "application/json"} if data else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise GitHubError(f"{method} {path}: HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise GitHubError(f"{method} {path}: {e.reason}") from None
        return json.loads(raw) if raw else None

    def create_review(
        self, repo: str, pr: int, head_sha: str, body: str, comments: list[dict]
    ) -> int:
        out = self._call(
            "POST",
            f"/repos/{repo}/pulls/{pr}/reviews",
            {
                "commit_id": head_sha,
                "event": "COMMENT",
                "body": body,
                "comments": comments,
            },
        )
        return int(out["id"])

    def review_comments(self, repo: str, pr: int, review_id: int) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            batch = self._call(
                "GET",
                f"/repos/{repo}/pulls/{pr}/reviews/{review_id}/comments"
                f"?per_page=100&page={page}",
            )
            out += batch
            if len(batch) < 100:
                return out
            page += 1


@dataclass(frozen=True)
class Posted:
    records: list[LogRecord]
    review_id: int | None


def _review_body(report: Report, outside: Sequence[ReportFinding]) -> str:
    lines = [f"pr-review ({report.model}): {_count(report.findings)}."]
    if outside:
        lines += ["", "Outside the diff:"]
        for rf in outside:
            f = rf.finding
            claim = comment_body(rf).removesuffix(f"\n\n{MARKER}")
            lines.append(f"- `{f.file}:{f.lines.start}-{f.lines.end}` {claim}")
    return "\n".join(lines + ["", MARKER])


def _count(findings: Sequence[ReportFinding]) -> str:
    return "1 finding" if len(findings) == 1 else f"{len(findings)} findings"


def post_report(
    gh: GitHub,
    *,
    report: Report,
    diff: str,
    repo: str,
    pr: int,
    head_sha: str,
) -> Posted:
    """Post one review with the report's findings. No findings: nothing is posted."""
    findings = [rf for rf in report.findings if not is_suppressed(rf.finding)]
    report = report.model_copy(update={"findings": findings})
    if not findings:
        return Posted([], None)
    lines = commentable_lines(diff)
    inline: list[tuple[ReportFinding, Anchor]] = []
    outside: list[ReportFinding] = []
    for rf in findings:
        a = anchor(rf, lines)
        if a is None:
            outside.append(rf)
        else:
            inline.append((rf, a))
    comments = []
    for rf, a in inline:
        c: dict[str, Any] = {
            "path": a.path,
            "line": a.line,
            "side": "RIGHT",
            "body": comment_body(rf),
        }
        if a.start_line != a.line:
            c |= {"start_line": a.start_line, "start_side": "RIGHT"}
        comments.append(c)
    review_id = gh.create_review(
        repo, pr, head_sha, _review_body(report, outside), comments
    )
    ids: dict[tuple[str, int, str], list[int]] = {}
    if inline:
        # The review is posted: a failed id lookup must not lose the log, so
        # its records keep comment_id None (item 9 can match by review_id).
        try:
            for c in gh.review_comments(repo, pr, review_id):
                line = c.get("line") or c.get("original_line") or 0
                ids.setdefault((c["path"], line, c["body"]), []).append(int(c["id"]))
        except GitHubError as e:
            print(f"warning: comment ids not read: {e}", file=sys.stderr)

    def record(rf: ReportFinding, a: Anchor | None) -> LogRecord:
        comment_id = None
        if a is not None:
            got = ids.get((a.path, a.line, comment_body(rf)))
            comment_id = got.pop(0) if got else None
        f = rf.finding
        return LogRecord(
            repo=repo,
            pr=pr,
            head_sha=head_sha,
            file=f.file,
            start_line=f.lines.start,
            end_line=f.lines.end,
            category=f.category,
            claim=f.claim,
            arm=report.arm,
            model=report.model,
            prompt_version=report.prompt_version,
            stage_versions=report.stage_versions,
            grade_score=rf.score,
            posted="body" if a is None else "inline",
            review_id=review_id,
            comment_id=comment_id,
            comment_line=None if a is None else a.line,
        )

    records = [record(rf, a) for rf, a in inline] + [record(rf, None) for rf in outside]
    return Posted(records, review_id)


def write_log(path: Path, records: Sequence[LogRecord]) -> None:
    """Write the findings log, one JSON record per line. Empty file when none."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8"
    )
