import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pr_review.make_case import (
    Hunk,
    hunk_range,
    is_reviewed_file,
    main,
    parse_hunks,
    strip_added_comments,
    strip_comment,
    truth_bugs,
)
from pr_review.models import LineRange

DIFF = """\
diff --git a/a.go b/a.go
--- a/a.go
+++ b/a.go
@@ -3,0 +4,2 @@ func f() {
+	x := 1
+	y := 2
@@ -10,3 +11,0 @@ func g() {
-	if p == nil {
-		return
-	}
diff --git a/new.go b/new.go
deleted file mode 100644
--- a/new.go
+++ /dev/null
@@ -1,2 +0,0 @@
-package a
-
"""


def test_parse_hunks_reads_buggy_side_ranges() -> None:
    hunks = parse_hunks(DIFF)
    assert [(h.file, h.new_start, h.new_count) for h in hunks] == [
        ("a.go", 4, 2),
        ("a.go", 11, 0),
    ]
    assert hunks[1].text.startswith("-\tif p == nil {")


def test_hunk_range_anchors_pure_deletions_to_the_gap() -> None:
    added = Hunk("a.go", 4, 2, "")
    deleted = Hunk("a.go", 11, 0, "")
    assert hunk_range(added, 50) == LineRange(start=4, end=5)
    assert hunk_range(deleted, 50) == LineRange(start=11, end=12)
    assert hunk_range(deleted, 11) == LineRange(start=11, end=11)
    assert hunk_range(Hunk("a.go", 0, 0, ""), 5) == LineRange(start=1, end=1)


def test_truth_bugs_merges_close_hunks_only() -> None:
    hunks = [
        Hunk("a.go", 10, 1, ""),
        Hunk("a.go", 16, 1, ""),  # 6 lines after: one location
        Hunk("a.go", 40, 2, ""),  # far: its own bug
        Hunk("b.go", 10, 1, ""),
    ]
    bugs = truth_bugs(hunks, [0, 1, 2, 3], {"a.go": 100, "b.go": 100})
    got = [(b.file, b.lines.start, b.lines.end) for b in bugs]
    assert got == [("a.go", 10, 16), ("a.go", 40, 41), ("b.go", 10, 10)]


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("\t// avoid uint16 overflow", None),
        ("\tsz := int(a) + int(b) // avoid overflow", "\tsz := int(a) + int(b)"),
        ('\tu := "http://x" // note', '\tu := "http://x"'),
        ('\ts := "a // b"', '\ts := "a // b"'),
        ("\tr := '/' // c", "\tr := '/'"),
        ("\tq := `//raw` + x", "\tq := `//raw` + x"),
        ('\te := "\\"" // c', '\te := "\\""'),
        ("//go:build linux", "//go:build linux"),
        ("\tx := 1", "\tx := 1"),
    ],
)
def test_strip_comment(line: str, want: str | None) -> None:
    assert strip_comment(line) == want


