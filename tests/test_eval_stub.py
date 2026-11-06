"""Acceptance test: the eval pipeline against a stub OpenAI-compatible server.

Two arms differ only in `arms.toml` (model id, key env var, cost source), and
the same Python runs both. Fixture cases use the item-1 manifest format and
the golden set's layout: base files in `base/` next to the diff, no clone.
"""

import json
import random
import threading
import time
from pathlib import Path
from typing import Any

import openai
import pytest
from stub_server import StubError, stub_server

from pr_review import llm
from pr_review.arms import load_arm
from pr_review.cli import main
from pr_review.evaluate import run_eval
from pr_review.llm import Budget, BudgetExceeded
from pr_review.manifest import load_manifest

BASE_GO = """\
package store

import "os"

func Load(path string) ([]byte, error) {
\tf, err := os.Open(path)
\tif err != nil {
\t\treturn nil, err
\t}
\tdefer f.Close()
\treturn readAll(f)
}
"""

DIFF = """\
--- a/store/load.go
+++ b/store/load.go
@@ -4,9 +4,6 @@

 func Load(path string) ([]byte, error) {
-\tf, err := os.Open(path)
-\tif err != nil {
-\t\treturn nil, err
-\t}
+\tf, _ := os.Open(path)
 \tdefer f.Close()
 \treturn readAll(f)
 }
"""

ARMS_TOML = """\
[arms.cheap]
base_url = "{url}"
api_key_env = "STUB_KEY_A"
model = "vendor/cheap-model"
price = {{ input_per_mtok = 1.0, output_per_mtok = 5.0 }}

[arms.reported]
base_url = "{url}"
api_key_env = "STUB_KEY_B"
model = "other-vendor/other-model"
reports_cost = true
json_mode = false
extra_body = {{ provider = {{ data_collection = "deny" }} }}
extra_headers = {{ HTTP-Referer = "https://example.com/app", X-OpenRouter-Title = "stub-app" }}

# Worst case per call is exactly $0.10: input is free, 1000 output tokens max.
[arms.tight]
base_url = "{url}"
api_key_env = "STUB_KEY_A"
model = "vendor/tight-model"
price = {{ input_per_mtok = 0.0, output_per_mtok = 100.0 }}
max_tokens = 1000
"""

HIT = {
    "file": "store/load.go",
    "start_line": 6,
    "end_line": 6,
    "category": "ignored_err",
    "claim": "os.Open error is discarded, f may be nil",
    "evidence": "f, _ := os.Open(path)",
}
MISS = {**HIT, "start_line": 40, "end_line": 41, "claim": "unrelated"}


