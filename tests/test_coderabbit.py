"""CodeRabbit arm (item 6), offline: sandbox build + leakage checks, normaliser, scoring."""

import subprocess
from pathlib import Path

import pytest

from pr_review import sandbox
from pr_review.coderabbit import (
    BOT_LOGIN,
    Comment,
    badge,
    normalise_inline,
    normalise_pr,
    normalise_review_body,
    rate_limit_wait,
    review_state,
    score_arm,
    summarise,
)
from pr_review.models import Case, Finding, LineRange

SHA_A = "a" * 40
SHA_B = "b" * 40

BASE_GO = "package x\n\nfunc F() int {\n\treturn 1\n}\n"
DIFF = """\
diff --git a/x.go b/x.go
--- a/x.go
+++ b/x.go
@@ -1,5 +1,5 @@
 package x

 func F() int {
-\treturn 1
+\treturn 2
 }
"""


def _case(cid: str, sha: str, clean: bool) -> Case:
    data: dict[str, object] = {
        "id": cid,
        "kind": "clean" if clean else "bug",
        "provenance": {
            "repo": "github.com/acme/widget",
            "fix_commit": sha,
            "licence": "MIT",
            "clean_check": {"later_commits": 20, "checked_at": sha} if clean else None,
        },
        "base_ref": sha,
        "diff_path": f"cases/{cid}/{'change' if clean else 'buggy'}.diff",
        "bug_class": None if clean else "nil_deref",
        "bugs": [] if clean else [{"file": "x.go", "lines": {"start": 4, "end": 4}}],
    }
    return Case.model_validate(data)


@pytest.fixture
def built(tmp_path: Path) -> tuple[Path, list[Case], dict[str, int], Path]:
    """A two-case sandbox (one bug, one clean) built from fixture files."""
    root = tmp_path / "root"
    cases = [
        _case("widget-aaaaaaa", SHA_A, False),
        _case("widget-clean-bbbbbbb", SHA_B, True),
    ]
    snaps = tmp_path / "snaps"
    for c in cases:
        cdir = root / c.diff_path
        cdir.parent.mkdir(parents=True)
        cdir.write_text(DIFF)
        (cdir.parent / "base").mkdir()
        (cdir.parent / "base" / "x.go").write_text(BASE_GO)
        (snaps / c.id).mkdir(parents=True)
        (snaps / c.id / "x.go").write_text(BASE_GO)
        (snaps / c.id / "y.go").write_text("package x\n")
    lic = tmp_path / "lic" / "acme"
    lic.mkdir(parents=True)
    (lic / "widget.txt").write_text("MIT License\n")
    numbers = sandbox.assign_numbers([c.id for c in cases])
    git_dir = tmp_path / "sandbox.git"
    sandbox.build_sandbox(
        cases,
        numbers,
        snaps,
        root,
        tmp_path / "lic",
        sandbox.readme_text(cases),
        git_dir,
        tmp_path / "work",
    )
    return git_dir, cases, numbers, snaps


