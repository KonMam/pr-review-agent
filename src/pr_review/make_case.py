"""Turn a Go bug-fix commit into a golden-set case.

The case is a synthetic PR that reintroduces the bug:

- **base** is the fix commit (the fixed code),
- **the diff** reverts the fix, limited to non-test `.go` files, so the agent
  never sees the regression test or a CHANGELOG line that names the bug,
- **comments the fix added are stripped** from the base files first (whole
  comment lines and trailing `//` comments on lines the fix added), so the
  diff never deletes a comment such as "avoid uint16 overflow" that states
  the label. The base is therefore the fix commit minus those comments.
- **ground truth** is the lines the chosen hunks put back into the buggy file.
  Hunks not chosen stay in the diff as noise (unrelated edits the fix carried).

It writes, under `--cases-dir` (default `cases/`):

- `<id>/buggy.diff`  the revert diff, applied to `<id>/base/`
- `<id>/base/<path>` each touched file at the fix commit (comments the fix
  added stripped), so `verify.sh` can
  check offline that the diff applies, and the agent can read full files
- one line in `manifest.jsonl` (replaced in place if the id already exists)

The source repo is only read (`git diff`, `git show`), never written.

    uv run python -m pr_review.make_case --repo-dir DIR --fix SHA --list-hunks
    uv run python -m pr_review.make_case --repo-dir DIR --fix SHA --id ID \\
        --repo github.com/o/r --licence MIT --bug-class nil_deref --truth 0,2

With `--clean`, `--fix` names an ordinary commit with no known bug, and the
case is that commit as it was: base is its parent, the diff is the commit's
non-test Go change, and `bugs` is empty, so every finding on it is a false
positive. Nothing is stripped; there is no label to leak. It is only written
if no later commit touched what it changed (see `clean_check`).

    uv run python -m pr_review.make_case --clean --repo-dir DIR --fix SHA \\
        --id ID --repo github.com/o/r --licence MIT [--later 20]
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pr_review.manifest import load_manifest
from pr_review.models import (
    Bug,
    BugClass,
    Case,
    CaseKind,
    CleanCheck,
    LineRange,
    Provenance,
)
from pr_review.scoring import TOLERANCE

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Hunk:
    """One zero-context hunk of the buggy diff (fixed → buggy)."""

    file: str  # path in the buggy tree
    new_start: int
    new_count: int
    text: str  # the hunk's -/+ lines, for --list-hunks


def git(repo: Path, *args: str) -> str:
    """Run a read-only git command in `repo` and return stdout."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def is_reviewed_file(path: str) -> bool:
    """Non-test Go source. Tests, testdata and non-Go files stay out of the diff."""
    parts = path.split("/")
    return (
        path.endswith(".go")
        and not path.endswith("_test.go")
        and "testdata" not in parts
        and "vendor" not in parts
    )


def changed_files(repo: Path, fix: str) -> list[str]:
    out = git(repo, "diff", "--no-renames", "--name-only", f"{fix}^", fix)
    return [p for p in out.splitlines() if is_reviewed_file(p)]


def show(repo: Path, rev: str, path: str) -> bytes | None:
    """A file's bytes at `rev`, or None when it doesn't exist there."""
    r = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"],
        check=False,  # a missing file is an answer, not an error
        capture_output=True,
    )
    return r.stdout if r.returncode == 0 else None


def strip_comment(line: str) -> str | None:
    """Drop a `//` comment from one Go line: None for a comment-only line.

    Compiler directives (`//go:`) are kept. `//` inside a string, rune or raw
    string literal is not a comment. Block comments are left alone.
    """
    body = line.strip()
    if body.startswith("//"):
        return line if body.startswith("//go:") else None
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote != "`":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif line.startswith("//", i):
            return line[:i].rstrip()
        i += 1
    return line


def strip_added_comments(base: bytes, added: set[int]) -> bytes:
    """Remove the comments on lines (1-based) that the fix added."""
    lines = base.decode("utf-8").split("\n")
    out: list[str] = []
    for n, line in enumerate(lines, start=1):
        kept = strip_comment(line) if n in added else line
        if kept is not None:
            out.append(kept)
    return "\n".join(out).encode("utf-8")


