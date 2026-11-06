"""The CodeRabbit arm (item 6): sandbox PRs, review collection, normalising, scoring.

`python -m pr_review.coderabbit <command>`:

- `build`: build the sandbox branches locally (`sandbox.py`) and check them for
  leakage. Writes `cases/coderabbit-map.json` (case id ↔ number). No network.
- `push`: push every branch to the sandbox repo. Refuses on any leakage problem.
- `open-prs`: open one PR per case, `case/case-NNN` into `base/case-NNN`.
- `trigger`: wait for each PR's review and record when it landed. A PR whose
  review was rate-limited gets `@coderabbitai review` after the wait, at most
  `MAX_REQUESTS` times.
- `collect`: save each PR's review comments, reviews and issue comments as raw
  JSON under `results/coderabbit/raw/`.
- `score`: normalise and score with the shared scorer, two ways (all comments,
  Major or Critical only), into `results/coderabbit/summary.json`.

Only `build` and `score` and the pure functions below run in tests; the rest
call `gh` and touch only `sandbox.SANDBOX_REPO`.

**Normalising.** A finding is one top-level inline review comment by the bot,
at its RIGHT-side line range (`start_line`..`line`; the `original_*` fields if
the comment is outdated), plus each located entry in the review body
(`` `path:12-15` ``, as in "Outside diff range comments"). The walkthrough /
summary issue comment is not a finding, nor is a review-body entry without
a severity badge. Labels are CodeRabbit's own badge line: severity,
category and effort (`Major · Stability & Availability · Quick win`). It has
no "Potential issue" / "Nitpick" labels any more, so the narrow cut is
severity Major or Critical. A comment with no line (file-level) has no
location to score, so it is counted but not scored.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pr_review import sandbox
from pr_review.manifest import load_manifest
from pr_review.models import Case, CaseKind, Finding, LineRange
from pr_review.scoring import Score, score_case

BOT_LOGIN = "coderabbitai[bot]"
TRIGGER = "@coderabbitai review"
MAX_REQUESTS = 3  # review requests per rate-limited PR before giving up
SERIOUS = {"Critical", "Major"}
UNVERIFIED = "Unverified finding"
MAP_PATH = Path("cases/coderabbit-map.json")
OUT_DIR = Path("results/coderabbit")

# Review-body sections that repeat comments already counted.
SKIP_SECTIONS = ("Duplicate comments",)
_HTML_RE = re.compile(r"<[^>]+>")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LOC_RE = re.compile(r"^`([^`\s:]+):(\d+)(?:-(\d+))?`$")
_LINES_RE = re.compile(r"^`(\d+)(?:-(\d+))?`:")
_SECTION_RE = re.compile(r"([A-Z][A-Za-z ]+comments?)\s*(?:\(\d+\)|:\s*\d+)")


@dataclass(frozen=True)
class Comment:
    """One CodeRabbit comment, normalised: a finding plus CodeRabbit's own labels.

    CodeRabbit (2026-11) heads each comment with a badge line of severity,
    category and effort, e.g. `Major · Stability & Availability · Quick win`.
    """

    finding: Finding | None  # None: no line to score (file-level comment)
    severity: str  # Critical / Major / Minor / ..., "" if none
    category: str  # e.g. "Functional Correctness", "" if none
    tags: tuple[str, ...]  # the rest of the badge line (effort, tier)
    source: str  # "inline" or the review-body section
    detail: str = ""  # the comment's prose, for the claim judge (`judge.py`)

    @property
    def serious(self) -> bool:
        """Major or Critical: the closest thing to the old "Potential issue" label."""
        return self.severity in SERIOUS


def _plain(line: str) -> str:
    """A markdown line without HTML tags or a blockquote prefix."""
    return _HTML_RE.sub("", line).lstrip("> ").strip()


def badge(body: str) -> tuple[str, str, tuple[str, ...]]:
    """(severity, category, other tags) from a comment's badge line, if any."""
    for raw in body.splitlines():
        line = _plain(raw)
        if "·" in line and line.startswith("**"):
            parts = [p.strip().strip("*").strip() for p in line.split("·")]
            parts = [p for p in parts if p]
            if parts:
                cat = parts[1] if len(parts) > 1 else ""
                return parts[0], cat, tuple(parts[2:])
    return "", "", ()


