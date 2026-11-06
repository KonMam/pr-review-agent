"""Build the CodeRabbit sandbox repo (item 6) locally, and check it for leakage.

Every golden-set case becomes two branches in a local bare repo:

- `base/case-NNN`: one parentless commit with the case's offline snapshot
  (non-test `.go` files at base, the case's own `base/` files laid over them;
  see `snapshot.py`), the upstream licence as `LICENSE`, and `.coderabbit.yaml`.
- `case/case-NNN`: that commit plus the case's diff, as one commit.

`main` holds only a README with the sources and licences. The case number is a
seeded shuffle of the case ids, so neither repo nor kind can be read off the
order. Every commit has the same author, committer and date, and messages and
PR text come from fixed templates, so a bug case and a clean case look the
same. The number ↔ case-id mapping is written only to this repo
(`cases/coderabbit-map.json`), never to the sandbox.

`check_repo` re-reads the built repo and lists anything that would leak: test
files, vendor or testdata, any file other than Go sources, LICENSE and the
config, history (parents), non-template names or messages, or a case id or
upstream SHA anywhere in names, messages or the README.
"""

import os
import random
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from pr_review.evaluate import base_dir
from pr_review.models import Case

SANDBOX_REPO = "KonMam/pr-review-sandbox"
SEED = "pr-review-sandbox-2026-11-06"
AUTHOR_NAME = "pr-review-sandbox"
AUTHOR_EMAIL = "pr-review-sandbox@users.noreply.github.com"
COMMIT_DATE = "2026-01-01T00:00:00Z"

PR_BODY = (
    "Sandbox pull request for an offline code-review evaluation. "
    "It is not meant to be merged.\n"
)
# Default review settings, with only the extras that are not findings turned
# off. `base_branches` lets reviews run on PRs whose base isn't `main`.
CODERABBIT_YAML = """\
# Default review settings; only extras that are not review findings are off.
reviews:
  poem: false
  sequence_diagrams: false
  related_issues: false
  related_prs: false
  suggested_labels: false
  suggested_reviewers: false
  estimate_code_review_effort: false
  in_progress_fortune: false
  finishing_touches:
    docstrings:
      enabled: false
    unit_tests:
      enabled: false
  auto_review:
    base_branches:
      - "base/.*"
"""

BOT_SUMMARY_MARKER = "<!-- This is an auto-generated comment"
ALLOWED_EXTRA = {"LICENSE", ".coderabbit.yaml"}
SKIP_DIRS = {"vendor", "testdata", ".git"}
NUM_RE = r"case-\d{3}"
BRANCH_RE = re.compile(rf"^(main|base/{NUM_RE}|case/{NUM_RE})$")


def label(n: int) -> str:
    return f"case-{n:03d}"


def pr_title(n: int) -> str:
    return f"Case {n:03d}"


def base_message(n: int) -> str:
    return f"Base for case {n:03d}"


def case_message(n: int) -> str:
    return f"Case {n:03d}"


def assign_numbers(case_ids: Sequence[str], seed: str = SEED) -> dict[str, int]:
    """Case id → sandbox number 1..N, from a seeded shuffle of the sorted ids."""
    ids = sorted(case_ids)
    random.Random(seed).shuffle(ids)
    return {cid: i for i, cid in enumerate(ids, start=1)}


def licence_file(case: Case, licences_dir: Path) -> Path | None:
    """The upstream licence text `cases/build.sh` copied, if the repo has one."""
    p = licences_dir / (case.provenance.repo.removeprefix("github.com/") + ".txt")
    return p if p.is_file() else None


def _git(
    git_dir: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> str:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git", "--git-dir", str(git_dir.resolve()), *args],
        check=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=full_env,
    ).stdout.strip()


def _ident_env() -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
        "GIT_AUTHOR_DATE": COMMIT_DATE,
        "GIT_COMMITTER_NAME": AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
        "GIT_COMMITTER_DATE": COMMIT_DATE,
    }


