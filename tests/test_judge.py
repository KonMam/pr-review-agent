"""The claim judge (item 7): reversing the diff, verdicts, caching, scoring.

Uses the stub-eval project (two cases sharing one diff, bug on store/load.go
line 6) and the stub OpenAI-compatible server. No network beyond loopback.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from stub_server import stub_server
from test_eval_stub import DIFF, HIT, MISS, write_arms

from pr_review.ask import MalformedReply
from pr_review.cli import main
from pr_review.coderabbit import detail
from pr_review.judge import (
    JUDGE_PROMPT,
    Item,
    Pacer,
    Verdict,
    agreement,
    load_agent_row,
    load_coderabbit_row,
    parse_verdict,
    reverse_diff,
    score_row,
)
from pr_review.manifest import load_manifest
from pr_review.models import Finding, LineRange


def finding(d: dict[str, Any]) -> dict[str, Any]:
    """A generate-reply finding dict in the stored `Finding` shape."""
    return {
        "file": d["file"],
        "lines": {"start": d["start_line"], "end": d["end_line"]},
        "category": d["category"],
        "claim": d["claim"],
        "evidence": d["evidence"],
    }


WRONG = {**HIT, "claim": "the deferred Close is redundant", "evidence": ""}


def write_reviews(project: Path, rows: dict[str, list[dict[str, Any]]]) -> Path:
    """A reviews.jsonl with run 0 of each case posting `rows[case]`."""
    lines = []
    for cid, found in rows.items():
        fs = [finding(f) for f in found]
        lines.append(
            json.dumps(
                {
                    "case_id": cid,
                    "run_index": 0,
                    "findings": fs,
                    "candidates": [{"finding": f, "score": 0.0} for f in fs],
                }
            )
        )
    p = project / "reviews.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


def respond_by_claim(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    user = body["messages"][1]["content"]
    same = "os.Open error is discarded" in user
    return json.dumps({"reason": "r", "same_defect": same}), {}


def judge_args(project: Path, arms: Path, reviews: Path, *extra: str) -> list[str]:
    return [
        "judge",
        "--arm",
        "cheap",
        "--arms-file",
        str(arms),
        "--manifest",
        str(project / "cases" / "manifest.jsonl"),
        "--root",
        str(project),
        "--cache-dir",
        str(project / "cache"),
        "--out",
        str(project / "out" / "judge.json"),
        "--row",
        f"a=agent:{reviews}",
        *extra,
    ]


def test_reverse_diff_swaps_sides_and_round_trips() -> None:
    rev = reverse_diff(DIFF)
    lines = rev.splitlines()
    assert lines[0] == "--- b/store/load.go" and lines[1] == "+++ a/store/load.go"
    assert lines[2] == "@@ -4,6 +4,9 @@"
    assert "+\tf, err := os.Open(path)" in lines
    assert "-\tf, _ := os.Open(path)" in lines
    assert " \tdefer f.Close()" in lines
    assert reverse_diff(rev) == DIFF


def test_parse_verdict() -> None:
    assert parse_verdict('{"same_defect": true, "reason": " ok "}') == (True, "ok")
    assert parse_verdict('```json\n{"same_defect": false}\n```') == (False, "")
    with pytest.raises(MalformedReply):
        parse_verdict('{"reason": "no verdict"}')
    with pytest.raises(MalformedReply):
        parse_verdict("yes")


def test_judge_sees_both_diffs_and_the_claim_only_for_located_findings(
    project: Path,
) -> None:
    reviews = write_reviews(project, {"store-001": [HIT, MISS], "store-002": [WRONG]})
    with stub_server(respond_by_claim) as stub:
        arms = write_arms(project, stub.base_url)
        assert main(judge_args(project, arms, reviews)) == 0
    # MISS is not near the bug, so only HIT and WRONG are judged.
    assert len(stub.requests) == 2
    req = stub.requests[0]
    assert req["messages"][0]["content"] == JUDGE_PROMPT
    user = req["messages"][1]["content"]
    assert "+\tf, _ := os.Open(path)" in user  # the buggy diff
    assert "+\tf, err := os.Open(path)" in user  # the fix (reversed)
    assert "Location: store/load.go, lines 6-6" in user
    for leak in ("store-001", "store-002", "bug_class", '"bugs"'):
        assert leak not in json.dumps(req)

    out = json.loads((project / "out" / "judge.json").read_text())
    row = out["rows"]["a"]
    assert (row["location_matched"], row["confirmed"]) == (2, 1)
    assert row["right_reason_rate"] == 0.5
    # Location: 2 of 3 findings hit, both bugs hit. Claim: only HIT counts.
    assert row["location"]["precision"] == pytest.approx(2 / 3)
    assert row["location"]["recall"] == 1.0
    assert row["claim"]["precision"] == pytest.approx(1 / 3)
    assert row["claim"]["recall"] == 0.5
    assert out["paid_calls"] == 2


def test_verdicts_are_cached_and_repeat_adds_a_second_sample(project: Path) -> None:
    reviews = write_reviews(project, {"store-001": [HIT], "store-002": [WRONG]})
    with stub_server(respond_by_claim) as stub:
        arms = write_arms(project, stub.base_url)
        assert main(judge_args(project, arms, reviews)) == 0
        assert len(stub.requests) == 2
        assert main(judge_args(project, arms, reviews)) == 0
        assert len(stub.requests) == 2  # all cached
        assert main(judge_args(project, arms, reviews, "--repeat", "1")) == 0
        assert len(stub.requests) == 3  # one second sample
    sc = json.loads((project / "out" / "judge.json").read_text())["self_consistency"]
    assert (sc["n"], sc["agreement"]) == (1, 1.0)


def test_malformed_verdict_is_not_confirmed(project: Path) -> None:
    reviews = write_reviews(project, {"store-001": [HIT], "store-002": []})
    with stub_server(lambda body: ("not json", {})) as stub:
        arms = write_arms(project, stub.base_url)
        assert main(judge_args(project, arms, reviews)) == 0
    assert len(stub.requests) == 2  # the one retry
    row = json.loads((project / "out" / "judge.json").read_text())["rows"]["a"]
    assert (row["confirmed"], row["malformed_verdicts"]) == (0, 1)
    assert row["claim"]["recall"] == 0.0


def test_bad_row_spec_is_an_error(project: Path) -> None:
    arms = write_arms(project, "http://127.0.0.1:9/v1")
    args = judge_args(project, arms, project / "x.jsonl")
    args[args.index("--row") + 1] = "a=bogus:x"
    assert main(args) == 2


def test_agent_row_without_grade_uses_candidates(tmp_path: Path) -> None:
    hit = finding(HIT)
    p = tmp_path / "r.jsonl"
    p.write_text(
        json.dumps(
            {
                "case_id": "c",
                "run_index": 1,
                "findings": [],
                "candidates": [
                    {"finding": hit, "score": 0.0},
                    {"finding": hit, "score": 0.0},  # a duplicate: dedup keeps one
                    {"finding": {**hit, "category": "style"}, "score": 1.0},
                ],
            }
        )
        + "\n"
    )
    assert load_agent_row(p) == {("c", 1): []}
    row = load_agent_row(p, without_grade=True)
    assert [it.finding.category for it in row[("c", 1)]] == ["ignored_err"]
    assert row[("c", 1)][0].text.endswith("Evidence: f, _ := os.Open(path)")


def test_coderabbit_row_reads_detail_and_filters_severity(tmp_path: Path) -> None:
    f = finding(HIT)
    p = tmp_path / "findings.json"
    p.write_text(
        json.dumps(
            {
                "c": [
                    {"severity": "Major", "finding": f, "detail": "Long text."},
                    {"severity": "Minor", "finding": f, "detail": ""},
                    {"severity": "Critical", "finding": None, "detail": "x"},
                ]
            }
        )
    )
    all_ = load_coderabbit_row(p, serious_only=False)[("c", 0)]
    assert [it.text for it in all_] == ["Long text.", HIT["claim"]]
    serious = load_coderabbit_row(p, serious_only=True)[("c", 0)]
    assert [it.text for it in serious] == ["Long text."]


def test_score_row_clean_findings_stay_false_positives(project: Path) -> None:
    cases = {c.id: c for c in load_manifest(project / "cases" / "manifest.jsonl")}
    f = Finding(
        file="store/load.go",
        lines=LineRange(start=6, end=6),
        category="x",
        claim="c",
        evidence="",
    )
    it = Item("store-001", 0, f, "c")
    yes = Verdict(
        same_defect=True,
        reason="",
        attempts=1,
        truncated=0,
        prompt_tokens=0,
        completion_tokens=0,
        reasoning_tokens=0,
        cost_usd=0.0,
        latency_s=0.0,
    )
    row = score_row(
        {("store-001", 0): [it], ("store-001", 1): []}, cases, {it.key: yes}
    )
    assert row["runs"] == 2
    # Run 0 finds the bug in store-001; store-002 is missed both runs.
    assert row["claim"]["recall"] == pytest.approx((0.5 + 0.0) / 2)
    assert row["right_reason_rate"] == 1.0


def test_agreement_and_kappa() -> None:
    a = agreement([(True, True), (False, False), (True, False), (True, True)])
    assert a["n"] == 4 and a["agreement"] == 0.75
    assert a["kappa"] == pytest.approx(0.5)
    assert agreement([])["n"] == 0
    assert agreement([(True, True)])["kappa"] == 1.0


def test_pacer_spaces_calls() -> None:
    t = [100.0]
    p = Pacer(30, clock=lambda: t[0])  # one call every 2 s
    assert [p.delay(), p.delay(), p.delay()] == [0.0, 2.0, 4.0]
    t[0] = 110.0
    assert p.delay() == 0.0
    assert Pacer(None).delay() == 0.0


def test_detail_keeps_prose_and_drops_collapsed_blocks() -> None:
    body = (
        "<!-- coderabbit-ai-generated:v1 -->\n\n"
        "**Major** · **Stability & Availability** · **Quick win**\n\n"
        "**Keep the index non-negative.**\n\n"
        "It panics on 32-bit. ([go.dev](https://go.dev/ref/spec)) See "
        "[the spec](https://go.dev).\n\n"
        "<details>\n<summary>Proposed fix</summary>\n\n```diff\n-a\n+b\n```\n"
        "<details><summary>inner</summary>x</details>\nstill hidden\n</details>\n"
        "---\n<!-- end -->\n"
    )
    assert detail(body) == (
        "Keep the index non-negative. It panics on 32-bit.  See the spec."
    )
