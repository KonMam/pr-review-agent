import json

import pytest

from pr_review.evaluate import percentile
from pr_review.generate import MalformedReply, changed_base_paths, parse_reply
from pr_review.llm import Budget, BudgetExceeded

FINDING = {
    "file": "a.go",
    "start_line": 9,
    "end_line": 7,
    "category": "nil_deref",
    "claim": "c",
    "evidence": "e",
}


def test_parse_reply_accepts_fences_and_swapped_lines() -> None:
    [f] = parse_reply("```json\n" + json.dumps({"findings": [FINDING]}) + "\n```")
    assert (f.file, f.lines.start, f.lines.end) == ("a.go", 7, 9)


def test_parse_reply_empty_list_is_valid() -> None:
    assert parse_reply('{"findings": []}') == []


@pytest.mark.parametrize(
    "content",
    [
        "",
        "I found a bug on line 3",
        '{"issues": []}',
        json.dumps({"findings": [{**FINDING, "start_line": 0}]}),
        json.dumps({"findings": [{k: v for k, v in FINDING.items() if k != "file"}]}),
    ],
)
def test_parse_reply_rejects_malformed(content: str) -> None:
    with pytest.raises(MalformedReply):
        parse_reply(content)


def test_changed_base_paths_skips_new_files() -> None:
    diff = (
        "--- a/x.go\n+++ b/x.go\n@@ -1 +1 @@\n-a\n+b\n"
        "--- /dev/null\n+++ b/new.go\n@@ -0,0 +1 @@\n+c\n"
    )
    assert changed_base_paths(diff) == ["x.go"]


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 21)]
    assert percentile(values, 50) == 10
    assert percentile(values, 95) == 19
    assert percentile([3.0], 95) == 3.0


def test_budget_refuses_once_spent() -> None:
    budget = Budget(0.01)
    budget.check()
    budget.charge(0.01)
    with pytest.raises(BudgetExceeded):
        budget.check()