def added_lines(repo: Path, fix: str, path: str) -> set[int]:
    """Line numbers, in the fix commit's file, of lines the fix added or changed."""
    diff = git(repo, "diff", "--no-renames", "-U0", f"{fix}^", fix, "--", path)
    return {
        n
        for h in parse_hunks(diff)
        for n in range(h.new_start, h.new_start + h.new_count)
    }


@dataclass(frozen=True)
class Revert:
    """A fix reverted against comment-stripped base files."""

    base: dict[str, bytes]  # path -> base content; absent if the fix deleted it
    diff0: str  # zero-context diff, for hunks and ground truth
    diff3: str  # the stored diff


def make_revert(repo: Path, fix: str, files: list[str]) -> Revert:
    """Diff the stripped base against the buggy files in a scratch repo.

    The source repo is only read; the scratch repo lives in a temp dir.
    """
    base: dict[str, bytes] = {}
    for f in files:
        content = show(repo, fix, f)
        if content is not None:
            base[f] = strip_added_comments(content, added_lines(repo, fix, f))
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        git(work, "init", "-q")
        for f, content in base.items():
            (work / f).parent.mkdir(parents=True, exist_ok=True)
            (work / f).write_bytes(content)
        git(work, "add", "-A")
        git(work, "-c", "user.name=case", "-c", "user.email=case@local",
            "commit", "-q", "--allow-empty", "-m", "base")  # fmt: skip
        for f in files:
            buggy = show(repo, f"{fix}^", f)
            if buggy is None:
                (work / f).unlink()
            else:
                (work / f).parent.mkdir(parents=True, exist_ok=True)
                (work / f).write_bytes(buggy)
        git(work, "add", "-A")
        diffs = [
            git(work, "diff", "--cached", "--no-renames", f"-U{n}", "--", *files)
            for n in (0, 3)
        ]
    return Revert(base=base, diff0=diffs[0], diff3=diffs[1])


def parse_hunks(diff: str) -> list[Hunk]:
    """Split a zero-context diff into hunks, keyed by the buggy-side file path."""
    hunks: list[Hunk] = []
    file: str | None = None
    current: tuple[int, int] | None = None
    body: list[str] = []

    def flush() -> None:
        if file is not None and current is not None:
            hunks.append(Hunk(file, current[0], current[1], "\n".join(body)))

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            flush()
            current, body, file = None, [], None
        elif line.startswith("+++ "):
            target = line[4:]
            file = None if target == "/dev/null" else target.removeprefix("b/")
        elif m := _HUNK_RE.match(line):
            flush()
            count = 1 if m.group(4) is None else int(m.group(4))
            current, body = (int(m.group(3)), count), []
        elif current is not None and line[:1] in "+-\\":
            body.append(line)
    flush()
    return hunks


def hunk_range(h: Hunk, file_len: int) -> LineRange:
    """Lines of the buggy file a hunk covers.

    A hunk that only deletes (the fix *added* the lines, e.g. a nil check) has
    no lines on the buggy side; it is anchored to the line before and after
    the gap, which is where a reviewer would comment.
    """
    if h.new_count > 0:
        return LineRange(start=h.new_start, end=h.new_start + h.new_count - 1)
    start = max(1, h.new_start)
    return LineRange(start=start, end=max(start, min(h.new_start + 1, file_len)))


def truth_bugs(
    hunks: list[Hunk], chosen: list[int], file_lens: dict[str, int]
) -> list[Bug]:
    """One `Bug` per cluster of chosen hunks in the same file.

    Hunks closer than the scorer's widened window (2 x TOLERANCE lines apart)
    are one location to a reviewer, so they merge; hunks further apart stay
    separate bugs and recall counts each.
    """
    by_file: dict[str, list[LineRange]] = {}
    for i in chosen:
        h = hunks[i]
        by_file.setdefault(h.file, []).append(hunk_range(h, file_lens[h.file]))
    bugs: list[Bug] = []
    for file, ranges in by_file.items():
        ranges.sort(key=lambda r: r.start)
        merged = [ranges[0]]
        for r in ranges[1:]:
            last = merged[-1]
            if r.start - last.end <= 2 * TOLERANCE:
                merged[-1] = LineRange(start=last.start, end=max(last.end, r.end))
            else:
                merged.append(r)
        bugs.extend(Bug(file=file, lines=r) for r in merged)
    return bugs