def test_strip_added_comments_touches_only_added_lines() -> None:
    src = b"a := 1 // old\n// added note\nb := 2 // added why\n"
    assert strip_added_comments(src, {2, 3}) == b"a := 1 // old\nb := 2\n"


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("a.go", True),
        ("pkg/a.go", True),
        ("a_test.go", False),
        ("testdata/fix.go", False),
        ("pkg/testdata/x/fix.go", False),
        ("vendor/x/y.go", False),
        ("CHANGELOG.md", False),
        ("go.mod", False),
    ],
)
def test_is_reviewed_file(path: str, want: bool) -> None:
    assert is_reviewed_file(path) is want


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose last commit adds a nil check, a test, and an unrelated edit."""
    r = tmp_path / "src"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    body = "\n".join(f"var v{i} = {i}" for i in range(1, 31))
    (r / "a.go").write_text(
        f"package a\n\nfunc f(p *int) int {{\n\treturn *p\n}}\n{body}\n"
    )
    git(r, "add", ".")
    git(r, "commit", "-qm", "buggy")
    fixed = (
        "package a\n\nfunc f(p *int) int {\n"
        "\t// guard against a nil pointer\n"
        "\tif p == nil { // nil means zero\n\t\treturn 0\n\t}\n"
        f"\treturn *p\n}}\n{body.replace('v25 = 25', 'v25 = 2500')}\n"
    )
    (r / "a.go").write_text(fixed)
    (r / "a_test.go").write_text("package a\n\n// TestNilPanics\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "fix nil deref")
    return r


def test_main_writes_case_with_noise_and_no_tests(repo: Path, tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    argv = ["--repo-dir", str(repo), "--fix", "HEAD", "--id", "t-1",
            "--repo", "example.com/t", "--licence", "MIT", "--bug-class", "nil_deref",
            "--cases-dir", str(cases), "--truth", "0"]  # fmt: skip
    assert main(argv) == 0

    case = json.loads((cases / "manifest.jsonl").read_text())
    assert case["base_ref"] == git(repo, "rev-parse", "HEAD")
    # Fix added lines 4-7 (comment stripped); the revert deletes them,
    # anchored at lines 3-4 of the buggy file.
    assert case["bugs"] == [{"file": "a.go", "lines": {"start": 3, "end": 4}}]

    diff = (cases / "t-1" / "buggy.diff").read_text()
    assert "a_test.go" not in diff
    assert "-var v25 = 2500" in diff  # unrelated edit kept as noise
    assert "nil pointer" not in diff and "nil means" not in diff
    base = (cases / "t-1" / "base" / "a.go").read_text()
    assert "nil pointer" not in base and "nil means" not in base
    assert "\tif p == nil {\n" in base

    # Re-running replaces the line instead of appending a duplicate.
    assert main(argv) == 0
    assert len((cases / "manifest.jsonl").read_text().splitlines()) == 1


def test_main_rejects_bad_truth_index(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="out of range"):
        main(["--repo-dir", str(repo), "--fix", "HEAD", "--id", "t-1",
              "--repo", "r", "--licence", "MIT", "--bug-class", "nil_deref",
              "--cases-dir", str(tmp_path), "--truth", "9"])  # fmt: skip


def test_truth_uses_buggy_file_numbers_when_fix_inserts_above(tmp_path: Path) -> None:
    """Ground truth is numbered like the buggy file (base + diff), not the fixed one.

    The agent is asked for lines in the file after the diff is applied. Here
    the fix inserts 4 lines above the chosen hunk, so the two numberings differ
    by 4 and only the buggy-side one points at the reintroduced bug.
    """
    r = tmp_path / "src"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    pad = [f"var v{i} = {i}" for i in range(1, 16)]
    buggy = ["package a", "", "func f() {", "}", *pad, "func g(a, b int32) int64 {",
             "\treturn int64(a + b)", "}"]  # fmt: skip
    (r / "a.go").write_text("\n".join(buggy) + "\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "buggy")
    fixed = ["package a", "", "func f() {", "\tx := 0", "\t_ = x", "\tx++", "\t_ = x",
             "}", *pad, "func g(a, b int32) int64 {", "\treturn int64(a) + int64(b)",
             "}"]  # fmt: skip
    (r / "a.go").write_text("\n".join(fixed) + "\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "fix overflow")

    cases = tmp_path / "cases"
    assert main(["--repo-dir", str(r), "--fix", "HEAD", "--id", "o-1",
                 "--repo", "example.com/o", "--licence", "MIT",
                 "--bug-class", "overflow_off_by_one", "--cases-dir", str(cases),
                 "--truth", "1"]) == 0  # fmt: skip

    buggy_line = buggy.index("\treturn int64(a + b)") + 1  # 21
    fixed_line = fixed.index("\treturn int64(a) + int64(b)") + 1  # 25
    assert fixed_line - buggy_line == 4
    case = json.loads((cases / "manifest.jsonl").read_text())
    assert case["bugs"] == [
        {"file": "a.go", "lines": {"start": buggy_line, "end": buggy_line}}
    ]

    # And that line of base + buggy.diff is the reintroduced bug.
    tree = tmp_path / "tree"
    shutil.copytree(cases / "o-1" / "base", tree)
    git(tree, "init", "-q")
    subprocess.run(["git", "apply", str(cases / "o-1" / "buggy.diff")],
                   cwd=tree, check=True)  # fmt: skip
    applied = (tree / "a.go").read_text().splitlines()
    assert applied[buggy_line - 1] == "\treturn int64(a + b)"


def clean_repo(tmp_path: Path, later_edit: str | None) -> tuple[Path, str]:
    """A repo where commit C adds `func h` to a.go, followed by 3 later commits.

    The later commits edit the far end of the file, except that `later_edit`
    ("change" or "insert") makes the second one touch C's lines.
    """
    r = tmp_path / "src"
    r.mkdir()
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    lines = ["package a", "", *[f"var v{i} = {i}" for i in range(1, 31)]]
    (r / "a.go").write_text("\n".join(lines) + "\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "base")
    h = ["func h() int {", "\treturn 1", "}"]
    lines[10:10] = h  # lines 11-13
    (r / "a.go").write_text("\n".join(lines) + "\n")
    (r / "a_test.go").write_text("package a\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "add h")
    c = git(r, "rev-parse", "HEAD")
    for n in range(3):
        if n == 1 and later_edit == "change":
            lines[11] = "\treturn 2"
        elif n == 1 and later_edit == "insert":
            lines[11:11] = ["\tprintln()"]
        else:
            lines[-1] = f"var v30 = {100 + n}"
        (r / "a.go").write_text("\n".join(lines) + "\n")
        git(r, "commit", "-qam", f"later {n}")
    return r, c


def clean_argv(repo: Path, commit: str, cases: Path) -> list[str]:
    return ["--clean", "--repo-dir", str(repo), "--fix", commit, "--id", "c-1",
            "--repo", "example.com/c", "--licence", "MIT", "--later", "3",
            "--cases-dir", str(cases)]  # fmt: skip


def test_main_writes_clean_case_from_the_commit(tmp_path: Path) -> None:
    repo, commit = clean_repo(tmp_path, None)
    cases = tmp_path / "cases"
    assert main(clean_argv(repo, commit, cases)) == 0

    case = json.loads((cases / "manifest.jsonl").read_text())
    assert case["kind"] == "clean"
    assert case["bugs"] == [] and case["bug_class"] is None
    assert case["base_ref"] == git(repo, "rev-parse", f"{commit}^")
    assert case["provenance"]["fix_commit"] == commit
    assert case["provenance"]["clean_check"] == {
        "later_commits": 3,
        "checked_at": git(repo, "rev-parse", "HEAD"),
    }
    assert case["diff_path"] == f"{cases.as_posix()}/c-1/change.diff"

    # The diff is the commit forward (adds h), without the test file, and it
    # applies to base/ (the parent).
    diff = (cases / "c-1" / "change.diff").read_text()
    assert "+func h() int {" in diff and "a_test.go" not in diff
    tree = tmp_path / "tree"
    shutil.copytree(cases / "c-1" / "base", tree)
    git(tree, "init", "-q")
    subprocess.run(["git", "apply", str(cases / "c-1" / "change.diff")],
                   cwd=tree, check=True)  # fmt: skip
    assert "func h() int {" in (tree / "a.go").read_text()


@pytest.mark.parametrize(
    ("edit", "why"),
    [
        ("change", "1 of 3 added lines changed later"),
        ("insert", "edited within 3 lines"),
    ],
)
def test_clean_rejects_commit_whose_lines_a_later_commit_touched(
    tmp_path: Path, edit: str, why: str
) -> None:
    repo, commit = clean_repo(tmp_path, edit)
    with pytest.raises(SystemExit, match=why):
        main(clean_argv(repo, commit, tmp_path / "cases"))
    assert not (tmp_path / "cases" / "manifest.jsonl").exists()


def test_clean_rejects_an_insertion_that_arrived_through_a_merge(
    tmp_path: Path,
) -> None:
    # The insertion is written on a branch, so blame credits the branch
    # commit, which is not on the first-parent line the window counts from.
    repo, commit = clean_repo(tmp_path, None)
    main_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    git(repo, "checkout", "-qb", "side", commit)
    lines = (repo / "a.go").read_text().splitlines()
    lines[11:11] = ["\tprintln()"]
    (repo / "a.go").write_text("\n".join(lines) + "\n")
    git(repo, "commit", "-qam", "side edit")
    git(repo, "checkout", "-q", main_branch)
    git(repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    argv = clean_argv(repo, commit, tmp_path / "cases")
    argv[argv.index("--later") + 1] = "4"  # 3 later commits, then the merge
    with pytest.raises(SystemExit, match="edited within 3 lines"):
        main(argv)


def test_clean_needs_enough_later_commits(tmp_path: Path) -> None:
    repo, commit = clean_repo(tmp_path, None)
    argv = clean_argv(repo, commit, tmp_path / "cases")
    argv[argv.index("--later") + 1] = "4"
    with pytest.raises(SystemExit, match="only 3 commits after"):
        main(argv)


def test_clean_takes_no_bug_class(tmp_path: Path) -> None:
    repo, commit = clean_repo(tmp_path, None)
    with pytest.raises(SystemExit):
        main(
            [*clean_argv(repo, commit, tmp_path / "cases"), "--bug-class", "nil_deref"]
        )
