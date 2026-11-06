"""Acceptance test: the eval pipeline against a stub OpenAI-compatible server.

Two arms differ only in `arms.toml` (model id, key env var, cost source), and
the same Python runs both. Fixture cases use the item-1 manifest format; a
throwaway git repo provides the base commit.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from stub_server import stub_server

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


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with two fixture cases and a local clone of their repo."""
    repo = tmp_path / "repos" / "example.com" / "store"
    (repo / "store").mkdir(parents=True)
    (repo / "store" / "load.go").write_text(BASE_GO)
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "-m",
        "base",
    )
    base = git(repo, "rev-parse", "HEAD")

    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "store-001.diff").write_text(DIFF)
    lines = []
    for case_id in ("store-001", "store-002"):
        lines.append(
            json.dumps(
                {
                    "id": case_id,
                    "provenance": {
                        "repo": "example.com/store",
                        "fix_commit": base,
                        "licence": "MIT",
                    },
                    "base_ref": base,
                    "diff_path": "cases/store-001.diff",
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
        "--repos-dir",
        str(project / "repos"),
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
    diff = project / "cases" / "store-001.diff"
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
        assert main(eval_args(project, arms, "cheap")) == 0

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
                repos_dir=project / "repos",
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
