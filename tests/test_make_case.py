import json
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
