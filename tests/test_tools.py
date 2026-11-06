"""The read-only tools ablation: sandboxed tools, snapshots, and the tool loop on a stub server.

No network: the stub listens on loopback and snapshots come from a local git
repo made in a temp dir.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from stub_server import stub_server
from test_eval_stub import BASE_GO, HIT, eval_args, findings_reply, write_arms

from pr_review.arms import Arm
from pr_review.cli import main
from pr_review.generate import ReviewInput
from pr_review.llm import Budget, make_client
from pr_review.models import Case
from pr_review.snapshot import build_snapshot, clone_dir, keep
from pr_review.tools import (
    MAX_GREP_MATCHES,
    MAX_READ_LINES,
    RepoTools,
    generate_with_tools,
    tools_version,
)


@pytest.fixture
def repo(tmp_path: Path) -> RepoTools:
    root = tmp_path / "snap"
    (root / "store").mkdir(parents=True)
    (root / "store" / "load.go").write_text(
        "\n".join(f"line {n}" for n in range(1, 501)) + "\n"
    )
    (root / "store" / "util.go").write_text("package store\n\nfunc readAll() {}\n")
    (tmp_path / "secret.go").write_text("outside\n")
    return RepoTools(root)


def test_read_file_numbers_lines_and_caps(repo: RepoTools) -> None:
    out = repo.read_file("store/util.go", 3, 3)
    assert out == "3: func readAll() {}"
    big = repo.read_file("store/load.go")
    assert big.count("\n") == MAX_READ_LINES  # capped lines plus the total note
    assert big.startswith("1: line 1") and "[500 lines in total]" in big


def test_read_file_stays_inside_snapshot(repo: RepoTools) -> None:
    assert repo.read_file("../secret.go").startswith("error: no file")
    assert repo.read_file("/store/util.go").startswith("1: package store")
    assert repo.read_file("store/missing.go").startswith("error: no file")
    assert repo.read_file("store/util.go", 99).startswith("error:")


def test_grep_matches_globs_and_caps(repo: RepoTools) -> None:
    assert repo.grep(r"func readAll") == "store/util.go:3: func readAll() {}"
    assert repo.grep("line", "store/util.go") == "no matches"
    many = repo.grep(r"^line \d+$")
    assert many.count("\n") == MAX_GREP_MATCHES and "[more matches cut]" in many
    assert repo.grep("(").startswith("error: bad regex")


def test_call_reports_bad_arguments_instead_of_raising(repo: RepoTools) -> None:
    assert repo.call("read_file", "not json").startswith("error: bad arguments")
    assert repo.call("read_file", "{}").startswith("error: bad arguments")
    assert repo.call("rm", "{}") == "error: unknown tool 'rm'"
    inf = repo.call("read_file", '{"path": "store/util.go", "start_line": Infinity}')
    assert inf.startswith("error: bad arguments")
    assert repo.call("read_file", json.dumps({"path": "x" * 300 + ".go"})).startswith(
        "error:"
    )
    assert repo.call("grep", json.dumps({"pattern": "readAll"})).startswith(
        "store/util.go:3"
    )


def test_digest_changes_with_content(repo: RepoTools) -> None:
    before = repo.digest()
    (repo.root / "store" / "util.go").write_text("package store\n")
    assert RepoTools(repo.root).digest() != before


def test_tools_version_depends_on_turns() -> None:
    assert tools_version(4) != tools_version(2)


# --- Snapshots -------------------------------------------------------------


def test_keep_only_non_test_go_outside_vendor() -> None:
    assert keep("a/b.go")
    assert not keep("a/b_test.go")
    assert not keep("vendor/x/y.go")
    assert not keep("a/testdata/z.go")
    assert not keep("README.md")


def test_clone_dir_follows_build_sh(tmp_path: Path) -> None:
    assert clone_dir("github.com/IBM/sarama", tmp_path / "r", tmp_path / "own") == (
        tmp_path / "r" / "IBM_sarama"
    )
    assert clone_dir("github.com/KonMam/go-taskqueue", tmp_path, tmp_path / "own") == (
        tmp_path / "own" / "go-taskqueue"
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_build_snapshot_from_local_clone(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    (clone / "store").mkdir(parents=True)
    (clone / "vendor").mkdir()
    (clone / "store" / "load.go").write_text("// the fix: check err\npackage store\n")
    (clone / "store" / "load_test.go").write_text("package store // regression\n")
    (clone / "store" / "other.go").write_text("package store\n")
    (clone / "vendor" / "v.go").write_text("package v\n")
    (clone / "README.md").write_text("hi\n")
    _git(clone.parent, "init", "-q", str(clone))
    _git(clone, "add", ".")
    _git(
        clone,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-qm",
        "fix",
    )
    sha = _git(clone, "rev-parse", "HEAD")
    case_base = tmp_path / "case" / "base"
    (case_base / "store").mkdir(parents=True)
    (case_base / "store" / "load.go").write_text("package store\n")  # comment stripped
    case = Case.model_validate(
        {
            "id": "store-001",
            "provenance": {
                "repo": "example.com/store",
                "fix_commit": sha,
                "licence": "MIT",
            },
            "base_ref": sha,
            "diff_path": "case/buggy.diff",
            "bug_class": "ignored_err",
            "bugs": [{"file": "store/load.go", "lines": {"start": 1, "end": 1}}],
        }
    )
    out = tmp_path / "snaps" / "store-001"
    assert build_snapshot(case, clone, case_base, out) == 2
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*.go")) == [
        "store/load.go",
        "store/other.go",
    ]
    assert (out / "store" / "load.go").read_text() == "package store\n"
    # Rebuilding replaces it rather than merging.
    (out / "stale.go").write_text("x")
    build_snapshot(case, clone, case_base, out)
    assert not (out / "stale.go").exists()


# --- The tool loop against a stub server ------------------------------------


def _arm(url: str, **extra: Any) -> Arm:
    return Arm.model_validate(
        {
            "name": "stub",
            "base_url": url,
            "api_key_env": "STUB_KEY_A",
            "model": "vendor/cheap-model",
            "price": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
            **extra,
        }
    )


def _call(n: int, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"call_{n}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


REVIEW = ReviewInput(diff="--- a/store/util.go\n+++ b/store/util.go\n", base_files={})


def test_tool_loop_runs_tools_then_answers(
    repo: RepoTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_KEY_A", "k")
    replies = iter(
        [
            ("", {"tool_calls": [_call(1, "grep", {"pattern": "readAll"})]}),
            (findings_reply(HIT), {}),
        ]
    )

    with stub_server(lambda body: next(replies)) as stub:
        arm = _arm(stub.base_url)
        r = generate_with_tools(make_client(arm), arm, REVIEW, Budget(1.0), repo)

    assert [f.claim for f in r.findings] == [HIT["claim"]]
    assert (r.calls, r.tool_calls, r.malformed, r.attempts) == (2, 1, False, 1)
    first, second = stub.requests
    assert {t["function"]["name"] for t in first["tools"]} == {"read_file", "grep"}
    assert first["tool_choice"] == "auto"
    assert "response_format" not in first  # JSON mode is off with tools
    assert "read_file" in first["messages"][0]["content"]
    assistant, tool = second["messages"][-2:]
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert tool == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "store/util.go:3: func readAll() {}",
    }
    # Two calls at 1000 prompt + 200 completion tokens, priced by the arm.
    assert r.cost_usd == pytest.approx(2 * (1000 * 1.0 + 200 * 5.0) / 1e6)


def test_tool_loop_stops_at_turn_cap_and_forces_answer(
    repo: RepoTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_KEY_A", "k")

    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if body.get("tool_choice") == "none":
            return findings_reply(), {}
        n = len(body["messages"])
        return "", {"tool_calls": [_call(n, "read_file", {"path": "store/util.go"})]}

    with stub_server(respond) as stub:
        arm = _arm(stub.base_url)
        r = generate_with_tools(
            make_client(arm), arm, REVIEW, Budget(1.0), repo, max_turns=2
        )

    assert (r.calls, r.tool_calls, r.findings, r.malformed) == (3, 2, [], False)
    assert [q["tool_choice"] for q in stub.requests] == ["auto", "auto", "none"]
    assert stub.requests[-1]["messages"][-1]["content"].startswith("No more tool calls")


def test_tool_loop_retries_cut_off_answer_with_truncation_body(
    repo: RepoTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_KEY_A", "k")
    replies = iter([("", {"finish_reason": "length"}), (findings_reply(HIT), {})])

    with stub_server(lambda body: next(replies)) as stub:
        arm = _arm(
            stub.base_url,
            extra_body={"provider": {"data_collection": "deny"}},
            truncation_extra_body={"reasoning": {"enabled": False}},
        )
        r = generate_with_tools(make_client(arm), arm, REVIEW, Budget(1.0), repo)

    assert (r.calls, r.truncated, r.attempts, r.malformed) == (2, 1, 2, False)
    retry = stub.requests[1]
    assert retry["reasoning"] == {"enabled": False}
    assert retry["provider"] == {"data_collection": "deny"}
    assert retry["tool_choice"] == "none"
    assert retry["messages"] == stub.requests[0]["messages"]


def test_tool_loop_malformed_twice_is_reported(
    repo: RepoTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_KEY_A", "k")
    with stub_server(lambda body: ("not json", {})) as stub:
        arm = _arm(stub.base_url)
        r = generate_with_tools(make_client(arm), arm, REVIEW, Budget(1.0), repo)
    assert (r.malformed, r.findings, r.calls) == (True, [], 2)
    assert "Your reply was not valid" in stub.requests[1]["messages"][-1]["content"]


# --- `pr-review eval --tools` end to end -------------------------------------


def _snapshots(project: Path, load_go: str = "package store\n") -> Path:
    snaps = project / "snapshots"
    for case_id in ("store-001", "store-002"):
        d = snaps / case_id / "store"
        d.mkdir(parents=True)
        (d / "load.go").write_text(load_go)
        (d / "read.go").write_text("package store\n\nfunc readAll() {}\n")
    return snaps


def test_eval_with_tools_caches_and_summarises(project: Path) -> None:
    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if "tools" not in body or body["messages"][-1]["role"] == "tool":
            return findings_reply(HIT), {}
        return "", {"tool_calls": [_call(1, "grep", {"pattern": "func readAll"})]}

    snaps = _snapshots(project, BASE_GO)
    with stub_server(respond) as stub:
        arms = write_arms(project, stub.base_url)
        tools = ("--tools", "--snapshots-dir", str(snaps))

        def run(run_id: str, *extra: str) -> int:
            args = eval_args(project, arms, "cheap", *extra)
            args[args.index("--run-id") + 1] = run_id
            return main(args)

        assert run("cheap", *tools) == 0
        assert len(stub.requests) == 8  # 2 cases x 2 runs x 2 calls
        # Same config again: every review comes from the cache.
        assert run("cheap-again", *tools) == 0
        assert len(stub.requests) == 8
        # Single-pass on the same arm is a different stage, not a cache hit.
        assert run("single") == 0
        assert len(stub.requests) == 12

    # Reviews run concurrently, so look at every request that carried a result.
    tool_msgs = [m for q in stub.requests for m in q["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 4
    assert {m["content"] for m in tool_msgs} == {"store/read.go:3: func readAll() {}"}
    summary = json.loads((project / "results" / "cheap" / "summary.json").read_text())
    assert summary["stage"].startswith("generate-tools-")
    assert summary["tools"] == {
        "max_turns": 4,
        "json_mode": False,
        "model_calls": 8,
        "tool_calls": 4,
        "reviews_using_tools": 4,
    }
    assert summary["recall"]["mean"] == 1.0
    single = json.loads((project / "results" / "single" / "summary.json").read_text())
    assert "tools" not in single and not single["stage"].startswith("generate-tools")


def test_eval_with_tools_needs_snapshots(project: Path) -> None:
    with stub_server(lambda body: (findings_reply(), {})) as stub:
        arms = write_arms(project, stub.base_url)
        args = eval_args(
            project, arms, "cheap", "--tools", "--snapshots-dir", str(project / "none")
        )
        assert main(args) == 2
    assert stub.requests == []


def test_eval_with_tools_refuses_stale_snapshot(project: Path) -> None:
    snaps = _snapshots(project)  # load.go here differs from the case's base/
    with stub_server(lambda body: (findings_reply(), {})) as stub:
        arms = write_arms(project, stub.base_url)
        args = eval_args(
            project, arms, "cheap", "--tools", "--snapshots-dir", str(snaps)
        )
        assert main(args) == 2
    assert stub.requests == []