def _git(git_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(git_dir), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


# --- sandbox build and leakage ---------------------------------------------


def test_assign_numbers_is_a_seeded_permutation() -> None:
    ids = [f"c{i}" for i in range(20)]
    a = sandbox.assign_numbers(ids)
    assert sorted(a.values()) == list(range(1, 21))
    assert a == sandbox.assign_numbers(list(reversed(ids)))
    assert a != sandbox.assign_numbers(ids, seed="other")


def test_built_sandbox_has_no_leaks(
    built: tuple[Path, list[Case], dict[str, int], Path],
) -> None:
    git_dir, cases, numbers, _ = built
    assert sandbox.check_repo(git_dir, cases, numbers) == []
    for n in numbers.values():
        lab = sandbox.label(n)
        files = _git(git_dir, "ls-tree", "-r", "--name-only", f"base/{lab}").split()
        assert sorted(files) == [".coderabbit.yaml", "LICENSE", "x.go", "y.go"]
        assert "\treturn 2" in _git(git_dir, "show", f"case/{lab}:x.go")
        assert _git(git_dir, "log", "--format=%H", f"base/{lab}").count("\n") == 1


def test_bug_and_clean_cases_look_the_same(
    built: tuple[Path, list[Case], dict[str, int], Path],
) -> None:
    git_dir, _, numbers, _ = built
    fmt = "--format=%an|%ae|%aI|%cn|%ce|%cI"
    a, b = (sandbox.label(n) for n in numbers.values())
    for side in ("base", "case"):
        assert _git(git_dir, "log", "-1", fmt, f"{side}/{a}") == _git(
            git_dir, "log", "-1", fmt, f"{side}/{b}"
        )
    # Same tree content too (only the number in the message differs).
    assert _git(git_dir, "rev-parse", f"case/{a}^{{tree}}") == _git(
        git_dir, "rev-parse", f"case/{b}^{{tree}}"
    )


def test_test_file_in_snapshot_is_caught(
    built: tuple[Path, list[Case], dict[str, int], Path], tmp_path: Path
) -> None:
    git_dir, cases, numbers, snaps = built
    (snaps / cases[0].id / "x_test.go").write_text("package x\n")
    (snaps / cases[0].id / "testdata").mkdir()
    (snaps / cases[0].id / "testdata" / "f.go").write_text("package f\n")
    sandbox.build_sandbox(
        cases,
        numbers,
        snaps,
        tmp_path / "root",
        tmp_path / "lic",
        sandbox.readme_text(cases),
        git_dir,
        tmp_path / "work",
    )
    problems = sandbox.check_repo(git_dir, cases, numbers)
    assert any("test file x_test.go" in p for p in problems)
    assert any("testdata/f.go" in p for p in problems)


def test_history_is_caught(
    built: tuple[Path, list[Case], dict[str, int], Path],
) -> None:
    git_dir, cases, numbers, _ = built
    lab = sandbox.label(next(iter(numbers.values())))
    tree = _git(git_dir, "rev-parse", f"base/{lab}^{{tree}}").strip()
    upstream = _git(git_dir, "commit-tree", tree, "-m", "upstream history").strip()
    child = _git(
        git_dir, "commit-tree", tree, "-p", upstream, "-m", sandbox.base_message(1)
    ).strip()
    _git(git_dir, "update-ref", f"refs/heads/base/{lab}", child)
    problems = sandbox.check_repo(git_dir, cases, numbers)
    assert any(f"base/{lab}: 1 parents" in p for p in problems)
    assert any("neutral identity" in p for p in problems)


def test_names_and_messages_must_be_neutral(
    built: tuple[Path, list[Case], dict[str, int], Path],
) -> None:
    git_dir, cases, numbers, _ = built
    lab = sandbox.label(next(iter(numbers.values())))
    sha = _git(git_dir, "rev-parse", f"case/{lab}").strip()
    _git(git_dir, "update-ref", f"refs/heads/fix/{cases[0].id}", sha)
    problems = sandbox.check_repo(git_dir, cases, numbers)
    assert any("is not neutral" in p for p in problems)
    assert any(cases[0].id in p for p in problems)


def test_readme_must_not_name_a_case() -> None:
    cases = [_case("widget-aaaaaaa", SHA_A, False)]
    forbidden = sandbox.forbidden_strings(cases)
    assert sandbox.check_text("readme", sandbox.readme_text(cases), forbidden) == []
    assert sandbox.check_text("readme", "see widget-aaaaaaa", forbidden)
    assert sandbox.check_text("readme", "commit aaaaaaa", forbidden)


def test_check_paths() -> None:
    assert (
        sandbox.check_paths(["a.go", "pkg/b.go", "LICENSE", ".coderabbit.yaml"]) == []
    )
    assert sandbox.check_paths(["a_test.go"]) == ["test file a_test.go"]
    assert sandbox.check_paths(["vendor/x/y.go"]) == [
        "vendored or testdata file vendor/x/y.go"
    ]
    assert sandbox.check_paths(["CHANGELOG.md"]) == ["non-Go file CHANGELOG.md"]


def test_pr_text_must_match_template() -> None:
    assert sandbox.check_pr_text(17, "Case 017", sandbox.PR_BODY) == []
    assert sandbox.check_pr_text(17, "Fix nil deref", sandbox.PR_BODY)
    assert sandbox.check_pr_text(17, "Case 017", "Reverts the fix")
    with_summary = (
        sandbox.PR_BODY.strip()
        + "\n\n\n<!-- This is an auto-generated comment: release notes by coderabbit.ai -->"
        + "\n## Summary by CodeRabbit\n"
    )
    assert sandbox.check_pr_text(17, "Case 017", with_summary) == []
    assert sandbox.check_pr_text(17, "Case 017", "Fixes #3\n" + with_summary)


# --- normaliser ---------------------------------------------------------------
# Fixtures follow the comment format CodeRabbit posted on the sandbox (2026-11),
# trimmed: an HTML-icon badge line of severity · category · effort, then a
# bold title.

ICON = '<picture><img src="https://x/major-16.svg" width="16" alt=""></picture>'
BADGE = f"**{ICON} Major** · **Stability & Availability** · **Quick win**"
INLINE = {
    "id": 1,
    "user": {"login": BOT_LOGIN},
    "path": "x.go",
    "start_line": 3,
    "line": 5,
    "original_line": 5,
    "side": "RIGHT",
    "in_reply_to_id": None,
    "body": f"<!-- coderabbit-ai-generated:v1 -->\n\n{BADGE}\n\n"
    "**Keep the peer index non-negative.**\n\nDetails here.",
}


def test_inline_comment_becomes_a_finding_with_its_labels() -> None:
    c = normalise_inline(INLINE)
    assert c is not None and c.finding is not None
    assert (c.finding.file, c.finding.lines.start, c.finding.lines.end) == (
        "x.go",
        3,
        5,
    )
    assert (c.severity, c.category, c.tags) == (
        "Major",
        "Stability & Availability",
        ("Quick win",),
    )
    assert c.source == "inline"
    assert c.finding.claim == "Keep the peer index non-negative."
    assert c.finding.category == "Stability & Availability"
    assert c.serious


def test_inline_single_line_outdated_and_file_level() -> None:
    single = normalise_inline({**INLINE, "start_line": None})
    assert (
        single and single.finding and single.finding.lines == LineRange(start=5, end=5)
    )
    outdated = normalise_inline(
        {
            **INLINE,
            "line": None,
            "start_line": None,
            "original_start_line": 8,
            "original_line": 9,
        }
    )
    assert outdated and outdated.finding
    assert outdated.finding.lines == LineRange(start=8, end=9)
    file_level = normalise_inline(
        {**INLINE, "line": None, "start_line": None, "original_line": None}
    )
    assert file_level is not None and file_level.finding is None


def test_replies_and_other_users_are_not_findings() -> None:
    assert normalise_inline({**INLINE, "in_reply_to_id": 7}) is None
    assert normalise_inline({**INLINE, "user": {"login": "KonMam"}}) is None


def test_badge_variants() -> None:
    minor = f"**{ICON} Minor** · **Functional Correctness** · **Quick win**"
    assert badge(minor) == ("Minor", "Functional Correctness", ("Quick win",))
    tier = (
        f"**{ICON} Major** · **Security & Privacy** · "
        f"**{ICON} Detected with Advanced Tier** · **Quick win**"
    )
    assert badge(tier) == (
        "Major",
        "Security & Privacy",
        ("Detected with Advanced Tier", "Quick win"),
    )
    assert badge("**No badge here**") == ("", "", ())
    c = normalise_inline({**INLINE, "body": "**Plain comment**"})
    assert c is not None and c.severity == "" and not c.serious
    assert c.finding is not None and c.finding.category == "unlabelled"


REVIEW_BODY = f"""\
<!-- coderabbit-ai-generated:v1 -->

> [!CAUTION]
> Some comments are outside the diff and can’t be posted inline due to GitHub limitations.
>
> **⚠️ Outside diff range comments (1)**
>
> <details>
> <summary><strong>{ICON} Major</strong> · Return the error · <code>registry.go:355-370</code></summary><blockquote>
>
> `prometheus/registry.go:355-370`
> {BADGE}
>
> **Return the request construction error**
>
> Text.
>
> <details>
> <summary>Prompt for AI Agents</summary>
>
> ```
> Review comment at @prometheus/registry.go around lines 355 - 370:
> ```
>
> </details>
> </blockquote></details>

**Actionable comments posted: 1**

<details>
<summary>{ICON} Additional comments: 2</summary><blockquote>

<details>
<summary>context.go-1056-1057: 1</summary><blockquote>

`1056-1057`: **{ICON} Minor** · **Functional Correctness** · **Quick win**

{ICON} **Unverified finding**
Verification ran but could not confirm this finding.

**Preserve the client-facing scheme.**

</blockquote></details>
<details>
<summary>main.go-5-9: 1</summary><blockquote>

`5-9`: **LGTM!**

</blockquote></details>
</blockquote></details>

<details>
<summary>{ICON} Review info</summary>

- **Review profile**: CHILL

</details>
"""


def test_review_body_entries_become_findings() -> None:
    got = normalise_review_body(REVIEW_BODY)
    rows = [
        (
            c.source,
            c.severity,
            c.finding.file,
            c.finding.lines.start,
            c.finding.lines.end,
        )
        for c in got
        if c.finding
    ]
    assert rows == [
        ("Outside diff range comments", "Major", "prometheus/registry.go", 355, 370),
        ("Additional comments", "Minor", "context.go", 1056, 1057),
    ]
    assert got[0].finding is not None
    assert got[0].finding.claim == "Return the request construction error"
    assert got[1].tags == ("Quick win", "Unverified finding")
    assert got[1].finding is not None
    assert got[1].finding.claim == "Preserve the client-facing scheme."


def test_normalise_pr_takes_bot_reviews_only() -> None:
    reviews = [
        {"user": {"login": BOT_LOGIN}, "body": REVIEW_BODY},
        {"user": {"login": "KonMam"}, "body": REVIEW_BODY},
    ]
    got = normalise_pr([INLINE, {**INLINE, "in_reply_to_id": 1}], reviews)
    assert len(got) == 3


def test_rate_limit_wait() -> None:
    body = "Rate limit exceeded. Please wait **12 minutes and 5 seconds** before ..."
    assert rate_limit_wait(body) == 725
    assert rate_limit_wait("rate limit exceeded, please wait **40 seconds**") == 40
    assert rate_limit_wait("Rate limit hit") == 3600
    assert rate_limit_wait("Walkthrough ...") is None


# --- scoring ------------------------------------------------------------------


def _comment(file: str, start: int, severity: str) -> Comment:
    f = Finding(
        file=file,
        lines=LineRange(start=start, end=start),
        category="Functional Correctness",
        claim="c",
        evidence="",
    )
    return Comment(f, severity, "Functional Correctness", (), "inline")


def test_score_arm_all_vs_major_or_critical() -> None:
    bug = _case("widget-aaaaaaa", SHA_A, False)
    clean = _case("widget-clean-bbbbbbb", SHA_B, True)
    comments = {
        bug.id: [_comment("x.go", 5, "Major"), _comment("x.go", 40, "Minor")],
        clean.id: [_comment("x.go", 2, "Minor")],
    }
    s = summarise([bug, clean], comments, [60.0, 120.0])
    allc, serious = s["all_comments"], s["major_or_critical"]
    assert allc["combined"]["precision"] == pytest.approx(1 / 3)
    assert allc["bug_only"]["precision"] == pytest.approx(1 / 2)
    assert allc["combined"]["recall"] == 1.0
    assert allc["fp_per_clean_review"] == 1.0 and allc["clean_flagged"] == 1.0
    assert serious["combined"]["precision"] == 1.0
    assert serious["fp_per_clean_review"] == 0.0
    assert s["severity"] == {"Minor": 2, "Major": 1}
    assert s["latency_s"]["n"] == 2


def test_unlocated_comments_are_counted_not_scored() -> None:
    bug = _case("widget-aaaaaaa", SHA_A, False)
    s = score_arm(
        [bug], {bug.id: [Comment(None, "Major", "", (), "inline")]}, lambda c: True
    )
    assert s["unlocated_comments"] == 1
    assert s["combined"]["findings"] == 0


def _run(
    status: str, title: str, started: str = "2026-11-06T10:00:00Z"
) -> dict[str, object]:
    return {
        "name": "CodeRabbit",
        "status": status,
        "started_at": started,
        "completed_at": "2026-11-06T10:04:00Z" if status == "completed" else None,
        "output": {"title": title},
    }


def test_review_state_from_check_run_and_reviews() -> None:
    since = "2026-11-06T09:59:00Z"
    assert review_state([], [], since) == ("pending", None)
    assert review_state([_run("in_progress", "Review in progress")], [], since) == (
        "pending",
        None,
    )
    assert review_state([_run("completed", "Review rate limited")], [], since) == (
        "rate_limited",
        None,
    )
    # Nothing to say: no PR review, so the check run's completion is the time.
    assert review_state([_run("completed", "Finished")], [], since) == (
        "done",
        "2026-11-06T10:04:00Z",
    )
    review = {"user": {"login": BOT_LOGIN}, "submitted_at": "2026-11-06T10:03:30Z"}
    assert review_state([_run("completed", "Finished")], [review], since) == (
        "done",
        "2026-11-06T10:03:30Z",
    )
    # A run from before the request doesn't count.
    old = _run("completed", "Finished", started="2026-11-06T09:00:00Z")
    assert review_state([old], [], since) == ("pending", None)
