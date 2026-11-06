"""Offline repo snapshots at each case's base, for the read-only tools ablation.

`pr-review snapshot` reads local clones (the ones `cases/build.sh` made) and
writes `<out>/<case id>/`: every non-test `.go` file at the case's base ref,
outside `vendor/` and `testdata/`, with the case's own `base/` files laid over
them. For a bug case the base ref is the fix commit and `base/` is that commit
minus the comments the fix added, so the overlay keeps those comments out too.

Test files are left out because a fix commit's regression test would name the
bug. Snapshots live under `cache/` (gitignored); reading them needs no network.
Building them needs the clones, and never writes to them.
"""

import io
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

from pr_review.models import Case

SKIP_DIRS = {"vendor", "testdata", ".git"}


def clone_dir(repo: str, repos_dir: Path, own_dir: Path) -> Path:
    """Where `cases/build.sh` keeps the clone for a provenance repo."""
    if repo.startswith("github.com/KonMam/"):
        return own_dir / repo.rsplit("/", 1)[-1]
    owner_name = repo.removeprefix("github.com/")
    return repos_dir / owner_name.replace("/", "_", 1)


def keep(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        p.suffix == ".go"
        and not p.name.endswith("_test.go")
        and not SKIP_DIRS & set(p.parts[:-1])
    )


def build_snapshot(case: Case, clone: Path, case_base: Path, out: Path) -> int:
    """Write one case's snapshot to `out` (replaced if present). Returns its file count."""
    tar_bytes = subprocess.run(
        ["git", "-C", str(clone), "archive", "--format=tar", case.base_ref],
        check=True,
        capture_output=True,
    ).stdout
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        members = [m for m in tar.getmembers() if m.isfile() and keep(m.name)]
        tar.extractall(tmp, members=members, filter="data")
    for f in case_base.rglob("*"):
        if f.is_file():
            dest = tmp / f.relative_to(case_base)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    return sum(1 for p in out.rglob("*") if p.is_file())