def parse_indices(spec: str | None, n: int) -> list[int]:
    if spec is None:
        return list(range(n))
    out = sorted({int(s) for s in spec.split(",") if s.strip()})
    bad = [i for i in out if not 0 <= i < n]
    if bad:
        raise SystemExit(f"--truth: hunk index out of range 0..{n - 1}: {bad}")
    if not out:
        raise SystemExit("--truth: choose at least one hunk")
    return out


def buggy_file_lengths(repo: Path, fix: str, files: list[str]) -> dict[str, int]:
    """Line count of each file in the buggy tree (the fix's parent)."""
    lens: dict[str, int] = {}
    for f in files:
        buggy = show(repo, f"{fix}^", f)
        # 0: the fix added this file; the revert deletes it
        lens[f] = 0 if buggy is None else len(buggy.splitlines())
    return lens


def write_case(args: argparse.Namespace) -> Case:
    repo: Path = args.repo_dir
    fix = git(repo, "rev-parse", f"{args.fix}^{{commit}}").strip()
    files = changed_files(repo, fix)
    if not files:
        raise SystemExit(f"{fix}: no non-test Go files changed")
    revert = make_revert(repo, fix, files)
    hunks = parse_hunks(revert.diff0)
    chosen = parse_indices(args.truth, len(hunks))
    lens = buggy_file_lengths(repo, fix, files)
    if any(lens[hunks[i].file] == 0 for i in chosen):
        raise SystemExit("--truth: a chosen hunk deletes a whole file; pick another")

    case = Case(
        id=args.id,
        provenance=Provenance(repo=args.repo, fix_commit=fix, licence=args.licence),
        base_ref=fix,
        diff_path=f"{args.cases_dir.as_posix()}/{args.id}/buggy.diff",
        bug_class=BugClass(args.bug_class),
        bugs=truth_bugs(hunks, chosen, lens),
    )

    case_dir = args.cases_dir / args.id
    if case_dir.exists():
        shutil.rmtree(case_dir)
    (case_dir / "base").mkdir(parents=True)
    (case_dir / "buggy.diff").write_text(revert.diff3, encoding="utf-8")
    for f, content in revert.base.items():
        dest = case_dir / "base" / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    upsert(args.cases_dir / "manifest.jsonl", case)
    return case


CLEAN_LATER_COMMITS = 20


class NotClean(Exception):
    """A later commit touched what the candidate clean commit changed."""


def later_commits(repo: Path, commit: str, n: int) -> list[str]:
    """The first `n` commits after `commit` on the first-parent line to the clone's HEAD."""
    out = git(repo, "rev-list", "--reverse", "--first-parent", "--ancestry-path",
              f"{commit}..HEAD")  # fmt: skip
    later = out.split()
    if len(later) < n:
        raise NotClean(f"only {len(later)} commits after {commit[:9]}, need {n}")
    return later[:n]


