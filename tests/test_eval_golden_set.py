"""End to end on the real golden set, offline: `pr-review eval` against a stub.

The stub echoes each case's ground truth as the model's findings, so the run
must score P = R = 1. That pins the joins between the parts built separately:
the manifest, each case's `buggy.diff` and `base/` files (read with no clone
and no network), the prompt, reply parsing and the scorer.

A second test pins the line-number convention on the real cases: every
ground-truth range sits on lines `buggy.diff` touches on its new (buggy) side,
which is the numbering the prompt asks the model for.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from stub_server import stub_server

from pr_review.cli import main
from pr_review.manifest import load_manifest
from pr_review.models import Case

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "cases" / "manifest.jsonl"
CASES = load_manifest(MANIFEST)

ARMS_TOML = """\
[arms.stub]
base_url = "{url}"
api_key_env = "STUB_KEY"
model = "stub/echo-truth"
price = {{ input_per_mtok = 1.0, output_per_mtok = 5.0 }}
"""


def truth_reply(case: Case) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "file": b.file,
                    "start_line": b.lines.start,
                    "end_line": b.lines.end,
                    "category": case.bug_class.value,
                    "claim": "echoed ground truth",
                }
                for b in case.bugs
            ]
        }
    )


def test_offline_eval_echoing_truth_scores_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diffs = {c.id: (ROOT / c.diff_path).read_text(encoding="utf-8") for c in CASES}
    assert len(set(diffs.values())) == len(CASES), "two cases share a diff"

    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        user = body["messages"][-1]["content"]
        (case,) = [c for c in CASES if diffs[c.id] in user]
        return truth_reply(case), {}

    monkeypatch.setenv("STUB_KEY", "k")
    with stub_server(respond) as stub:
        arms = tmp_path / "arms.toml"
        arms.write_text(ARMS_TOML.format(url=stub.base_url))
        argv = ["eval", "--arm", "stub", "--runs", "1", "--arms-file", str(arms),
                "--manifest", str(MANIFEST), "--root", str(ROOT),
                "--cache-dir", str(tmp_path / "cache"),
                "--results-dir", str(tmp_path / "results"), "--run-id", "echo"]  # fmt: skip
        assert main(argv) == 0

    assert len(stub.requests) == len(CASES)
    summary = json.loads((tmp_path / "results" / "echo" / "summary.json").read_text())
    assert summary["cases"] == len(CASES) == len(summary["case_ids"])
    assert summary["malformed_reviews"] == 0
    assert summary["precision"]["mean"] == 1.0
    assert summary["recall"]["mean"] == 1.0
    # Every base file the diff changes reached the model.
    for body in stub.requests:
        user = body["messages"][-1]["content"]
        for path in re.findall(r"^--- a/(\S+)$", user, flags=re.MULTILINE):
            assert f"### {path}" in user, path


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def touched_buggy_lines(diff: str) -> dict[str, set[int]]:
    """Per file, buggy-side lines a diff adds, plus both neighbours of a deletion."""
    out: dict[str, set[int]] = {}
    file = ""
    new = 0
    for line in diff.splitlines():
        if line.startswith("--- "):
            continue  # a file header, not a deleted line
        if line.startswith("+++ "):
            file = line[4:].removeprefix("b/")
        elif m := _HUNK.match(line):
            new = int(m.group(1))
        elif line.startswith("+"):
            out.setdefault(file, set()).add(new)
            new += 1
        elif line.startswith("-"):
            out.setdefault(file, set()).update({new - 1, new})
        elif line.startswith(" "):
            new += 1
    return out


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_truth_is_on_lines_the_diff_touches_buggy_side(case: Case) -> None:
    touched = touched_buggy_lines((ROOT / case.diff_path).read_text(encoding="utf-8"))
    for bug in case.bugs:
        lines = touched.get(bug.file, set())
        # Merged clusters may span untouched lines; both ends must be touched.
        assert bug.lines.start in lines, (bug, sorted(lines))
        assert bug.lines.end in lines, (bug, sorted(lines))
