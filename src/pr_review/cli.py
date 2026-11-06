"""Command line: `pr-review eval --arm <name> --runs N`."""

import argparse
import sys
from pathlib import Path

from pr_review.arms import ArmsError, load_arm
from pr_review.evaluate import DEFAULT_CONCURRENCY, base_dir, run_eval
from pr_review.llm import DEFAULT_BUDGET_USD, Budget, BudgetExceeded, CostUnknown
from pr_review.manifest import ManifestError, load_manifest
from pr_review.snapshot import build_snapshot, clone_dir
from pr_review.stages import DEFAULT_THRESHOLD, Stage
from pr_review.tools import DEFAULT_MAX_TURNS


def _stages(value: str) -> list[Stage]:
    names = [v.strip() for v in value.split(",") if v.strip()]
    try:
        return [Stage(n) for n in names]
    except ValueError as e:
        known = ", ".join(s.value for s in Stage)
        raise argparse.ArgumentTypeError(f"{e}; known stages: {known}") from e


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pr-review")
    sub = parser.add_subparsers(dest="command", required=True)
    ev = sub.add_parser("eval", help="run the golden set on one arm and score it")
    ev.add_argument("--arm", required=True, help="arm name in the arms file")
    ev.add_argument("--runs", type=int, default=3, help="runs per case (default 3)")
    ev.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        metavar="ID",
        help="only this case id (repeatable); for quick local smoke runs",
    )
    ev.add_argument("--arms-file", type=Path, default=Path("arms.toml"))
    ev.add_argument("--manifest", type=Path, default=Path("cases/manifest.jsonl"))
    ev.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="directory the manifest's diff_path values are relative to; "
        "each case's base files are in base/ next to its diff",
    )
    ev.add_argument("--cache-dir", type=Path, default=Path("cache/replies"))
    ev.add_argument("--results-dir", type=Path, default=Path("results"))
    ev.add_argument("--run-id", default=None)
    ev.add_argument(
        "--budget-usd",
        type=float,
        default=DEFAULT_BUDGET_USD,
        help=f"hard spend limit for this run (default {DEFAULT_BUDGET_USD})",
    )
    ev.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help="reviews in flight at once; 1 runs them one by one "
        f"(default {DEFAULT_CONCURRENCY})",
    )
    ev.add_argument(
        "--stages",
        type=_stages,
        default=[],
        metavar="LIST",
        help="comma-separated stages after generate: grade, dedup, suppress "
        "(always run in that order). Default: none, the single-pass reviewer",
    )
    ev.add_argument(
        "--grade-arm",
        default=None,
        help="arm for the grade stage (default: the --arm)",
    )
    ev.add_argument(
        "--grade-threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"drop candidates graded below this (default {DEFAULT_THRESHOLD})",
    )
    ev.add_argument(
        "--tools",
        action="store_true",
        help="ablation: let the generator call read-only tools (read_file, grep) "
        "on the case's offline snapshot (see `pr-review snapshot`)",
    )
    ev.add_argument("--snapshots-dir", type=Path, default=Path("cache/snapshots"))
    ev.add_argument(
        "--max-tool-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help=f"turns that may call tools before the answer (default {DEFAULT_MAX_TURNS})",
    )
    sn = sub.add_parser(
        "snapshot",
        help="build offline repo snapshots at each case's base for --tools "
        "(reads local clones; no network)",
    )
    sn.add_argument("--manifest", type=Path, default=Path("cases/manifest.jsonl"))
    sn.add_argument("--root", type=Path, default=Path("."))
    sn.add_argument("--repos-dir", type=Path, default=Path("cache/repos"))
    sn.add_argument(
        "--own-dir",
        type=Path,
        default=Path.home() / "projects",
        help="where the owner's repos are cloned (read only)",
    )
    sn.add_argument("--out", type=Path, default=Path("cache/snapshots"))
    args = parser.parse_args(argv)

    if args.command == "snapshot":
        return _snapshot(args)

    try:
        arm = load_arm(args.arms_file, args.arm)
        grade_arm = load_arm(args.arms_file, args.grade_arm) if args.grade_arm else arm
        cases = load_manifest(args.manifest)
        if args.case_ids:
            unknown = set(args.case_ids) - {c.id for c in cases}
            if unknown:
                raise ManifestError(f"unknown case ids: {', '.join(sorted(unknown))}")
            cases = [c for c in cases if c.id in args.case_ids]
    except (ArmsError, ManifestError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.runs < 1 or args.budget_usd <= 0 or args.concurrency < 1:
        print(
            "error: --runs, --budget-usd and --concurrency must be positive",
            file=sys.stderr,
        )
        return 2
    if Stage.GRADE not in args.stages and (
        args.grade_arm or args.grade_threshold != DEFAULT_THRESHOLD
    ):
        print(
            "warning: --grade-arm and --grade-threshold have no effect "
            "without --stages grade",
            file=sys.stderr,
        )
    if args.max_tool_turns < 1:
        print("error: --max-tool-turns must be positive", file=sys.stderr)
        return 2
    if not 0 <= args.grade_threshold <= 1:
        print("error: --grade-threshold must be in [0, 1]", file=sys.stderr)
        return 2
    budget = Budget(args.budget_usd)
    try:
        summary = run_eval(
            cases=cases,
            arm=arm,
            runs=args.runs,
            root=args.root,
            cache_dir=args.cache_dir,
            results_dir=args.results_dir,
            budget=budget,
            run_id=args.run_id,
            concurrency=args.concurrency,
            stages=args.stages,
            grade_arm=grade_arm,
            threshold=args.grade_threshold,
            snapshots_dir=args.snapshots_dir if args.tools else None,
            max_tool_turns=args.max_tool_turns,
        )
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except BudgetExceeded as e:
        print(
            f"stopped: {e}. Paid replies are cached; rerun with a larger "
            "--budget-usd to finish.",
            file=sys.stderr,
        )
        return 3
    except CostUnknown as e:
        print(f"stopped: {e}", file=sys.stderr)
        return 3
    print(summary)
    return 0


def _snapshot(args: argparse.Namespace) -> int:
    try:
        cases = load_manifest(args.manifest)
    except (ManifestError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    for case in cases:
        clone = clone_dir(case.provenance.repo, args.repos_dir, args.own_dir)
        if not clone.is_dir():
            print(f"error: {case.id}: no clone at {clone}", file=sys.stderr)
            return 2
        n = build_snapshot(case, clone, base_dir(case, args.root), args.out / case.id)
        print(f"{case.id}: {n} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