def claim(body: str) -> str:
    """The comment's title: its first bold line that isn't the badge line."""
    for raw in body.splitlines():
        line = _plain(raw)
        if "·" in line or not line.startswith("**"):
            continue
        m = _BOLD_RE.match(line)
        if m and m.group(1).strip() and m.group(1).strip() != UNVERIFIED:
            return m.group(1).strip()
    for raw in body.splitlines():
        line = _plain(raw)
        if line and "·" not in line and not line.startswith("<!--"):
            return line[:200]
    return "(no text)"


DETAIL_MAX_CHARS = 1500
_LINK_RE = re.compile(r"\(?\[([^\]]*)\]\([^)]*\)\)?")


def detail(body: str) -> str:
    """A comment's prose: its title and explanation, without the badge line,
    HTML comments or collapsed `<details>` blocks (proposed fix, committable
    suggestion, AI-agent prompt). Links keep their text only when it is not a
    bare citation in parentheses. Capped at `DETAIL_MAX_CHARS`.
    """
    out: list[str] = []
    depth = 0
    for raw in body.splitlines():
        opened, closed = raw.count("<details"), raw.count("</details>")
        if depth or opened:
            depth = max(0, depth + opened - closed)
            continue
        if raw.lstrip().startswith("<!--"):
            continue
        line = _plain(raw)
        if not line or "·" in line or not line.strip("-"):
            continue
        line = _LINK_RE.sub(
            lambda m: "" if m.group(0).startswith("(") else m.group(1), line
        )
        out.append(line.replace("**", "").strip())
    text = " ".join(t for t in out if t)
    return text[:DETAIL_MAX_CHARS]


def _finding(file: str, lo: int, hi: int, category: str, text: str) -> Finding:
    lo, hi = sorted((lo, hi))
    return Finding(
        file=file,
        lines=LineRange(start=lo, end=hi),
        category=category or "unlabelled",
        claim=claim(text),
        evidence="",
    )


def normalise_inline(c: Mapping[str, Any]) -> Comment | None:
    """A top-level bot review comment → `Comment`; replies and others → None."""
    if c.get("user", {}).get("login") != BOT_LOGIN or c.get("in_reply_to_id"):
        return None
    body = c.get("body") or ""
    severity, category, tags = badge(body)
    # Current position if the comment still applies, else where it was made.
    prefix = "" if c.get("line") is not None else "original_"
    end = c.get(f"{prefix}line")
    start = c.get(f"{prefix}start_line") or end
    finding = None
    if end is not None and start is not None:
        finding = _finding(c["path"], int(start), int(end), category, body)
    return Comment(finding, severity, category, tags, "inline", detail(body))


