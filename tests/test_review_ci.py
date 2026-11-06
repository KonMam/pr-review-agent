"""Item 8: `pr-review review` and `pr-review post`, offline.

The model is the stub OpenAI-compatible server and GitHub is a stub REST
server, both on loopback, so nothing here touches the network.
"""

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from stub_server import stub_server
from test_eval_stub import ARMS_TOML, BASE_GO, DIFF, HIT, MISS, findings_reply

from pr_review.cli import main
from pr_review.generate import changed_base_paths
from pr_review.github import (
    MARKER,
    LogRecord,
    commentable_lines,
)
from pr_review.review import Report, go_only
from pr_review.stages import SUPPRESS_VERSION

TOKEN = "ghs_stub_token_never_printed"
HEAD_SHA = "a" * 40

# A git-style diff: one Go file (as in test_eval_stub's DIFF) and a README.
GIT_DIFF = (
    "diff --git a/README.md b/README.md\n"
    "--- a/README.md\n"
    "+++ b/README.md\n"
    "@@ -1 +1,2 @@\n"
    " # store\n"
    "+More docs.\n"
    "diff --git a/store/load.go b/store/load.go\n"
    "index 1111111..2222222 100644\n" + DIFF
)

STYLE = {
    **HIT,
    "start_line": 7,
    "end_line": 7,
    "category": "style",
    "claim": "rename f",
}
DUP = {**HIT, "claim": "the same bug, said again", "evidence": ""}


@pytest.fixture
def base(tmp_path: Path) -> Path:
    root = tmp_path / "base"
    (root / "store").mkdir(parents=True)
    (root / "store" / "load.go").write_text(BASE_GO)
    (root / "README.md").write_text("# store\n")
    return root


@pytest.fixture
def arms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Any]]:
    monkeypatch.setenv("STUB_KEY_A", "stub-a")
    replies: list[str] = []

    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return replies.pop(0) if replies else findings_reply(), {}

    with stub_server(respond) as stub:
        path = tmp_path / "arms.toml"
        path.write_text(ARMS_TOML.format(url=stub.base_url))
        stub.replies = replies  # type: ignore[attr-defined]
        yield path, stub


def review_args(arms: Path, diff: Path, base: Path, *extra: str) -> list[str]:
    return [
        "review",
        "--diff",
        str(diff),
        "--base-dir",
        str(base),
        "--arms-file",
        str(arms),
        "--arm",
        "cheap",
        *extra,
    ]


# --- diff handling -----------------------------------------------------------


def test_go_only_keeps_go_blocks() -> None:
    kept, n = go_only(GIT_DIFF)
    assert n == 1
    assert "README" not in kept and "store/load.go" in kept
    assert go_only(DIFF) == (DIFF, 1)  # a bare patch is one block
    readme_only = GIT_DIFF.split("diff --git a/store")[0]
    assert go_only(readme_only) == ("", 0)


def test_commentable_lines_are_added_and_context_lines_per_hunk() -> None:
    lines = commentable_lines(GIT_DIFF)
    assert lines["README.md"] == [{1, 2}]
    assert lines["store/load.go"] == [{4, 5, 6, 7, 8, 9}]
    two_files = DIFF + DIFF.replace("store/load.go", "store/other.go")
    got = commentable_lines(two_files)
    # The second file's `---`/`+++` headers are not read as lines of the first.
    assert got["store/load.go"] == [{4, 5, 6, 7, 8, 9}]
    assert got["store/other.go"] == [{4, 5, 6, 7, 8, 9}]