def blame_lines(repo: Path, rev: str, path: str) -> list[str]:
    """The commit each line of `path` at `rev` comes from, in file order."""
    r = subprocess.run(
        ["git", "-C", str(repo), "blame", "--porcelain", rev, "--", path],
        check=False,  # a missing file is an answer, not an error
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise NotClean(f"{path} is gone at {rev[:9]}")
    owners: list[str] = []
    for line in r.stdout.splitlines():
        if line.startswith("\t"):
            continue
        parts = line.split()
        # header: <40-hex sha> <orig line> <final line> [<group size>]
        if len(parts) in (3, 4) and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
            owners.append(parts[0])
    return owners


def check_clean(
    repo: Path, commit: str, files: list[str], n: int = CLEAN_LATER_COMMITS
) -> CleanCheck:
    """Check no commit in the next `n` touched the lines `commit` added.

    Blame at the n-th later commit: every line `commit` added must still come
    from `commit`, and no line within the scorer's tolerance of one may come
    from a commit in between (an insertion, such as a nil check added next to
    them). Any later edit there counts, fix or not, so the check is strict.
    Raises `NotClean` otherwise.
    """
    later = later_commits(repo, commit, n)
    # Every commit in between, merged branches included: blame credits a
    # merged line to the branch commit that wrote it, not to the merge.
    window = set(git(repo, "rev-list", f"{commit}..{later[-1]}").split())
    for f in files:
        added = len(added_lines(repo, commit, f))
        if added == 0:
            continue  # the commit only deleted lines here
        owners = blame_lines(repo, later[-1], f)
        mine = [i for i, o in enumerate(owners) if o == commit]
        if len(mine) != added:
            raise NotClean(
                f"{f}: {added - len(mine)} of {added} added lines changed later"
            )
        for i in mine:
            near = owners[max(0, i - TOLERANCE) : i + TOLERANCE + 1]
            if hits := sorted(window.intersection(near)):
                raise NotClean(f"{f}: {hits[0][:9]} edited within {TOLERANCE} lines")
    return CleanCheck(later_commits=n, checked_at=later[-1])


def write_clean_case(args: argparse.Namespace) -> Case:
    repo: Path = args.repo_dir
    commit = git(repo, "rev-parse", f"{args.fix}^{{commit}}").strip()
    parents = git(repo, "rev-list", "--parents", "-n1", commit).split()[1:]
    if len(parents) != 1:
        raise SystemExit(f"{commit}: need exactly one parent, has {len(parents)}")
    parent = parents[0]
    files = changed_files(repo, commit)
    if not files:
        raise SystemExit(f"{commit}: no non-test Go files changed")
    try:
        check = check_clean(repo, commit, files, args.later)
    except NotClean as e:
        raise SystemExit(f"{commit[:9]} is not clean: {e}") from e
    diff = git(repo, "diff", "--no-renames", "-U3", parent, commit, "--", *files)

    case = Case(
        id=args.id,
        kind=CaseKind.CLEAN,
        provenance=Provenance(
            repo=args.repo, fix_commit=commit, licence=args.licence, clean_check=check
        ),
        base_ref=parent,
        diff_path=f"{args.cases_dir.as_posix()}/{args.id}/change.diff",
        bugs=[],
    )
    case_dir = args.cases_dir / args.id
    if case_dir.exists():
        shutil.rmtree(case_dir)
    (case_dir / "base").mkdir(parents=True)
    (case_dir / "change.diff").write_text(diff, encoding="utf-8")
    for f in files:
        content = show(repo, parent, f)
        if content is not None:  # absent: the commit adds this file
            dest = case_dir / "base" / f
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)

    upsert(args.cases_dir / "manifest.jsonl", case)
    return case


def upsert(manifest: Path, case: Case) -> None:
    """Replace the case's line if its id exists, else append. Keeps the file valid."""
    cases = load_manifest(manifest) if manifest.exists() else []
    cases = [case if c.id == case.id else c for c in cases]
    if case.id not in {c.id for c in cases}:
        cases.append(case)
    manifest.write_text(
        "".join(json.dumps(c.model_dump(mode="json")) + "\n" for c in cases),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo-dir", type=Path, required=True, help="local clone to read")
    p.add_argument(
        "--fix", required=True, help="the fix commit (with --clean: the commit)"
    )
    p.add_argument(
        "--clean",
        action="store_true",
        help="build a clean case from an ordinary commit",
    )
    p.add_argument(
        "--later",
        type=int,
        default=CLEAN_LATER_COMMITS,
        help="--clean: later commits that must leave its lines alone",
    )
    p.add_argument("--list-hunks", action="store_true", help="print hunks and exit")
    p.add_argument("--id")
    p.add_argument("--repo", help="provenance, e.g. github.com/owner/name")
    p.add_argument("--licence", help="SPDX id of the repo's licence")
    p.add_argument("--bug-class", choices=[c.value for c in BugClass])
    p.add_argument("--truth", help="comma-separated hunk indices (default: all)")
    p.add_argument("--cases-dir", type=Path, default=Path("cases"))
    args = p.parse_args(argv)

    if args.list_hunks:
        fix = git(args.repo_dir, "rev-parse", f"{args.fix}^{{commit}}").strip()
        files = changed_files(args.repo_dir, fix)
        revert = make_revert(args.repo_dir, fix, files)
        for i, h in enumerate(parse_hunks(revert.diff0)):
            print(f"[{i}] {h.file} +{h.new_start},{h.new_count}\n{h.text}\n")
        return 0
    needed = ("id", "repo", "licence") + (() if args.clean else ("bug_class",))
    missing = [n for n in needed if not getattr(args, n)]
    if missing:
        p.error(
            "required to write a case: "
            + ", ".join("--" + m.replace("_", "-") for m in missing)
        )
    if args.clean and (args.bug_class or args.truth):
        p.error("--clean takes no --bug-class or --truth")
    case = write_clean_case(args) if args.clean else write_case(args)
    print(json.dumps(case.model_dump(mode="json")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
