"""Command line: `pr-review eval --arm <name> --runs N`, `snapshot`, `judge`, `review`, `post`."""

import argparse
import json
import os
import sys
from pathlib import Path

from pr_review.arms import ArmsError, load_arm
from pr_review.evaluate import DEFAULT_CONCURRENCY, base_dir, run_eval
from pr_review.github import (
    DEFAULT_API_URL,
    GitHub,
    GitHubError,
    post_report,
    write_log,
)
from pr_review.judge import Row, load_agent_row, load_coderabbit_row, run_judge
from pr_review.llm import DEFAULT_BUDGET_USD, Budget, BudgetExceeded, CostUnknown
from pr_review.manifest import ManifestError, load_manifest
from pr_review.review import (
    DEFAULT_REVIEW_BUDGET_USD,
    DEFAULT_REVIEW_STAGES,
    Report,
    review_pr,
)
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
    ju = sub.add_parser(
        "judge",
        help="ask a judge arm whether each location-matched finding names the "
        "ground-truth defect; scores rows at location and claim level",
    )
    ju.add_argument("--arm", required=True, help="judge arm in the arms file")
    ju.add_argument(
        "--row",
        action="append",
        required=True,
        metavar="LABEL=KIND:PATH",
        help="a row to score (repeatable). KIND is agent (a reviews.jsonl), "
        "agent-no-grade (a graded run's reviews.jsonl without the grade stage), "
        "coderabbit (findings.json, all comments) or coderabbit-major "
        "(Major or Critical only)",
    )
    ju.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="ID",
        help="leave this case out of every row (repeatable)",
    )
    ju.add_argument(
        "--repeat",
        type=int,
        default=0,
        help="judge this many distinct findings a second time (seeded sample) "
        "and report agreement (default 0)",
    )
    ju.add_argument("--arms-file", type=Path, default=Path("arms.toml"))
    ju.add_argument("--manifest", type=Path, default=Path("cases/manifest.jsonl"))
    ju.add_argument("--root", type=Path, default=Path("."))
    ju.add_argument("--cache-dir", type=Path, default=Path("cache/replies"))
    ju.add_argument("--out", type=Path, required=True, help="where judge.json goes")
    ju.add_argument(
        "--budget-usd",
        type=float,
        default=DEFAULT_BUDGET_USD,
        help=f"hard spend limit for this run (default {DEFAULT_BUDGET_USD})",
    )
    ju.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ju.add_argument(
        "--max-rpm",
        type=float,
        default=None,
        help="start at most this many judge calls a minute (provider rate caps)",
    )
    rv = sub.add_parser(
        "review",
        help="review one PR: its diff and a tree of the repo at its base; "
        "prints a JSON report (CI runs this)",
    )
    rv.add_argument("--diff", type=Path, required=True, help="the PR's unified diff")
    rv.add_argument(
        "--base-dir",
        type=Path,
        required=True,
        help="the repo's files at the PR's base (merge base), e.g. a git archive",
    )
    rv.add_argument("--arm", default="or-deepseek-flash")
    rv.add_argument("--arms-file", type=Path, default=Path("arms.toml"))
    rv.add_argument(
        "--stages",
        type=_stages,
        default=list(DEFAULT_REVIEW_STAGES),
        metavar="LIST",
        help="stages after generate (default: dedup,suppress)",
    )
    rv.add_argument("--grade-arm", default=None)
    rv.add_argument("--grade-threshold", type=float, default=DEFAULT_THRESHOLD)
    rv.add_argument(
        "--tools",
        action="store_true",
        help="let the generator call read_file/grep on --base-dir",
    )
    rv.add_argument("--max-tool-turns", type=int, default=DEFAULT_MAX_TURNS)
    rv.add_argument(
        "--budget-usd",
        type=float,
        default=DEFAULT_REVIEW_BUDGET_USD,
        help=f"hard spend limit for this review (default {DEFAULT_REVIEW_BUDGET_USD})",
    )
    rv.add_argument("--out", type=Path, default=None, help="also write the report here")
    po = sub.add_parser(
        "post",
        help="post a review report as inline PR comments and write the findings "
        "log (token in GITHUB_TOKEN)",
    )
    po.add_argument("--report", type=Path, required=True)
    po.add_argument(
        "--diff", type=Path, required=True, help="the diff the report reviewed"
    )
    po.add_argument("--repo", required=True, help="owner/name")
    po.add_argument("--pr", type=int, required=True)
    po.add_argument("--head-sha", required=True)
    po.add_argument(
        "--log", type=Path, required=True, help="JSONL findings log to write"
    )
    po.add_argument("--api-url", default=DEFAULT_API_URL)
    args = parser.parse_args(argv)

    if args.command == "snapshot":
        return _snapshot(args)
    if args.command == "judge":
        return _judge(args)
    if args.command == "review":
        return _review(args)
    if args.command == "post":
        return _post(args)

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