def _commit_dir(git_dir: Path, work: Path, message: str, parent: str | None) -> str:
    """Commit everything under `work` as one tree; returns the commit SHA."""
    index = git_dir.resolve() / "sandbox-index"
    index.unlink(missing_ok=True)
    env = {"GIT_INDEX_FILE": str(index)}
    _git(
        git_dir, "--work-tree", str(work.resolve()), "add", "-A", ".", env=env, cwd=work
    )
    tree = _git(git_dir, "write-tree", env=env)
    index.unlink()
    args = ["commit-tree", tree, "-m", message]
    if parent:
        args += ["-p", parent]
    return _git(git_dir, *args, env=_ident_env())


def _fill_base(
    case: Case, snapshot: Path, root: Path, licences_dir: Path, work: Path
) -> None:
    shutil.copytree(snapshot, work)
    # The snapshot already has base/ laid over it; do it again so a stale
    # snapshot can't hold a pre-overlay file (fix comments included).
    over = base_dir(case, root)
    for f in over.rglob("*"):
        if f.is_file():
            dest = work / f.relative_to(over)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
    lic = licence_file(case, licences_dir)
    if lic:
        shutil.copyfile(lic, work / "LICENSE")
    (work / ".coderabbit.yaml").write_text(CODERABBIT_YAML, encoding="utf-8")


def build_sandbox(
    cases: Sequence[Case],
    numbers: Mapping[str, int],
    snapshots_dir: Path,
    root: Path,
    licences_dir: Path,
    readme: str,
    git_dir: Path,
    scratch: Path,
) -> dict[str, tuple[str, str]]:
    """Build every branch in a fresh bare repo. Returns case id → (base SHA, case SHA)."""
    shutil.rmtree(git_dir, ignore_errors=True)
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(git_dir)], check=True
    )
    shas: dict[str, tuple[str, str]] = {}
    for case in cases:
        n = numbers[case.id]
        work = scratch / label(n)
        shutil.rmtree(work, ignore_errors=True)
        _fill_base(case, snapshots_dir / case.id, root, licences_dir, work)
        base_sha = _commit_dir(git_dir, work, base_message(n), None)
        # Through the sandbox's git dir, so paths resolve against `work` even
        # when `work` sits inside another checkout.
        _git(
            git_dir,
            "--work-tree",
            str(work.resolve()),
            "apply",
            "--whitespace=nowarn",
            str((root / case.diff_path).resolve()),
            cwd=work,
        )
        case_sha = _commit_dir(git_dir, work, case_message(n), base_sha)
        _git(git_dir, "update-ref", f"refs/heads/base/{label(n)}", base_sha)
        _git(git_dir, "update-ref", f"refs/heads/case/{label(n)}", case_sha)
        shutil.rmtree(work)
        shas[case.id] = (base_sha, case_sha)
    main = scratch / "main"
    shutil.rmtree(main, ignore_errors=True)
    main.mkdir(parents=True)
    (main / "README.md").write_text(readme, encoding="utf-8")
    (main / ".coderabbit.yaml").write_text(CODERABBIT_YAML, encoding="utf-8")
    _git(
        git_dir,
        "update-ref",
        "refs/heads/main",
        _commit_dir(git_dir, main, "README", None),
    )
    shutil.rmtree(main)
    return shas


def readme_text(cases: Sequence[Case]) -> str:
    """Sources and licences, per upstream repo; nothing per case."""
    repos: dict[str, str] = {}
    for c in cases:
        repos.setdefault(c.provenance.repo, c.provenance.licence)
    lines = [
        "# pr-review-sandbox",
        "",
        "Pull requests for an offline evaluation of automated code review.",
        "Each `base/case-NNN` branch holds the non-test Go files of one upstream",
        "Go repository at one commit, and each `case/case-NNN` branch adds one",
        "change on top. The pull requests are not meant to be merged.",
        "",
        "Code in each branch belongs to its upstream authors and keeps its",
        "upstream licence (in that branch's `LICENSE` file).",
        "",
        "| Source | Licence |",
        "|---|---|",
    ]
    for repo in sorted(repos):
        lic = repos[repo]
        if lic == "none":
            lic = "the sandbox owner's own code (no licence file)"
        lines.append(f"| https://{repo} | {lic} |")
    return "\n".join(lines) + "\n"


# --- leakage checks -------------------------------------------------------


