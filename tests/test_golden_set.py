"""The committed golden set: schema, balance, and that every diff applies, offline.

Everything this needs is under `cases/`: each case's diff (`buggy.diff`, or
`change.diff` for a clean case) and the files it touches at the base commit
(`base/`). No network, no source repos.
"""

import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from pr_review.make_case import is_reviewed_file, strip_comment
from pr_review.manifest import load_manifest
from pr_review.models import Case, CaseKind

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "cases"
CASES = load_manifest(CASES_DIR / "manifest.jsonl")
BUG_CASES = [c for c in CASES if c.kind is CaseKind.BUG]
CLEAN_CASES = [c for c in CASES if c.kind is CaseKind.CLEAN]


def test_size_is_30_to_50_bug_cases_and_15_to_20_clean() -> None:
    assert 30 <= len(BUG_CASES) <= 50
    assert 15 <= len(CLEAN_CASES) <= 20


def test_no_bug_class_is_more_than_half() -> None:
    cls, n = Counter(c.bug_class for c in BUG_CASES).most_common(1)[0]
    assert n <= len(BUG_CASES) / 2, f"{cls} is {n} of {len(BUG_CASES)} cases"


def test_every_case_is_go() -> None:
    for c in CASES:
        assert all(b.file.endswith(".go") for b in c.bugs), c.id


def test_base_ref_is_the_fix_commit() -> None:
    # The synthetic PR reverts the fix, so it applies on top of the fix commit.
    for c in BUG_CASES:
        assert c.base_ref == c.provenance.fix_commit, c.id


@pytest.mark.parametrize("case", CLEAN_CASES, ids=lambda c: c.id)
def test_clean_case_is_checked_and_sized_like_the_bug_cases(case: Case) -> None:
    # Base is the commit's parent (a 40-hex sha, not the commit itself), the
    # check covered 20 later commits, and the diff is 10-80 lines.
    assert case.base_ref != case.provenance.fix_commit
    assert len(case.base_ref) == 40
    check = case.provenance.clean_check
    assert check is not None and check.later_commits >= 20
    n = len((ROOT / case.diff_path).read_text(encoding="utf-8").splitlines())
    assert 10 <= n <= 80, n


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_diff_applies_cleanly_and_truth_is_in_range(case: Case, tmp_path: Path) -> None:
    diff = ROOT / case.diff_path
    base = CASES_DIR / case.id / "base"
    assert diff.is_file(), f"missing {case.diff_path}"
    assert base.is_dir(), f"missing {base.relative_to(ROOT)}"

    tree = tmp_path / "tree"
    shutil.copytree(base, tree)
    # Its own repo, so git apply resolves paths here and not in an enclosing checkout.
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    applied = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(diff)],
        cwd=tree,
        check=False,  # the assert below reports stderr
        capture_output=True,
        text=True,
    )
    assert applied.returncode == 0, applied.stderr

    for bug in case.bugs:
        f = tree / bug.file
        assert f.is_file(), f"{case.id}: ground-truth file {bug.file} not in buggy tree"
        n = len(f.read_text(encoding="utf-8").splitlines())
        assert bug.lines.end <= n, (
            f"{case.id}: {bug.file}:{bug.lines.end} past EOF ({n})"
        )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_diff_touches_only_non_test_go(case: Case) -> None:
    # Regression tests or changelog lines in the diff would hand the agent the label.
    files = [
        line.removeprefix("+++ b/").removeprefix("--- a/")
        for line in (ROOT / case.diff_path).read_text(encoding="utf-8").splitlines()
        if line.startswith(("+++ b/", "--- a/"))
    ]
    assert files
    assert all(is_reviewed_file(f) for f in files), files


@pytest.mark.parametrize("case", BUG_CASES, ids=lambda c: c.id)
def test_diff_deletes_no_comment_the_fix_added(case: Case) -> None:
    # A '-' line is a line the fix added. If it carries a comment, the comment
    # likely explains the fix ("avoid uint16 overflow") and leaks the label.
    leaks = [
        line
        for line in (ROOT / case.diff_path).read_text(encoding="utf-8").splitlines()
        if line.startswith("-")
        and not line.startswith("---")
        and strip_comment(line[1:]) != line[1:]
    ]
    assert not leaks, leaks
