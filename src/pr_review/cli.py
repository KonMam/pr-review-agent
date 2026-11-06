"""Command line: `pr-review eval --arm <name> --runs N`."""

import argparse
import sys
from pathlib import Path

from pr_review.arms import ArmsError, load_arm
from pr_review.evaluate import run_eval
from pr_review.llm import DEFAULT_BUDGET_USD, Budget, BudgetExceeded, CostUnknown
from pr_review.manifest import ManifestError, load_manifest


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
    args = parser.parse_args(argv)

    try:
        arm = load_arm(args.arms_file, args.arm)
        cases = load_manifest(args.manifest)
        if args.case_ids:
            unknown = set(args.case_ids) - {c.id for c in cases}
            if unknown:
                raise ManifestError(f"unknown case ids: {', '.join(sorted(unknown))}")
            cases = [c for c in cases if c.id in args.case_ids]
    except (ArmsError, ManifestError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.runs < 1 or args.budget_usd <= 0:
        print("error: --runs and --budget-usd must be positive", file=sys.stderr)
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
        )
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


if __name__ == "__main__":
    sys.exit(main())
