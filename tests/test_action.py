"""Offline checks of `action.yml`'s token wiring: App token when configured, else GITHUB_TOKEN.

There's no YAML parser in the dependencies, so the steps are read by indentation,
which is enough for this one hand-written file. The input check's shell script
is run for real with bash.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

ACTION = Path(__file__).resolve().parents[1] / "action.yml"


def _steps() -> dict[str, list[str]]:
    """Each step's lines, keyed by its `name:`."""
    lines = ACTION.read_text(encoding="utf-8").splitlines()
    steps: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in lines:
        m = re.match(r"^    - name: (.+)$", line)
        if m:
            current = steps.setdefault(m.group(1), [])
        elif current is not None and (line.startswith("      ") or not line.strip()):
            current.append(line)
        else:
            current = None
    return steps


def _field(step: list[str], key: str) -> str:
    for line in step:
        m = re.match(rf"^\s+{re.escape(key)}: (.*)$", line)
        if m:
            return m.group(1)
    raise AssertionError(f"{key} not in step")


def _run_script(step: list[str]) -> str:
    start = next(i for i, line in enumerate(step) if line.strip() == "run: |")
    return "\n".join(line[8:] for line in step[start + 1 :]) + "\n"


def test_every_used_action_is_pinned_by_sha() -> None:
    uses = re.findall(r"uses: (\S+)", ACTION.read_text(encoding="utf-8"))
    assert uses
    for u in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u), u


def test_app_token_step_runs_only_with_app_inputs() -> None:
    step = _steps()["Mint a GitHub App token"]
    assert _field(step, "id") == "app-token"
    assert _field(step, "uses").startswith("actions/create-github-app-token@")
    cond = _field(step, "if")
    assert "steps.check.outputs.enabled == 'true'" in cond
    assert "inputs.app-client-id != ''" in cond
    assert _field(step, "client-id") == "${{ inputs.app-client-id }}"
    assert _field(step, "private-key") == "${{ inputs.app-private-key }}"
    assert _field(step, "permission-pull-requests") == "write"
    assert _field(step, "permission-contents") == "read"


def test_app_token_is_minted_before_posting() -> None:
    names = list(_steps())
    assert names.index("Mint a GitHub App token") < names.index(
        "Post comments and write the findings log"
    )


def test_post_uses_app_token_else_github_token() -> None:
    step = _steps()["Post comments and write the findings log"]
    assert (
        _field(step, "GITHUB_TOKEN")
        == "${{ steps.app-token.outputs.token || inputs.github-token }}"
    )


@pytest.mark.parametrize(
    ("has_id", "has_key", "code", "enabled"),
    [
        ("false", "false", 0, "true"),  # today's behaviour: GITHUB_TOKEN
        ("true", "true", 0, "true"),  # App configured
        (
            "true",
            "false",
            1,
            None,
        ),  # half-configured is an error, not a silent fallback
        ("false", "true", 1, None),
    ],
)
def test_check_step_rejects_half_configured_app(
    tmp_path: Path, has_id: str, has_key: str, code: int, enabled: str | None
) -> None:
    script = _run_script(_steps()["Check inputs"])
    out = tmp_path / "out"
    out.touch()
    env = {
        "PATH": os.environ["PATH"],
        "KEY": "sk-test",
        "EVENT": "pull_request",
        "HAS_APP_ID": has_id,
        "HAS_APP_KEY": has_key,
        "GITHUB_OUTPUT": str(out),
    }
    r = subprocess.run(
        ["bash", "-c", script], check=False, env=env, capture_output=True, text=True
    )
    assert r.returncode == code, r.stdout + r.stderr
    if enabled is not None:
        assert out.read_text() == f"enabled={enabled}\n"
    else:
        assert "app-client-id and app-private-key" in r.stdout


def test_check_step_env_carries_app_flags() -> None:
    step = _steps()["Check inputs"]
    assert _field(step, "HAS_APP_ID") == "${{ inputs.app-client-id != '' }}"
    assert _field(step, "HAS_APP_KEY") == "${{ inputs.app-private-key != '' }}"