FIX_SHA = "0" * 40
CASE_DIR = "cases/store-001"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with two fixture cases sharing one diff and its base files."""
    case_dir = tmp_path / CASE_DIR
    (case_dir / "base" / "store").mkdir(parents=True)
    (case_dir / "base" / "store" / "load.go").write_text(BASE_GO)
    (case_dir / "buggy.diff").write_text(DIFF)
    lines = []
    for case_id in ("store-001", "store-002"):
        lines.append(
            json.dumps(
                {
                    "id": case_id,
                    "provenance": {
                        "repo": "example.com/store",
                        "fix_commit": FIX_SHA,
                        "licence": "MIT",
                    },
                    "base_ref": FIX_SHA,
                    "diff_path": f"{CASE_DIR}/buggy.diff",
                    "bug_class": "ignored_err",
                    "bugs": [
                        {"file": "store/load.go", "lines": {"start": 6, "end": 6}}
                    ],
                }
            )
        )
    (tmp_path / "cases" / "manifest.jsonl").write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("STUB_KEY_A", "key-a")
    monkeypatch.setenv("STUB_KEY_B", "key-b")
    return tmp_path


def write_arms(project: Path, url: str) -> Path:
    path = project / "arms.toml"
    path.write_text(ARMS_TOML.format(url=url))
    return path


def findings_reply(*findings: dict[str, Any]) -> str:
    return json.dumps({"findings": list(findings)})


def eval_args(project: Path, arms: Path, arm: str, *extra: str) -> list[str]:
    return [
        "eval",
        "--arm",
        arm,
        "--runs",
        "2",
        "--arms-file",
        str(arms),
        "--manifest",
        str(project / "cases" / "manifest.jsonl"),
        "--root",
        str(project),
        "--cache-dir",
        str(project / "cache"),
        "--results-dir",
        str(project / "results"),
        "--run-id",
        arm,
        *extra,
    ]


def test_two_arms_differ_only_in_config(project: Path) -> None:
    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if body["model"] == "vendor/cheap-model":
            return findings_reply(HIT, MISS), {}
        # Fenced JSON with an extra field, as some models reply; cost reported.
        return "```json\n" + findings_reply({**HIT, "severity": "high"}) + "\n```", {
            "cost": 0.0042
        }

    with stub_server(respond) as stub:
        arms = write_arms(project, stub.base_url)
        assert main(eval_args(project, arms, "cheap")) == 0
        assert main(eval_args(project, arms, "reported")) == 0

    # 2 cases x 2 runs per arm, each sent to the model the arm names.
    models = [r["model"] for r in stub.requests]
    assert models == ["vendor/cheap-model"] * 4 + ["other-vendor/other-model"] * 4
    assert stub.auth_headers == ["Bearer key-a"] * 4 + ["Bearer key-b"] * 4
    assert all(
        r["response_format"] == {"type": "json_object"} for r in stub.requests[:4]
    )
    assert all("response_format" not in r for r in stub.requests[4:])
    assert all(r["provider"] == {"data_collection": "deny"} for r in stub.requests[4:])
    # Attribution headers reach the server only for the arm that sets them.
    assert all(
        h.get("http-referer") == "https://example.com/app"
        and h.get("x-openrouter-title") == "stub-app"
        for h in stub.headers[4:]
    )
    assert all(
        "http-referer" not in h and "x-openrouter-title" not in h
        for h in stub.headers[:4]
    )

    # The model sees the diff and the base file, never the ground truth.
    user = stub.requests[0]["messages"][1]["content"]
    assert "f, _ := os.Open(path)" in user
    assert "### store/load.go" in user and "defer f.Close()" in user
    sent = json.dumps(stub.requests)
    for leak in ("store-001", "store-002", "example.com/store", '"bugs"', "bug_class"):
        assert leak not in sent

    cheap = json.loads((project / "results" / "cheap" / "summary.json").read_text())
    assert cheap["model"] == "vendor/cheap-model"
    assert cheap["reviews"] == 4
    assert cheap["case_ids"] == ["store-001", "store-002"]
    assert cheap["precision"] == {"mean": 0.5, "min": 0.5, "max": 0.5}
    assert cheap["recall"] == {"mean": 1.0, "min": 1.0, "max": 1.0}
    assert cheap["f1"]["mean"] == pytest.approx(2 / 3)
    # tokens x price: (1000 x $1 + 200 x $5) / 1M
    assert cheap["cost_per_review_usd"] == pytest.approx(0.002)
    assert cheap["latency_s"]["p50"] > 0
    assert cheap["latency_s"]["p95"] >= cheap["latency_s"]["p50"]

    reported = json.loads(
        (project / "results" / "reported" / "summary.json").read_text()
    )
    assert reported["precision"]["mean"] == 1.0
    assert reported["cost_per_review_usd"] == pytest.approx(0.0042)
    assert reported["arm_config_hash"] != cheap["arm_config_hash"]

    lines = (project / "results" / "cheap" / "reviews.jsonl").read_text().splitlines()
    assert len(lines) == 4


def test_cache_reruns_cost_nothing(project: Path) -> None:
    with stub_server(lambda body: (findings_reply(HIT), {})) as stub:
        arms = write_arms(project, stub.base_url)
        assert main(eval_args(project, arms, "cheap")) == 0
        assert len(stub.requests) == 4
        args = eval_args(project, arms, "cheap")
        args[args.index("--run-id") + 1] = "again"
        assert main(args) == 0
        assert len(stub.requests) == 4  # all four came from the cache

        # One more run index pays only for the new run.
        args[args.index("--run-id") + 1] = "three"
        args[args.index("--runs") + 1] = "3"
        assert main(args) == 0
        assert len(stub.requests) == 6

    # A regenerated diff for the same case id is new input, so it re-runs.
    diff = project / CASE_DIR / "buggy.diff"
    diff.write_text(DIFF.replace("f, _ := os.Open(path)", "f, _ := os.Open(path) "))
    with stub_server(lambda body: (findings_reply(HIT), {})) as stub2:
        arms = write_arms(project, stub2.base_url)
        args[args.index("--run-id") + 1] = "changed"
        assert main(args) == 0
        # store-002 shares the diff file, so both cases x 3 runs re-run.
        assert len(stub2.requests) == 6

    again = json.loads((project / "results" / "again" / "summary.json").read_text())
    assert again["fresh_reviews"] == 0
    assert again["spent_this_invocation_usd"] == 0
    assert again["cost_per_review_usd"] == pytest.approx(0.002)


def test_malformed_reply_gets_one_retry(project: Path) -> None:
    replies = iter(["not json", findings_reply(HIT)] + ["{}"] * 2 * 3)

    with stub_server(lambda body: (next(replies), {})) as stub:
        arms = write_arms(project, stub.base_url)
        # The reply iterator is order-dependent, so run one review at a time.
        assert main(eval_args(project, arms, "cheap", "--concurrency", "1")) == 0

    # Case 1 run 0: bad then good. The other three: bad twice, then give up.
    assert len(stub.requests) == 2 + 2 * 3
    retry = stub.requests[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": "not json"}
    assert "not valid" in retry[-1]["content"]
    summary = json.loads((project / "results" / "cheap" / "summary.json").read_text())
    assert summary["malformed_reviews"] == 3
    assert summary["per_run"][0]["findings"] == 1


def test_budget_stops_the_run(project: Path) -> None:
    with stub_server(lambda body: (findings_reply(HIT), {"cost": 0.4})) as stub:
        arms = write_arms(project, stub.base_url)
        arm = load_arm(arms, "reported")
        budget = Budget(1.0)
        with pytest.raises(BudgetExceeded):
            run_eval(
                cases=load_manifest(project / "cases" / "manifest.jsonl"),
                arm=arm,
                runs=2,
                root=project,
                cache_dir=project / "cache",
                results_dir=project / "results",
                budget=budget,
            )
        # $0.4 x 3 reaches $1; the fourth call is refused before it is sent.
        assert len(stub.requests) == 3
        assert budget.spent_usd == pytest.approx(1.2)

        # The CLI stops with exit 3. Rerunning pays only for uncached reviews:
        # one more call fits in $0.3, the one after is refused.
        args = eval_args(project, arms, "reported", "--budget-usd", "0.3")
        args[args.index("--runs") + 1] = "3"
        assert main(args) == 3
        assert len(stub.requests) == 4
        assert not (project / "results" / "reported").exists()


def test_reports_cost_without_cost_fails_closed(project: Path) -> None:
    # The "reported" arm has no fallback price, so a reply without usage.cost
    # leaves spend unknown: the run stops instead of guessing.
    with stub_server(lambda body: (findings_reply(HIT), {})) as stub:
        arms = write_arms(project, stub.base_url)
        assert main(eval_args(project, arms, "reported")) == 3
        assert len(stub.requests) == 1
        assert not (project / "results" / "reported").exists()


def test_case_filter_runs_only_named_cases(project: Path) -> None:
    with stub_server(lambda body: (findings_reply(HIT), {})) as stub:
        arms = write_arms(project, stub.base_url)
        args = eval_args(project, arms, "cheap", "--case", "store-002")
        assert main(args) == 0
        assert len(stub.requests) == 2  # one case x 2 runs
        assert main([*args[:-2], "--case", "nope"]) == 2
        assert main([*args[:-2], "--runs", "0"]) == 2


class InFlight:
    """Counts requests the stub is handling at once."""

    def __init__(self) -> None:
        self.now = self.max = 0
        self._lock = threading.Lock()

    def __enter__(self) -> None:
        with self._lock:
            self.now += 1
            self.max = max(self.max, self.now)

    def __exit__(self, *exc: object) -> None:
        with self._lock:
            self.now -= 1


def test_concurrent_calls_overlap_and_order_is_stable(project: Path) -> None:
    inflight = InFlight()
    rng = random.Random(7)

    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        with inflight:
            time.sleep(0.2 + rng.random() * 0.2)  # scramble finish order
        return findings_reply(HIT, MISS), {}

    with stub_server(respond) as stub:
        arms = write_arms(project, stub.base_url)
        args = eval_args(project, arms, "cheap", "--concurrency", "8")
        args[args.index("--runs") + 1] = "4"
        started = time.perf_counter()
        assert main(args) == 0
        elapsed = time.perf_counter() - started
        assert len(stub.requests) == 8
        # 8 reviews of >= 0.2s each would take >= 1.6s one at a time.
        assert inflight.max >= 2
        assert elapsed < 1.6

        # The same run one at a time, from a fresh cache.
        serial = eval_args(project, arms, "cheap", "--concurrency", "1")
        serial[serial.index("--runs") + 1] = "4"
        serial[serial.index("--run-id") + 1] = "serial"
        serial[serial.index("--cache-dir") + 1] = str(project / "cache-serial")
        assert main(serial) == 0
        assert len(stub.requests) == 16

    def reviews(run_id: str) -> list[dict[str, Any]]:
        path = project / "results" / run_id / "reviews.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            assert row.pop("latency_s") > 0
            # Each stage's own timing too: everything else must match.
            assert row["generate"].pop("latency_s") > 0
        return rows

    concurrent = reviews("cheap")
    assert [(r["run_index"], r["case_id"]) for r in concurrent] == [
        (run, case) for run in range(4) for case in ("store-001", "store-002")
    ]
    assert concurrent == reviews("serial")

    def summary(run_id: str) -> dict[str, Any]:
        data = json.loads((project / "results" / run_id / "summary.json").read_text())
        for key in ("run_id", "latency_s"):
            data.pop(key)
        for row in data["ablation"]:
            row.pop("latency_s")
        return data

    assert summary("cheap") == summary("serial")


def run_tight(project: Path, url: str, budget: Budget, concurrency: int) -> None:
    run_eval(
        cases=load_manifest(project / "cases" / "manifest.jsonl"),
        arm=load_arm(write_arms(project, url), "tight"),
        runs=4,
        root=project,
        cache_dir=project / "cache",
        results_dir=project / "results",
        budget=budget,
        concurrency=concurrency,
    )


def test_budget_stays_hard_under_concurrency(project: Path) -> None:
    # Every reply costs its full worst case ($0.10). Under the old
    # check-before-call guard, 8 workers would all start and spend $0.80.
    inflight = InFlight()

    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        with inflight:
            time.sleep(0.2)
        return findings_reply(HIT), {"completion_tokens": 1000}

    with stub_server(respond) as stub:
        budget = Budget(0.35)
        with pytest.raises(BudgetExceeded):
            run_tight(project, stub.base_url, budget, concurrency=8)
        assert len(stub.requests) == 3
        assert inflight.max <= 3
        assert budget.spent_usd == pytest.approx(0.3)
        assert budget.spent_usd <= budget.limit_usd
        assert budget.reserved_usd == 0


def test_budget_waits_for_settling_calls_instead_of_failing(project: Path) -> None:
    # Replies cost $0.01 against a $0.10 reservation: only 3 calls fit in
    # flight under $0.35, but each settles low, so all 8 reviews complete.
    inflight = InFlight()

    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        with inflight:
            time.sleep(0.1)
        return findings_reply(HIT), {"completion_tokens": 100}

    with stub_server(respond) as stub:
        budget = Budget(0.35)
        run_tight(project, stub.base_url, budget, concurrency=8)
        assert len(stub.requests) == 8
        assert inflight.max <= 3
        assert budget.spent_usd == pytest.approx(0.08)


def test_429_and_5xx_are_retried_with_backoff(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(llm, "_sleep", lambda s, cancelled: sleeps.append(s))
    errors = iter([StubError(429, {"Retry-After": "2"}), StubError(503)])
    lock = threading.Lock()

    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        with lock:
            err = next(errors, None)
        if err is not None:
            raise err
        return findings_reply(HIT), {}

    with stub_server(respond) as stub:
        arms = write_arms(project, stub.base_url)
        # One at a time, so both errors hit the same request in turn.
        assert main(eval_args(project, arms, "cheap", "--concurrency", "1")) == 0
        assert len(stub.requests) == 4 + 2

    assert sleeps[0] == 2.0  # Retry-After honoured
    # Second retry of that request: base x 2, plus up to as much jitter.
    assert llm.BACKOFF_BASE_S * 2 <= sleeps[1] <= llm.BACKOFF_BASE_S * 4
    summary = json.loads((project / "results" / "cheap" / "summary.json").read_text())
    assert summary["http_retries"] == 2
    # Backoff is reported apart from latency, which times the good attempt.
    reviews = [
        json.loads(line)
        for line in (project / "results" / "cheap" / "reviews.jsonl")
        .read_text()
        .splitlines()
    ]
    retried = [r for r in reviews if r["http_retries"]]
    assert len(retried) == 1 and retried[0]["retry_wait_s"] > 0
    assert all(r["retry_wait_s"] == 0 for r in reviews if not r["http_retries"])
    assert summary["cost_per_review_usd"] == pytest.approx(0.002)


@pytest.mark.parametrize(("status", "calls"), [(500, 1 + llm.MAX_RETRIES), (400, 1)])
def test_retries_are_bounded_and_refused_calls_cost_nothing(
    project: Path, monkeypatch: pytest.MonkeyPatch, status: int, calls: int
) -> None:
    monkeypatch.setattr(llm, "_sleep", lambda s, cancelled: None)

    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        raise StubError(status)

    with stub_server(respond) as stub:
        budget = Budget(1.0)
        with pytest.raises(openai.APIStatusError):
            run_tight(project, stub.base_url, budget, concurrency=1)
        assert len(stub.requests) == calls
        assert budget.spent_usd == 0
        assert budget.reserved_usd == 0


def test_a_failure_stops_paid_calls_and_keeps_paid_replies(project: Path) -> None:
    # Only 3 calls fit in flight under $0.35; 5 more reviews wait to reserve.
    # The first request is refused (400): the waiting reviews must not send
    # anything, and the two calls already in flight are cached.
    lock = threading.Lock()
    seen = 0

    def respond(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        nonlocal seen
        with lock:
            seen += 1
            first = seen == 1
        if first:
            raise StubError(400)
        time.sleep(0.3)
        return findings_reply(HIT), {"completion_tokens": 100}

    with stub_server(respond) as stub:
        budget = Budget(0.35)
        with pytest.raises(openai.BadRequestError):
            run_tight(project, stub.base_url, budget, concurrency=8)
        assert len(stub.requests) == 3
        assert budget.spent_usd == pytest.approx(0.02)
        assert budget.reserved_usd == 0
    assert len(list((project / "cache").rglob("run-*.json"))) == 2