def test_commentable_lines_keep_hunks_apart_and_skip_deleted_files() -> None:
    diff = (
        "--- a/x.go\n+++ b/x.go\n"
        "@@ -1,2 +1,2 @@\n a\n-b\n+B\n"
        "@@ -20,1 +20,2 @@\n c\n+d\n"
        "--- a/gone.go\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    assert commentable_lines(diff) == {"x.go": [{1, 2}, {20, 21}]}


# --- pr-review review ----------------------------------------------------------


def test_review_runs_default_stages_and_prints_json(
    arms: tuple[Path, Any],
    base: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arms_path, stub = arms
    stub.replies.append(findings_reply(HIT, DUP, STYLE))
    diff = tmp_path / "pr.diff"
    diff.write_text(GIT_DIFF)
    out = tmp_path / "report.json"
    assert main(review_args(arms_path, diff, base, "--out", str(out))) == 0
    report = Report.model_validate_json(capsys.readouterr().out)
    assert report == Report.model_validate_json(out.read_text())
    # dedup collapsed HIT and DUP; suppress dropped the style finding.
    assert [rf.finding.claim for rf in report.findings] == [HIT["claim"]]
    assert report.model == "vendor/cheap-model" and report.arm == "cheap"
    assert set(report.stage_versions) == {"generate", "dedup", "suppress"}
    assert report.stage_versions["suppress"] == SUPPRESS_VERSION
    assert report.go_files == 1 and report.cost_usd > 0
    # The model saw the Go diff and the Go base file, not the README.
    (request,) = stub.requests
    prompt = request["messages"][1]["content"]
    assert "store/load.go" in prompt and "README" not in prompt


def test_review_of_a_diff_without_go_costs_nothing(
    arms: tuple[Path, Any],
    base: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arms_path, stub = arms
    diff = tmp_path / "pr.diff"
    diff.write_text(GIT_DIFF.split("diff --git a/store")[0])
    assert main(review_args(arms_path, diff, base)) == 0
    report = Report.model_validate_json(capsys.readouterr().out)
    assert report.findings == [] and report.cost_usd == 0 and report.go_files == 0
    assert stub.requests == []


def test_review_budget_is_hard(
    arms: tuple[Path, Any],
    base: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arms_path, stub = arms
    diff = tmp_path / "pr.diff"
    diff.write_text(GIT_DIFF)
    # The `tight` arm's worst case is $0.10 a call: a $0.05 budget refuses it.
    args = review_args(arms_path, diff, base, "--budget-usd", "0.05")
    args[args.index("cheap")] = "tight"
    assert main(args) == 3
    assert "budget" in capsys.readouterr().err
    assert stub.requests == []


def test_review_with_tools_reads_the_base_tree(
    arms: tuple[Path, Any],
    base: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arms_path, stub = arms
    diff = tmp_path / "pr.diff"
    diff.write_text(GIT_DIFF)
    assert main(review_args(arms_path, diff, base, "--tools")) == 0
    report = Report.model_validate_json(capsys.readouterr().out)
    assert report.stage_versions["generate"].startswith("tools-")
    assert stub.requests[0]["tools"]


# --- pr-review post --------------------------------------------------------------


@dataclass
class GitHubStub:
    api_url: str = ""
    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    auth: list[str] = field(default_factory=list)
    fail_post: int = 0  # answer review creation with this HTTP status
    fail_get: int = 0  # answer the comment-id lookup with this HTTP status


@contextmanager
def github_stub() -> Iterator[GitHubStub]:
    gh = GitHubStub()
    reviews: dict[int, list[dict[str, Any]]] = {}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: Any) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            gh.calls.append(("POST", self.path, body))
            gh.auth.append(self.headers.get("Authorization", ""))
            if gh.fail_post:
                self._send(gh.fail_post, {"message": "Unprocessable"})
                return
            review_id = 100 + len(reviews)
            reviews[review_id] = [
                {**c, "id": 1000 * review_id + i, "original_line": c["line"]}
                for i, c in enumerate(body["comments"])
            ]
            self._send(200, {"id": review_id})

        def do_GET(self) -> None:
            gh.calls.append(("GET", self.path, None))
            gh.auth.append(self.headers.get("Authorization", ""))
            if gh.fail_get:
                self._send(gh.fail_get, {"message": "Bad gateway"})
                return
            review_id = int(self.path.split("/reviews/")[1].split("/")[0])
            page = int(self.path.split("page=")[-1])
            # Newest first, as an unordered API might: matching must not rely on order.
            comments = list(reversed(reviews[review_id]))
            self._send(200, comments[100 * (page - 1) : 100 * page])

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    gh.api_url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield gh
    finally:
        server.shutdown()
        server.server_close()


def write_report(
    path: Path, *findings: dict[str, Any], score: float | None = None
) -> None:
    report = {
        "arm": "or-deepseek-flash",
        "model": "deepseek/deepseek-v4.1-flash",
        "prompt_version": "abc123def456",
        "stage_versions": {"generate": "abc123def456", "dedup": "dedup-1"},
        "findings": [
            {
                "finding": {
                    "file": f["file"],
                    "lines": {"start": f["start_line"], "end": f["end_line"]},
                    "category": f["category"],
                    "claim": f["claim"],
                    "evidence": f["evidence"],
                },
                "score": score,
            }
            for f in findings
        ],
        "go_files": 1,
        "cost_usd": 0.004,
    }
    path.write_text(json.dumps(report))


def post_args(tmp_path: Path, api_url: str) -> list[str]:
    diff = tmp_path / "pr.diff"
    diff.write_text(GIT_DIFF)
    return [
        "post",
        "--report",
        str(tmp_path / "report.json"),
        "--diff",
        str(diff),
        "--repo",
        "KonMam/go-taskqueue",
        "--pr",
        "7",
        "--head-sha",
        HEAD_SHA,
        "--log",
        str(tmp_path / "out" / "findings.jsonl"),
        "--api-url",
        api_url,
    ]


def read_log(tmp_path: Path) -> list[LogRecord]:
    text = (tmp_path / "out" / "findings.jsonl").read_text()
    return [LogRecord.model_validate_json(line) for line in text.splitlines()]


def test_post_makes_one_review_with_inline_comments_and_logs_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    ranged = {**HIT, "start_line": 2, "end_line": 7, "claim": "range claim"}
    write_report(tmp_path / "report.json", HIT, ranged, MISS, STYLE, score=0.9)
    with github_stub() as gh:
        assert main(post_args(tmp_path, gh.api_url)) == 0
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert gh.auth and all(a == f"Bearer {TOKEN}" for a in gh.auth)

    (method, path, body), get = gh.calls[0], gh.calls[1]
    assert (method, path) == ("POST", "/repos/KonMam/go-taskqueue/pulls/7/reviews")
    assert get[0] == "GET" and len(gh.calls) == 2
    assert body["event"] == "COMMENT" and body["commit_id"] == HEAD_SHA
    one, multi = body["comments"]
    assert one == {
        "path": "store/load.go",
        "line": 6,
        "side": "RIGHT",
        "body": f"**ignored_err**: {HIT['claim']}\n\n{MARKER}",
    }
    # Lines 2-7 narrowed to the hunk's 4-9 overlap: a multi-line comment on 4-7.
    assert multi["start_line"] == 4 and multi["line"] == 7
    assert multi["start_side"] == "RIGHT"
    # The out-of-diff finding is in the body; the style finding is nowhere.
    assert "store/load.go:40-41" in body["body"] and "unrelated" in body["body"]
    assert "rename f" not in json.dumps(body)

    records = read_log(tmp_path)
    assert [(r.claim, r.posted) for r in records] == [
        (HIT["claim"], "inline"),
        ("range claim", "inline"),
        ("unrelated", "body"),
    ]
    first = records[0]
    assert first.repo == "KonMam/go-taskqueue" and first.pr == 7
    assert first.head_sha == HEAD_SHA and first.file == "store/load.go"
    assert (first.start_line, first.end_line, first.category) == (6, 6, "ignored_err")
    assert first.model == "deepseek/deepseek-v4.1-flash"
    assert first.arm == "or-deepseek-flash"
    assert first.prompt_version == "abc123def456"
    assert first.stage_versions == {"generate": "abc123def456", "dedup": "dedup-1"}
    assert first.grade_score == 0.9
    assert first.review_id == 100 and first.comment_line == 6
    assert [r.comment_id for r in records] == [100000, 100001, None]
    # Every line of the log is exactly one record with these keys.
    raw = json.loads((tmp_path / "out" / "findings.jsonl").read_text().splitlines()[0])
    assert set(raw) == set(LogRecord.model_fields)


def test_post_with_no_findings_posts_nothing_and_writes_an_empty_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    write_report(tmp_path / "report.json", STYLE)  # suppressed, so none left
    with github_stub() as gh:
        assert main(post_args(tmp_path, gh.api_url)) == 0
    assert gh.calls == []
    assert (tmp_path / "out" / "findings.jsonl").read_text() == ""


def test_post_failure_is_reported_without_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    write_report(tmp_path / "report.json", HIT)
    with github_stub() as gh:
        gh.fail_post = 422
        assert main(post_args(tmp_path, gh.api_url)) == 3
    out = capsys.readouterr()
    assert "HTTP 422" in out.err and TOKEN not in out.out + out.err
    assert read_log(tmp_path) == []


def test_post_needs_a_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    write_report(tmp_path / "report.json", HIT)
    assert main(post_args(tmp_path, "http://127.0.0.1:9")) == 2


def test_post_keeps_the_log_when_comment_ids_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    write_report(tmp_path / "report.json", HIT)
    with github_stub() as gh:
        gh.fail_get = 502
        assert main(post_args(tmp_path, gh.api_url)) == 0
    assert "comment ids not read" in capsys.readouterr().err
    (record,) = read_log(tmp_path)
    assert record.posted == "inline" and record.review_id == 100
    assert record.comment_id is None


def test_post_reads_comment_ids_across_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    many = [{**HIT, "claim": f"claim {i}"} for i in range(150)]
    write_report(tmp_path / "report.json", *many)
    with github_stub() as gh:
        assert main(post_args(tmp_path, gh.api_url)) == 0
    gets = [path for method, path, _ in gh.calls if method == "GET"]
    assert len(gets) == 2 and gets[1].endswith("page=2")
    records = read_log(tmp_path)
    assert [r.comment_id for r in records] == [100000 + i for i in range(150)]


def test_changed_base_paths_skips_hunk_lines_that_look_like_headers() -> None:
    diff = (
        "diff --git a/db/m.go b/db/m.go\n"
        "--- a/db/m.go\n+++ b/db/m.go\n"
        "@@ -1,3 +1,2 @@\n const q = `\n--- drop old table\n SELECT 1`\n"
        "--- a/db/n.go\n+++ b/db/n.go\n@@ -1 +1 @@\n-a\n+b\n"
    )
    assert changed_base_paths(diff) == ["db/m.go", "db/n.go"]