def _review(args: argparse.Namespace) -> int:
    try:
        arm = load_arm(args.arms_file, args.arm)
        grade_arm = load_arm(args.arms_file, args.grade_arm) if args.grade_arm else None
        diff = args.diff.read_text(encoding="utf-8")
    except (ArmsError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not args.base_dir.is_dir():
        print(f"error: --base-dir {args.base_dir} is not a directory", file=sys.stderr)
        return 2
    if args.budget_usd <= 0 or args.max_tool_turns < 1:
        print(
            "error: --budget-usd and --max-tool-turns must be positive", file=sys.stderr
        )
        return 2
    if not 0 <= args.grade_threshold <= 1:
        print("error: --grade-threshold must be in [0, 1]", file=sys.stderr)
        return 2
    budget = Budget(args.budget_usd)
    try:
        report = review_pr(
            diff=diff,
            base_dir=args.base_dir,
            arm=arm,
            budget=budget,
            stages=args.stages,
            grade_arm=grade_arm,
            threshold=args.grade_threshold,
            tools=args.tools,
            max_tool_turns=args.max_tool_turns,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        # BudgetExceeded and CostUnknown are RuntimeErrors too.
        print(f"stopped: {e}", file=sys.stderr)
        return 3
    text = report.model_dump_json(indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def _post(args: argparse.Namespace) -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("error: GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    try:
        report = Report.model_validate_json(args.report.read_text(encoding="utf-8"))
        diff = args.diff.read_text(encoding="utf-8")
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        posted = post_report(
            GitHub(token, args.api_url),
            report=report,
            diff=diff,
            repo=args.repo,
            pr=args.pr,
            head_sha=args.head_sha,
        )
    except (GitHubError, ValueError) as e:
        write_log(args.log, [])
        print(f"error: {e}", file=sys.stderr)
        return 3
    write_log(args.log, posted.records)
    inline = sum(r.posted == "inline" for r in posted.records)
    print(
        f"posted {len(posted.records)} findings ({inline} inline) "
        f"in review {posted.review_id}; log: {args.log}"
    )
    return 0


ROW_KINDS = ("agent", "agent-no-grade", "coderabbit", "coderabbit-major")


def _load_row(spec: str) -> tuple[str, Row]:
    label, sep, rest = spec.partition("=")
    kind, sep2, path = rest.partition(":")
    if not (sep and sep2 and label and path) or kind not in ROW_KINDS:
        raise ValueError(
            f"--row {spec!r}: expected LABEL=KIND:PATH, KIND one of {', '.join(ROW_KINDS)}"
        )
    p = Path(path)
    if kind.startswith("agent"):
        return label, load_agent_row(p, without_grade=kind == "agent-no-grade")
    return label, load_coderabbit_row(p, serious_only=kind == "coderabbit-major")


def _judge(args: argparse.Namespace) -> int:
    try:
        arm = load_arm(args.arms_file, args.arm)
        cases = load_manifest(args.manifest)
        unknown = set(args.exclude) - {c.id for c in cases}
        if unknown:
            raise ManifestError(f"unknown case ids: {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c.id not in args.exclude]
        rows = dict(_load_row(spec) for spec in args.row)
        diffs = {
            c.id: (args.root / c.diff_path).read_text(encoding="utf-8") for c in cases
        }
    except (ArmsError, ManifestError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if (
        args.budget_usd <= 0
        or args.concurrency < 1
        or args.repeat < 0
        or (args.max_rpm is not None and args.max_rpm <= 0)
    ):
        print(
            "error: --budget-usd, --concurrency and --max-rpm must be positive, "
            "--repeat not negative",
            file=sys.stderr,
        )
        return 2
    try:
        result = run_judge(
            rows=rows,
            cases=cases,
            diffs=diffs,
            arm=arm,
            cache_dir=args.cache_dir,
            budget=Budget(args.budget_usd),
            repeat=args.repeat,
            concurrency=args.concurrency,
            max_rpm=args.max_rpm,
        )
    except BudgetExceeded as e:
        print(
            f"stopped: {e}. Paid verdicts are cached; rerun with a larger "
            "--budget-usd to finish.",
            file=sys.stderr,
        )
        return 3
    except CostUnknown as e:
        print(f"stopped: {e}", file=sys.stderr)
        return 3
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    brief = {k: v for k, v in result.items() if k != "verdicts"}
    print(json.dumps(brief, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