def normalise_review_body(body: str) -> list[Comment]:
    """Located entries in a review body, such as "Outside diff range comments".

    Two entry forms occur: a `` `path:12-15` `` line, or a `` `12-15`: ...``
    line under a `<summary>path-12-15: 1</summary>` heading. The entry runs to
    the next entry or section. An entry counts only if it carries a severity
    badge; remarks without one are not findings, and "Duplicate comments"
    repeat earlier ones. "Unverified finding" is kept as a tag.
    """
    out: list[Comment] = []
    section = ""
    last_file = ""
    entry: tuple[str, int, int, str, list[str]] | None = None

    def flush() -> None:
        nonlocal entry
        if entry is not None:
            file, lo, hi, sec, lines = entry
            text = "\n".join(lines)
            severity, category, tags = badge(text)
            if UNVERIFIED in text:
                tags = (*tags, UNVERIFIED)
            if severity and not sec.startswith(SKIP_SECTIONS):
                f = _finding(file, lo, hi, category, text)
                out.append(Comment(f, severity, category, tags, sec, detail(text)))
        entry = None

    for raw in body.splitlines():
        line = _plain(raw)
        sm = _SECTION_RE.search(line)
        if sm and ("<summary>" in raw or line.startswith("**")) and "·" not in line:
            flush()
            section = sm.group(1).strip()
            continue
        if "<summary>" in raw:
            name = re.sub(r"(:\s*\d+|\s*\(\d+\))$", "", line).strip("` ")
            name = re.sub(r"(-\d+){1,2}$", "", name)
            if "." in name and " " not in name:
                last_file = name
            continue
        lm = _LOC_RE.match(line)
        em = _LINES_RE.match(line)
        if lm or (em and last_file):
            flush()
            if lm:
                file, lo, hi, rest = lm.group(1), lm.group(2), lm.group(3), ""
            else:
                assert em is not None
                rest = _LINES_RE.sub("", _HTML_RE.sub("", raw).strip(), count=1).strip()
                file, lo, hi = last_file, em.group(1), em.group(2)
            start = int(lo)
            sec = section or "review body"
            entry = (file, start, int(hi or start), sec, [rest] if rest else [])
            continue
        if entry is not None:
            entry[4].append(raw.lstrip("> "))
    flush()
    return out


def normalise_pr(
    review_comments: Sequence[Mapping[str, Any]], reviews: Sequence[Mapping[str, Any]]
) -> list[Comment]:
    """Every CodeRabbit comment on one PR, inline and review-body."""
    out = [c for rc in review_comments if (c := normalise_inline(rc)) is not None]
    for r in reviews:
        if r.get("user", {}).get("login") == BOT_LOGIN:
            out += normalise_review_body(r.get("body") or "")
    return out


# --- scoring ----------------------------------------------------------------


def score_arm(
    cases: Sequence[Case],
    comments: Mapping[str, Sequence[Comment]],
    keep: Callable[[Comment], bool],
) -> dict[str, Any]:
    """Score one filter of CodeRabbit's comments with the shared scorer."""
    combined, bug, clean = Score(), Score(), Score()
    unlocated = 0
    for case in cases:
        kept = [c for c in comments.get(case.id, []) if keep(c)]
        unlocated += sum(1 for c in kept if c.finding is None)
        s = score_case([c.finding for c in kept if c.finding is not None], case.bugs)
        combined += s
        if case.kind is CaseKind.BUG:
            bug += s
        else:
            clean += s
    return {
        "cases": len(cases),
        "combined": _score_dict(combined),
        "bug_only": _score_dict(bug),
        "clean_only": _score_dict(clean),
        "fp_per_clean_review": combined.fp_per_clean_review,
        "clean_flagged": combined.flagged_clean_share,
        "unlocated_comments": unlocated,
    }


def _score_dict(s: Score) -> dict[str, Any]:
    return {
        "precision": s.precision,
        "recall": s.recall,
        "f1": s.f1,
        "findings": s.findings,
        "matched_findings": s.matched_findings,
        "bugs": s.bugs,
        "matched_bugs": s.matched_bugs,
    }


def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, max(0, round(q / 100 * (len(v) - 1))))]


def latency_s(triggered_at: str, reviewed_at: str) -> float:
    return (_ts(reviewed_at) - _ts(triggered_at)).total_seconds()


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def summarise(
    cases: Sequence[Case],
    comments: Mapping[str, Sequence[Comment]],
    latencies: Sequence[float],
) -> dict[str, Any]:
    severity: dict[str, int] = {}
    category: dict[str, int] = {}
    for cs in comments.values():
        for c in cs:
            severity[c.severity or "none"] = severity.get(c.severity or "none", 0) + 1
            category[c.category or "none"] = category.get(c.category or "none", 0) + 1
    return {
        "runs": 1,
        "all_comments": score_arm(cases, comments, lambda c: True),
        "major_or_critical": score_arm(cases, comments, lambda c: c.serious),
        "severity": dict(sorted(severity.items(), key=lambda kv: -kv[1])),
        "category": dict(sorted(category.items(), key=lambda kv: -kv[1])),
        "latency_s": {
            "n": len(latencies),
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
        },
    }