def check_paths(paths: Sequence[str]) -> list[str]:
    """Problems with a tree's file list: only non-test Go, LICENSE and config."""
    problems = []
    for path in paths:
        p = PurePosixPath(path)
        if path in ALLOWED_EXTRA:
            continue
        if p.suffix != ".go":
            problems.append(f"non-Go file {path}")
        elif p.name.endswith("_test.go"):
            problems.append(f"test file {path}")
        elif SKIP_DIRS & set(p.parts[:-1]):
            problems.append(f"vendored or testdata file {path}")
    return problems


def forbidden_strings(cases: Sequence[Case]) -> list[str]:
    """Strings that would tie a sandbox case to its golden-set case."""
    out = []
    for c in cases:
        out.append(c.id)
        out.append(c.provenance.fix_commit)
        out.append(c.provenance.fix_commit[:7])
        out.append(c.base_ref)
    return sorted(set(out))


def check_text(where: str, text: str, forbidden: Sequence[str]) -> list[str]:
    low = text.lower()
    return [f"{where} contains {s!r}" for s in forbidden if s.lower() in low]


def check_pr_text(n: int, title: str, body: str) -> list[str]:
    problems = []
    if title != pr_title(n):
        problems.append(f"PR {n}: title {title!r} is not {pr_title(n)!r}")
    # CodeRabbit appends its own summary after a marker; GitHub trims the end.
    own = body.split(BOT_SUMMARY_MARKER, 1)[0]
    if own.strip() != PR_BODY.strip():
        problems.append(f"PR {n}: body is not the shared template")
    return problems


def check_repo(
    git_dir: Path, cases: Sequence[Case], numbers: Mapping[str, int]
) -> list[str]:
    """Everything in the built repo that could leak a case's identity or kind."""
    problems: list[str] = []
    forbidden = forbidden_strings(cases)
    refs = _git(
        git_dir, "for-each-ref", "--format=%(refname:short)", "refs/heads"
    ).split()
    expected = {"main"} | {
        f"{side}/{label(n)}" for n in numbers.values() for side in ("base", "case")
    }
    for ref in refs:
        if not BRANCH_RE.match(ref):
            problems.append(f"branch name {ref!r} is not neutral")
    if set(refs) != expected:
        problems.append(
            f"branches differ from expected: missing {sorted(expected - set(refs))}, "
            f"extra {sorted(set(refs) - expected)}"
        )
    want_ident = f"{AUTHOR_NAME} <{AUTHOR_EMAIL}> 2026-01-01T00:00:00Z"
    for ref in sorted(set(refs) & expected):
        parents = _git(git_dir, "log", "-1", "--format=%P", ref).split()
        idents = [
            _git(git_dir, "log", "-1", f"--format={fmt}", ref)
            for fmt in ("%an <%ae> %aI", "%cn <%ce> %cI")
        ]
        message = _git(git_dir, "log", "-1", "--format=%B", ref)
        if any(i != want_ident for i in idents):
            problems.append(f"{ref}: author or committer is not the neutral identity")
        if ref == "main":
            want_msg, want_parents = "README", 0
        else:
            side, lab = ref.split("/")
            n = int(lab.removeprefix("case-"))
            want_msg = base_message(n) if side == "base" else case_message(n)
            want_parents = 0 if side == "base" else 1
            if side == "case":
                if parents and parents[0] != _git(git_dir, "rev-parse", f"base/{lab}"):
                    problems.append(f"{ref}: parent is not base/{lab}")
                if not _git(git_dir, "diff", "--name-only", f"base/{lab}", ref):
                    problems.append(f"{ref}: no change on top of base/{lab}")
        if message != want_msg:
            problems.append(f"{ref}: message {message!r} is not {want_msg!r}")
        if len(parents) != want_parents:
            problems.append(f"{ref}: {len(parents)} parents, want {want_parents}")
        paths = _git(git_dir, "ls-tree", "-r", "--name-only", ref).splitlines()
        if ref == "main":
            if sorted(paths) != [".coderabbit.yaml", "README.md"]:
                problems.append(f"main has unexpected files {paths}")
            readme = _git(git_dir, "show", "main:README.md")
            problems += check_text("main README", readme, forbidden)
            if re.search(NUM_RE, readme.replace("case-NNN", "")):
                problems.append("main README names a sandbox case number")
        else:
            problems += [f"{ref}: {p}" for p in check_paths(paths)]
        problems += check_text(f"{ref} message", message, forbidden)
        problems += check_text(f"{ref} name", ref, forbidden)
    return problems
