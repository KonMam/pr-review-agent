"""Post a review report to a GitHub PR as inline comments, and log each posted finding.

`pr-review post` reads `pr-review review`'s report and the same diff, and makes
one PR review (event COMMENT, never APPROVE or REQUEST_CHANGES) through the REST
API with the token in `GITHUB_TOKEN`. Comments are terse (item 8b): a badge
with the readable category, the claim's first sentence as a bold title, and the
rest of the claim as one short paragraph. The review body is one count line and
a footer with the model and cost. A finding whose lines aren't in the diff can't
be an inline comment, so it is written out in the review body instead. Suppressed categories are dropped here too,
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
from pr_review.stages import is_suppressed, normalise_category

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_SERVER_URL = "https://github.com"
MARKER = "<!-- pr-review -->"  # tags our reviews and comments
MAX_TITLE_CHARS = 160
MAX_WHY_CHARS = 400

# Readable names for the generate prompt's categories. Anything else (another
# arm's wording) is shown as words, so no snake_case id is ever posted.
CATEGORY_LABELS = {
    "nil_deref": "Nil dereference",
    "ignored_err": "Ignored error",
    "goroutine_leak": "Goroutine leak",
    "data_race": "Data race",
    "defer_in_loop": "Defer in loop",
    "slice_aliasing": "Slice aliasing",
    "context_propagation": "Context propagation",
    "overflow_off_by_one": "Overflow or off-by-one",
    "unclosed_resource": "Unclosed resource",
    "other_bug": "Other bug",
}

# A sentence ends at . ! or ? and a space; "e.g." and friends don't end one.
# Go claims often start a sentence with a lowercase identifier, so the next
# character can be anything.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=\S)")
_ABBREVIATION = re.compile(r"(?:^|[\s(])(?:e\.g|i\.e|etc|vs|cf)\.$", re.IGNORECASE)
# Where a too-long first sentence may be cut: after , ; : or around a dash.
_CLAUSE = re.compile(r"[,;:](?=\s)|\s(?=[—–-]\s)")
_MIN_TITLE_CHARS = 40  # a clause cut shorter than this reads as a fragment

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


def category_label(category: str) -> str:
    """The readable name of a category: `ignored_err` -> "Ignored error"."""
    key = normalise_category(category)
    if key in CATEGORY_LABELS:
        return CATEGORY_LABELS[key]
    words = key.replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Bug"


def _outside_code(text: str, i: int) -> bool:
    """Whether position `i` is outside every `code` span (an even count before it)."""
    return text.count("`", 0, i) % 2 == 0


def _md(text: str) -> str:
    """Escape `*` outside code spans and close a code span left open, so the
    model's text (`*Task`, `**`) can't break the bold title or the paragraph."""
    out = [
        part if n % 2 else part.replace("*", "\\*")
        for n, part in enumerate(text.split("`"))
    ]
    joined = "`".join(out)
    return joined + "`" if text.count("`") % 2 else joined


def _cut(text: str, limit: int) -> int:
    """Where to cut `text` to at most `limit` chars: the last clause break, else
    the last space, never inside a code span."""
    clauses = [
        m.end()
        for m in _CLAUSE.finditer(text, 0, limit)
        if m.end() >= _MIN_TITLE_CHARS and _outside_code(text, m.end())
    ]
    if clauses:
        return clauses[-1]
    spaces = [i for i in range(1, limit) if text[i] == " " and _outside_code(text, i)]
    return spaces[-1] if spaces else limit - 1


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: _cut(text, limit - 1)].rstrip(" ,;:—–-") + "…"


def title_and_why(claim: str) -> tuple[str, str]:
    """Split a claim into its first sentence (the title) and the rest (the why).

    The why is "" for a one-sentence claim. A first sentence too long for a
    title is cut at a clause break and its tail opens the why, so no text is
    lost from the middle; only the why's end is clipped. Both are markdown-safe.
    """
    text = " ".join(claim.split())
    head, rest = text, ""
    for m in _SENTENCE_END.finditer(text):
        if not _ABBREVIATION.search(text[: m.start()]):
            head, rest = text[: m.start()], text[m.end() :]
            break
    semi = next(
        (
            m.start()
            for m in re.finditer(r";\s", head)
            if _MIN_TITLE_CHARS <= m.start() < MAX_TITLE_CHARS
            and _outside_code(head, m.start())
        ),
        None,
    )
    if len(head) > MAX_TITLE_CHARS and semi is not None:
        # "X is ignored; if it fails ..." reads as two sentences: split there.
        tail = head[semi + 1 :].strip()
        head = head[:semi] + "."
        rest = tail[:1].upper() + tail[1:] + (" " + rest if rest else "")
    elif len(head) > MAX_TITLE_CHARS:
        i = _cut(head, MAX_TITLE_CHARS - 1)
        tail = head[i:].lstrip(" ,;:—–-")
        head = head[:i].rstrip(" ,;:—–-") + "…"
        rest = "…" + tail + (" " + rest if rest else "")
    return _md(head), _md(_clip(rest, MAX_WHY_CHARS))


def _finding_md(rf: ReportFinding, where: str = "") -> str:
    """Badge, bold title and why: the inline format, also used in the body."""
    title, why = title_and_why(rf.finding.claim)
    badge = f"🐛 **{category_label(rf.finding.category)}**"
    parts = [badge + (f" · {where}" if where else ""), f"**{title}**"]
    if why:
        parts.append(why)
    return "\n\n".join(parts)


def comment_body(rf: ReportFinding) -> str:
    return f"{_finding_md(rf)}\n\n{MARKER}"


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


def _bugs(n: int) -> str:
    return "1 possible bug" if n == 1 else f"{n} possible bugs"


def _link(rf: ReportFinding, *, server_url: str, repo: str, head_sha: str) -> str:
    """`file:start-end`, linked to those lines at the head commit."""
    f = rf.finding
    lines = (
        str(f.lines.start)
        if f.lines.start == f.lines.end
        else f"{f.lines.start}-{f.lines.end}"
    )
    anchor = (
        f"L{f.lines.start}"
        if f.lines.start == f.lines.end
        else f"L{f.lines.start}-L{f.lines.end}"
    )
    url = f"{server_url.rstrip('/')}/{repo}/blob/{head_sha}/{f.file}#{anchor}"
    return f"[`{f.file}:{lines}`]({url})"


def review_body(
    report: Report,
    n_inline: int,
    outside: Sequence[ReportFinding],
    *,
    server_url: str,
    repo: str,
    head_sha: str,
) -> str:
    """One count line, any findings that couldn't go inline, and a footer."""
    total = n_inline + len(outside)
    if not outside:
        count = f"{_bugs(total)}, commented inline"
    elif not n_inline:
        count = f"{_bugs(total)}, outside the diff"
    else:
        count = f"{_bugs(total)}: {n_inline} commented inline, {len(outside)} outside the diff"
    parts = [f"**pr-review** found {count}."]
    for rf in outside:
        link = _link(rf, server_url=server_url, repo=repo, head_sha=head_sha)
        parts.append(_finding_md(rf, link))
    parts += [f"<sub>{report.model} · ${report.cost_usd:.4f}</sub>", MARKER]
    return "\n\n".join(parts)


def post_report(
    gh: GitHub,
    *,
    report: Report,
    diff: str,
    repo: str,
    pr: int,
    head_sha: str,
    server_url: str = DEFAULT_SERVER_URL,
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
    body = review_body(
        report,
        len(inline),
        outside,
        server_url=server_url,
        repo=repo,
        head_sha=head_sha,
    )
    review_id = gh.create_review(repo, pr, head_sha, body, comments)
    # Keyed by path and body: this endpoint returns `line: null` (seen on
    # GitHub, 2026-11-06), so the line can't be part of the match.
    ids: dict[tuple[str, str], list[int]] = {}
    if inline:
        # The review is posted: a failed id lookup must not lose the log, so
        # its records keep comment_id None (item 9 can match by review_id).
        try:
            for c in gh.review_comments(repo, pr, review_id):
                ids.setdefault((c["path"], c["body"]), []).append(int(c["id"]))
        except GitHubError as e:
            print(f"warning: comment ids not read: {e}", file=sys.stderr)

    def record(rf: ReportFinding, a: Anchor | None) -> LogRecord:
        comment_id = None
        if a is not None:
            got = ids.get((a.path, comment_body(rf)))
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