# --- gh helpers (network; only ever the sandbox repo) -----------------------


def _gh(*args: str, input_: str | None = None) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True, input=input_
    ).stdout


def _api(path: str, paginate: bool = True) -> Any:
    args = ["api", f"repos/{sandbox.SANDBOX_REPO}/{path}"]
    if paginate:
        args += ["--paginate", "--slurp"]
        pages = json.loads(_gh(*args))
        return [item for page in pages for item in page]
    return json.loads(_gh(*args))


def _load_map() -> dict[str, int]:
    data: dict[str, int] = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    return data


def _state(name: str) -> dict[str, Any]:
    p = OUT_DIR / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _save_state(name: str, data: Mapping[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / name).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def cmd_build(args: argparse.Namespace) -> int:
    cases = load_manifest(args.manifest)
    numbers = sandbox.assign_numbers([c.id for c in cases])
    sandbox.build_sandbox(
        cases,
        numbers,
        args.snapshots_dir,
        Path("."),
        Path("cases/licences"),
        sandbox.readme_text(cases),
        args.git_dir,
        args.scratch,
    )
    MAP_PATH.write_text(
        json.dumps(dict(sorted(numbers.items(), key=lambda kv: kv[1])), indent=2) + "\n"
    )
    problems = sandbox.check_repo(args.git_dir, cases, numbers)
    for p in problems:
        print(f"leak: {p}", file=sys.stderr)
    print(f"built {len(cases)} cases in {args.git_dir}; {len(problems)} problems")
    return 1 if problems else 0


def cmd_push(args: argparse.Namespace) -> int:
    cases = load_manifest(args.manifest)
    numbers = _load_map()
    problems = sandbox.check_repo(args.git_dir, cases, numbers)
    if problems:
        print("refusing to push: leakage check failed", file=sys.stderr)
        return 1
    url = f"git@github.com:{sandbox.SANDBOX_REPO}.git"
    subprocess.run(
        [
            "git",
            "--git-dir",
            str(args.git_dir),
            "push",
            "--force",
            url,
            "refs/heads/*:refs/heads/*",
        ],
        check=True,
    )
    return 0


def cmd_open_prs(args: argparse.Namespace) -> int:
    numbers = _load_map()
    prs = _state("prs.json")
    for n in sorted(numbers.values()):
        lab = sandbox.label(n)
        if lab in prs:
            continue
        out = _gh(
            "pr",
            "create",
            "--repo",
            sandbox.SANDBOX_REPO,
            "--base",
            f"base/{lab}",
            "--head",
            f"case/{lab}",
            "--title",
            sandbox.pr_title(n),
            "--body",
            sandbox.PR_BODY,
        )
        number = int(out.strip().rsplit("/", 1)[-1])
        pr = _api(f"pulls/{number}", paginate=False)
        prs[lab] = {"number": number, "created_at": pr["created_at"]}
        _save_state("prs.json", prs)
        print(f"{lab}: PR #{number}")
        time.sleep(args.pause)
    return 0


def check_prs() -> list[str]:
    """Title and body of every opened sandbox PR against the templates."""
    problems = []
    numbers = _load_map()
    for pr in _api("pulls?state=all&per_page=100"):
        n = int(pr["title"].split()[-1]) if pr["title"].startswith("Case ") else -1
        problems += sandbox.check_pr_text(n, pr["title"], pr["body"] or "")
    if len(numbers) != len(_state("prs.json")):
        problems.append("not every case has a PR")
    return problems


_WAIT_RE = re.compile(r"wait \*\*(?:(\d+) minutes?)?(?: and )?(?:(\d+) seconds?)?\*\*")


def rate_limit_wait(body: str) -> int | None:
    """Seconds CodeRabbit asks to wait, from its rate-limit comment; None if not one."""
    if "rate limit" not in body.lower():
        return None
    m = _WAIT_RE.search(body)
    if not m:
        return 3600
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def review_state(
    check_runs: Sequence[Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
    since: str,
) -> tuple[str, str | None]:
    """("done", when) / ("rate_limited", None) / ("pending", None) for one PR.

    CodeRabbit marks each review with a "CodeRabbit" check run. It is finished
    when that run completes; a rate-limited review completes too, titled "Review
    rate limited". A review with nothing to say posts no PR review at all, so
    "when" is the bot's first review after `since` if there is one, else the
    check run's completion.
    """
    runs = [
        r
        for r in check_runs
        if r.get("name") == "CodeRabbit" and (r.get("started_at") or "") >= since
    ]
    if not runs:
        return "pending", None
    run = max(runs, key=lambda r: r["started_at"])
    if run.get("status") != "completed":
        return "pending", None
    if "rate limit" in ((run.get("output") or {}).get("title") or "").lower():
        return "rate_limited", None
    posted = sorted(
        r["submitted_at"]
        for r in reviews
        if r.get("user", {}).get("login") == BOT_LOGIN
        and (r.get("submitted_at") or "") >= since
    )
    return "done", posted[0] if posted else run.get("completed_at")


def _review_state(number: int, since: str) -> tuple[str, str | None]:
    pr = _api(f"pulls/{number}", paginate=False)
    runs = _api(f"commits/{pr['head']['sha']}/check-runs", paginate=False)
    return review_state(
        runs.get("check_runs", []), _api(f"pulls/{number}/reviews"), since
    )


def _rate_limit_wait(number: int, since: str) -> int:
    for c in _api(f"issues/{number}/comments"):
        if c["user"]["login"] == BOT_LOGIN and c["updated_at"] >= since:
            w = rate_limit_wait(c["body"] or "")
            if w is not None:
                return w
    return 3600


def cmd_trigger(args: argparse.Namespace) -> int:
    """Record each PR's review; post a review request on rate-limited ones.

    Latency runs from PR open for a review that ran on open, and from the
    request comment for one that had to be asked for again.
    """
    prs = _state("prs.json")
    trig = _state("triggers.json")
    for lab in sorted(prs):
        if args.only and lab not in args.only:
            continue
        if trig.get(lab, {}).get("reviewed_at"):
            continue
        number = prs[lab]["number"]
        since = trig.get(lab, {}).get("triggered_at", prs[lab]["created_at"])
        auto = lab not in trig
        deadline = time.time() + 1800  # stop waiting on one PR after 30 min
        while True:
            state, when = _review_state(number, since)
            if state == "pending" and time.time() < deadline:
                if args.no_trigger:
                    break
                time.sleep(30)
                continue
            if state == "done":
                trig[lab] = {
                    "number": number,
                    "triggered_at": since,
                    "reviewed_at": when,
                    "auto": auto,
                }
                _save_state("triggers.json", trig)
                print(f"{lab}: reviewed at {when}", flush=True)
                break
            if args.no_trigger or state == "pending":
                print(f"{lab}: {state}", flush=True)
                break
            requests = trig.get(lab, {}).get("requests", 0)
            if requests >= MAX_REQUESTS:
                print(
                    f"{lab}: still rate limited after {requests} requests", flush=True
                )
                break
            wait = _rate_limit_wait(number, since)
            print(f"{lab}: rate limited, waiting {wait + 60}s", flush=True)
            time.sleep(wait + 60)
            out = _gh(
                "api",
                f"repos/{sandbox.SANDBOX_REPO}/issues/{number}/comments",
                "-f",
                f"body={TRIGGER}",
            )
            since = json.loads(out)["created_at"]
            auto = False
            deadline = time.time() + 1800
            trig[lab] = {
                "number": number,
                "triggered_at": since,
                "requests": requests + 1,
            }
            _save_state("triggers.json", trig)
            print(f"{lab}: requested review at {since}", flush=True)
            time.sleep(60)
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    prs = _state("prs.json")
    for lab, pr in sorted(prs.items()):
        d = OUT_DIR / "raw" / lab
        d.mkdir(parents=True, exist_ok=True)
        n = pr["number"]
        for name, path in [
            ("review_comments", f"pulls/{n}/comments"),
            ("reviews", f"pulls/{n}/reviews"),
            ("issue_comments", f"issues/{n}/comments"),
        ]:
            (d / f"{name}.json").write_text(json.dumps(_api(path), indent=2) + "\n")
    print(f"collected {len(prs)} PRs into {OUT_DIR / 'raw'}")
    return 0


def load_comments(
    raw_dir: Path, numbers: Mapping[str, int]
) -> dict[str, list[Comment]]:
    out: dict[str, list[Comment]] = {}
    for cid, n in numbers.items():
        d = raw_dir / sandbox.label(n)
        if not d.is_dir():
            continue
        rc = json.loads((d / "review_comments.json").read_text(encoding="utf-8"))
        rv = json.loads((d / "reviews.json").read_text(encoding="utf-8"))
        out[cid] = normalise_pr(rc, rv)
    return out


def cmd_score(args: argparse.Namespace) -> int:
    cases = load_manifest(args.manifest)
    unknown = set(args.exclude) - {c.id for c in cases}
    if unknown:
        print(f"error: unknown case ids: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    numbers = _load_map()
    comments = load_comments(OUT_DIR / "raw", numbers)
    reviewed = [c for c in cases if c.id in comments and c.id not in args.exclude]
    trig = _state("triggers.json")
    kept = {sandbox.label(numbers[c.id]) for c in reviewed}
    lat = [
        latency_s(t["triggered_at"], t["reviewed_at"])
        for lab, t in trig.items()
        if lab in kept and t.get("reviewed_at")
    ]
    summary = summarise(reviewed, comments, lat)
    summary["excluded_cases"] = sorted(args.exclude)
    summary["missing_cases"] = sorted(c.id for c in cases if c.id not in comments)
    findings = {
        cid: [
            {
                "severity": c.severity,
                "category": c.category,
                "tags": list(c.tags),
                "source": c.source,
                "finding": c.finding.model_dump() if c.finding else None,
                "detail": c.detail,
            }
            for c in cs
        ]
        for cid, cs in sorted(comments.items())
    }
    _save_state("findings.json", findings)
    _save_state(args.out, summary)
    print(json.dumps(summary, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m pr_review.coderabbit")
    p.add_argument("--manifest", type=Path, default=Path("cases/manifest.jsonl"))
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--snapshots-dir", type=Path, default=Path("cache/snapshots"))
    b.add_argument("--git-dir", type=Path, default=Path("cache/sandbox.git"))
    b.add_argument("--scratch", type=Path, default=Path("cache/sandbox-work"))
    pu = sub.add_parser("push")
    pu.add_argument("--git-dir", type=Path, default=Path("cache/sandbox.git"))
    o = sub.add_parser("open-prs")
    o.add_argument("--pause", type=float, default=3.0)
    sub.add_parser("check-prs")
    t = sub.add_parser("trigger")
    t.add_argument("--only", nargs="*", default=None, metavar="case-NNN")
    t.add_argument(
        "--no-trigger",
        action="store_true",
        help="only record reviews that already finished; post nothing",
    )
    sub.add_parser("collect")
    sc = sub.add_parser("score")
    sc.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="ID",
        help="leave this case id out of the scores (repeatable)",
    )
    sc.add_argument(
        "--out", default="summary.json", help="file name under results/coderabbit/"
    )
    args = p.parse_args(argv)
    if args.command == "check-prs":
        problems = check_prs()
        for prob in problems:
            print(prob, file=sys.stderr)
        print(f"{len(problems)} problems")
        return 1 if problems else 0
    cmds: dict[str, Callable[[argparse.Namespace], int]] = {
        "build": cmd_build,
        "push": cmd_push,
        "open-prs": cmd_open_prs,
        "trigger": cmd_trigger,
        "collect": cmd_collect,
        "score": cmd_score,
    }
    return cmds[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
