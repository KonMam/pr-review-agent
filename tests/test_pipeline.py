"""The stages after generate (grade, dedup, suppress) and the truncation retry.

Same fixture project and stub server as `test_eval_stub.py`: two cases sharing
one diff whose bug is on store/load.go line 6. No network beyond loopback.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from stub_server import stub_server
from test_eval_stub import HIT, MISS, eval_args, findings_reply, write_arms

from pr_review.ask import MalformedReply
from pr_review.cli import main
from pr_review.grade import GRADE_PROMPT, parse_grades
from pr_review.models import Finding, LineRange
from pr_review.stages import Candidate, Stage, apply, dedup, suppress

STYLE = {**HIT, "start_line": 9, "end_line": 9, "category": "Style", "claim": "x"}

# A reasoning arm whose cut-off replies are retried with reasoning off.
REASONING_ARM = """
[arms.thinking]
base_url = "{url}"
api_key_env = "STUB_KEY_A"
model = "vendor/thinking-model"
price = {{ input_per_mtok = 1.0, output_per_mtok = 5.0 }}
extra_body = {{ provider = {{ data_collection = "deny" }} }}
truncation_extra_body = {{ reasoning = {{ enabled = false }} }}
"""


def is_grade(body: dict[str, Any]) -> bool:
    return body["messages"][0]["content"] == GRADE_PROMPT


def grades_reply(*scores: float) -> str:
    return json.dumps(
        {"grades": [{"index": i, "score": s} for i, s in enumerate(scores)]}
    )


def results(project: Path, run_id: str) -> tuple[dict[str, Any], list[Any]]:
    out = project / "results" / run_id
    summary = json.loads((out / "summary.json").read_text())
    rows = [
        json.loads(line) for line in (out / "reviews.jsonl").read_text().splitlines()
    ]
    return summary, rows


def run(project: Path, url: str, arm: str, run_id: str, *extra: str) -> int:
    arms = write_arms(project, url)
    arms.write_text(arms.read_text() + REASONING_ARM.format(url=url))
    args = eval_args(project, arms, arm, *extra)
    args[args.index("--run-id") + 1] = run_id
    return main(args)


def test_grade_drops_low_scores_and_ablation_adds_its_cost(project: Path) -> None:
    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if is_grade(body):
            return grades_reply(0.9, 0.2), {}
        return findings_reply(HIT, MISS), {}

    with stub_server(respond) as stub:
        assert run(project, stub.base_url, "cheap", "g", "--stages", "grade") == 0
    # 2 cases x 2 runs, one generate and one grade call each.
    assert len(stub.requests) == 8
    assert sum(is_grade(r) for r in stub.requests) == 4

    # The grader sees the PR and the candidates, never the ground truth.
    grade_req = next(r for r in stub.requests if is_grade(r))
    user = grade_req["messages"][1]["content"]
    assert "f, _ := os.Open(path)" in user and "### store/load.go" in user
    assert "os.Open error is discarded" in user and '"index": 1' in user
    for leak in ("store-001", "example.com/store", '"bugs"', "bug_class"):
        assert leak not in json.dumps(grade_req)

    summary, rows = results(project, "g")
    assert summary["stages"] == ["grade"]
    assert summary["precision"]["mean"] == 1.0
    assert summary["recall"]["mean"] == 1.0
    assert summary["grade"]["threshold"] == 0.3
    assert summary["grade"]["calls"] == 4
    assert summary["grade"]["reviews_graded"] == 4
    # Two calls per review at (1000 x $1 + 200 x $5) / 1M each.
    assert summary["cost_per_review_usd"] == pytest.approx(0.004)

    single, graded = summary["ablation"]
    assert single["row"] == "single-pass" and graded["row"] == "+ grade"
    assert single["precision"]["mean"] == 0.5 and graded["precision"]["mean"] == 1.0
    assert single["findings_per_review"] == 2 and graded["findings_per_review"] == 1
    assert single["cost_per_review_usd"] == pytest.approx(0.002)
    assert graded["cost_per_review_usd"] == pytest.approx(0.004)
    assert graded["latency_s"]["p50"] > single["latency_s"]["p50"]
    assert summary["findings_per_case"]["store-001"] == {
        "single-pass": 2,
        "+ grade": 1,
    }

    # Each candidate keeps its score, so the sweep needs no new calls.
    assert [c["score"] for c in rows[0]["candidates"]] == [0.9, 0.2]
    assert rows[0]["grade"]["scores"] == [0.9, 0.2]
    sweep = {r["threshold"]: r for r in summary["grade_sweep"]["rows"]}
    assert sweep[0.0]["precision"]["mean"] == 0.5
    assert sweep[0.3]["precision"]["mean"] == 1.0
    assert sweep[1.0]["findings_per_review"] == 0
    # 0.3-0.9 all give P = R = 1; the highest of them wins.
    assert summary["grade_sweep"]["best_f0_5"] == 0.9


def test_grade_arm_is_configurable_and_cached(project: Path) -> None:
    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if is_grade(body):
            return grades_reply(0.9, 0.2), {"cost": 0.001}
        return findings_reply(HIT, MISS), {}

    with stub_server(respond) as stub:
        url = stub.base_url
        args = ("--stages", "grade", "--grade-arm", "reported")
        assert run(project, url, "cheap", "a", *args) == 0
        grade_models = {r["model"] for r in stub.requests if is_grade(r)}
        gen_models = {r["model"] for r in stub.requests if not is_grade(r)}
        assert grade_models == {"other-vendor/other-model"}
        assert gen_models == {"vendor/cheap-model"}

        # Rerunning, or adding stages that need no model, costs nothing.
        assert run(project, url, "cheap", "b", *args) == 0
        assert run(project, url, "cheap", "c", "--stages", "grade,dedup,suppress",
                   "--grade-arm", "reported") == 0  # fmt: skip
        assert len(stub.requests) == 8
        # Single-pass reuses the generate cache; a new threshold needs no call.
        assert run(project, url, "cheap", "d") == 0
        assert run(project, url, "cheap", "e", *args, "--grade-threshold", "0.1") == 0
        assert len(stub.requests) == 8

    summary, _ = results(project, "c")
    assert summary["grade"]["model"] == "other-vendor/other-model"
    assert summary["cost_per_review_usd"] == pytest.approx(0.003)
    assert [r["row"] for r in summary["ablation"]] == [
        "single-pass",
        "+ grade",
        "+ grade + dedup",
        "+ grade + dedup + suppress",
    ]
    assert results(project, "e")[0]["precision"]["mean"] == 0.5


def test_no_candidates_means_no_grade_call(project: Path) -> None:
    with stub_server(lambda body: (findings_reply(), {})) as stub:
        assert run(project, stub.base_url, "cheap", "empty", "--stages", "grade") == 0
    assert len(stub.requests) == 4
    assert not any(is_grade(r) for r in stub.requests)
    summary, _ = results(project, "empty")
    assert summary["grade"]["calls"] == 0
    assert summary["cost_per_review_usd"] == pytest.approx(0.002)


def test_malformed_grade_drops_the_review_s_candidates(project: Path) -> None:
    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if is_grade(body):
            return grades_reply(0.9), {}  # one grade for two candidates
        return findings_reply(HIT, MISS), {}

    with stub_server(respond) as stub:
        assert run(project, stub.base_url, "cheap", "bad", "--stages", "grade") == 0
    # Each grade was retried once, with the error.
    assert sum(is_grade(r) for r in stub.requests) == 8
    retry = [r for r in stub.requests if is_grade(r) and len(r["messages"]) == 4]
    assert len(retry) == 4 and "no grade for findings [1]" in json.dumps(retry[0])
    summary, rows = results(project, "bad")
    assert summary["grade"]["malformed"] == 4
    assert summary["grade"]["calls"] == 8 and summary["grade"]["reviews_graded"] == 4
    assert all(r["findings"] == [] and r["grade"]["scores"] is None for r in rows)
    assert summary["per_run"][0]["findings"] == 0


def test_suppressed_categories_never_reach_stored_results(project: Path) -> None:
    with stub_server(lambda body: (findings_reply(HIT, STYLE), {})) as stub:
        assert run(project, stub.base_url, "cheap", "s", "--stages", "suppress") == 0
        assert run(project, stub.base_url, "cheap", "raw") == 0

    summary, rows = results(project, "s")
    assert summary["suppressed_category_findings"] == 0
    stored = [f["category"].lower() for r in rows for f in r["findings"]]
    assert stored and not {
        "style",
        "naming",
        "formatting",
        "docs",
        "readability",
    } & set(stored)
    single, suppressed = summary["ablation"]
    assert single["findings_per_review"] == 2
    assert suppressed["findings_per_review"] == 1
    assert suppressed["precision"]["mean"] == 1.0

    # Without the stage, the summary flags what got through.
    assert results(project, "raw")[0]["suppressed_category_findings"] == 4


def test_truncated_reply_retries_with_the_arm_s_fallback_body(project: Path) -> None:
    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if "reasoning" in body:
            return findings_reply(HIT), {}
        details = {"reasoning_tokens": 4096}
        return "", {
            "finish_reason": "length",
            "completion_tokens": 4096,
            "completion_tokens_details": details,
        }

    with stub_server(respond) as stub:
        assert run(project, stub.base_url, "thinking", "t", "--concurrency", "1") == 0
    assert len(stub.requests) == 8
    first, retry = stub.requests[0], stub.requests[1]
    assert "reasoning" not in first
    # Same messages, not an error turn; the arm's other body keys stay.
    assert retry["messages"] == first["messages"]
    assert retry["reasoning"] == {"enabled": False}
    assert retry["provider"] == {"data_collection": "deny"}
    summary, rows = results(project, "t")
    assert summary["malformed_reviews"] == 0
    assert summary["truncated_attempts"] == 4
    assert summary["reasoning_tokens"] == 4 * 4096
    assert summary["recall"]["mean"] == 1.0
    assert all(r["generate"]["attempts"] == 2 for r in rows)


def test_truncation_without_a_fallback_body_is_an_ordinary_retry(
    project: Path,
) -> None:
    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if len(body["messages"]) == 4:
            return findings_reply(HIT), {}
        return '{"findings": [', {"finish_reason": "length"}

    with stub_server(respond) as stub:
        assert run(project, stub.base_url, "cheap", "t2", "--concurrency", "1") == 0
    retry = stub.requests[1]
    assert retry["messages"][2] == {"role": "assistant", "content": '{"findings": ['}
    assert "reasoning" not in retry
    assert results(project, "t2")[0]["truncated_attempts"] == 4


def test_grade_options_without_grade_warn(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with stub_server(lambda body: (findings_reply(HIT), {})) as stub:
        assert (
            run(project, stub.base_url, "cheap", "w", "--grade-threshold", "0.8") == 0
        )
    assert "no effect without --stages grade" in capsys.readouterr().err
    assert not any(is_grade(r) for r in stub.requests)


def test_unknown_stage_is_a_usage_error(project: Path) -> None:
    with pytest.raises(SystemExit) as e:
        main(["eval", "--arm", "cheap", "--stages", "grade,lint"])
    assert e.value.code == 2


# --- The deterministic stages, on their own -----------------------------------


def finding(
    start: int, end: int, category: str = "nil_deref", file: str = "a.go"
) -> Finding:
    return Finding(
        file=file,
        lines=LineRange(start=start, end=end),
        category=category,
        claim="c",
        evidence="",
    )


def test_dedup_keeps_the_best_graded_of_an_overlapping_claim() -> None:
    a = Candidate(finding(10, 12), 0.6)
    b = Candidate(finding(12, 14), 0.9)  # overlaps a, same category
    c = Candidate(finding(13, 13, "data_race"), 0.5)  # overlaps, other claim
    d = Candidate(finding(20, 20), 0.7)  # same category, no overlap
    e = Candidate(finding(10, 12, file="b.go"), 0.7)  # other file
    assert dedup([a, b, c, d, e]) == [b, c, d, e]
    # Ungraded: the first one wins, and its range is not widened.
    x, y = Candidate(finding(5, 6)), Candidate(finding(6, 9))
    assert dedup([x, y]) == [x]


def test_dedup_matches_category_case_insensitively() -> None:
    a = Candidate(finding(1, 2, "Nil-Deref"))
    b = Candidate(finding(2, 3, "nil_deref"))
    assert dedup([a, b]) == [a]


def test_suppress_drops_every_suppressed_category() -> None:
    kept = Candidate(finding(1, 1, "other_bug"))
    found = [Candidate(finding(1, 1, c)) for c in ("style", "Naming", "formatting")]
    found += [Candidate(finding(1, 1, c)) for c in ("DOCS", " readability ")]
    assert suppress([*found, kept]) == [kept]


def test_apply_runs_stages_in_spec_order() -> None:
    # Dedup after grade: the low-graded duplicate is gone before dedup sees it.
    hi = Candidate(finding(1, 3), 0.8)
    lo = Candidate(finding(2, 4), 0.1)
    style = Candidate(finding(9, 9, "style"), 0.9)
    stages = [Stage.SUPPRESS, Stage.DEDUP, Stage.GRADE]
    assert apply([lo, hi, style], stages, 0.5) == [hi]
    assert apply([lo, hi, style], [], 0.5) == [lo, hi, style]
    # Ungraded candidates never pass the grade stage.
    assert apply([Candidate(finding(1, 1))], [Stage.GRADE], 0.0) == []


@pytest.mark.parametrize(
    ("content", "error"),
    [
        (grades_reply(0.5), r"no grade for findings \[1\]"),
        (grades_reply(0.5, 1.5), "less than or equal to 1"),
        ('{"grades": [{"index": 0, "score": 1}, {"index": 0, "score": 1}]}', "twice"),
        ('{"grades": [{"index": 2, "score": 1}]}', "index 2"),
        ("nope", "not JSON"),
    ],
)
def test_parse_grades_rejects_malformed(content: str, error: str) -> None:
    with pytest.raises(MalformedReply, match=error):
        parse_grades(content, 2)


def test_parse_grades_orders_by_index() -> None:
    content = '```json\n{"grades": [{"index": 1, "score": 0.2, "reason": "r"}, {"index": 0, "score": 1}]}\n```'
    assert parse_grades(content, 2) == [1.0, 0.2]
