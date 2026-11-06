import re
from pathlib import Path

import pytest

from pr_review.arms import ArmsError, load_arm, load_arms

REPO_ARMS = Path(__file__).parent.parent / "arms.toml"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "arms.toml"
    path.write_text(text)
    return path


def test_repo_arms_file_loads() -> None:
    arms = load_arms(REPO_ARMS)
    assert {"ollama-gemma", "or-deepseek-flash", "deepseek-direct"} <= set(arms)
    assert arms["or-deepseek-flash"].extra_body["provider"] == {
        "data_collection": "deny"
    }


def test_loads_arm_with_defaults(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '[arms.a]\nbase_url = "http://x/v1"\napi_key_env = "K"\nmodel = "m"\n'
        "price = { input_per_mtok = 1, output_per_mtok = 2 }\n",
    )
    arm = load_arm(path, "a")
    assert arm.name == "a" and arm.model == "m" and arm.json_mode
    assert arm.price is not None and arm.price.cost(1_000_000, 500_000) == 2.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "no [arms.<name>] tables"),
        ("[arms.a\n", "invalid TOML"),
        (
            '[arms.a]\nbase_url = "u"\napi_key_env = "K"\nmodel = "m"\n',
            "set `price` or `reports_cost = true`",
        ),
        (
            (
                '[arms.a]\nbase_url = "u"\napi_key_env = "K"\nmodel = "m"\n'
                "reports_cost = true\ntypo = 1\n"
            ),
            "typo",
        ),
    ],
)
def test_rejects_bad_arms(tmp_path: Path, text: str, expected: str) -> None:
    with pytest.raises(ArmsError, match=re.escape(expected)):
        load_arms(write(tmp_path, text))


def test_unknown_arm_names_known_ones(tmp_path: Path) -> None:
    with pytest.raises(
        ArmsError, match="no arm .nope.; known: deepseek-direct, ollama-gemma"
    ):
        load_arm(REPO_ARMS, "nope")


def test_config_hash_ignores_name_and_price(tmp_path: Path) -> None:
    base = '[arms.{n}]\nbase_url = "u"\napi_key_env = "K"\nmodel = "{m}"\n'
    path = write(
        tmp_path,
        base.format(n="a", m="m")
        + "reports_cost = true\n"
        + base.format(n="b", m="m")
        + "price = { input_per_mtok = 1, output_per_mtok = 2 }\n"
        + base.format(n="c", m="other")
        + "reports_cost = true\n",
    )
    arms = load_arms(path)
    assert arms["a"].config_hash() == arms["b"].config_hash()
    assert arms["a"].config_hash() != arms["c"].config_hash()


def test_config_hash_ignores_extra_headers(tmp_path: Path) -> None:
    base = '[arms.{n}]\nbase_url = "u"\napi_key_env = "K"\nmodel = "m"\nreports_cost = true\n'
    path = write(
        tmp_path,
        base.format(n="plain")
        + base.format(n="attributed")
        + 'extra_headers = { HTTP-Referer = "https://x", X-OpenRouter-Title = "t" }\n',
    )
    arms = load_arms(path)
    assert arms["attributed"].extra_headers == {
        "HTTP-Referer": "https://x",
        "X-OpenRouter-Title": "t",
    }
    assert arms["plain"].config_hash() == arms["attributed"].config_hash()


def test_repo_openrouter_arms_send_attribution_headers() -> None:
    openrouter = [
        a for a in load_arms(REPO_ARMS).values() if "openrouter.ai" in a.base_url
    ]
    assert openrouter
    for arm in openrouter:
        assert arm.extra_headers == {
            "HTTP-Referer": "https://github.com/KonMam/pr-review-agent",
            "X-OpenRouter-Title": "pr-review-agent",
        }


def test_repo_openrouter_arms_deny_data_collection_and_report_cost() -> None:
    """Every paid OpenRouter arm, the ladder included, keeps prompts off
    providers that store or train on them, and has a fallback price."""
    openrouter = {
        n: a for n, a in load_arms(REPO_ARMS).items() if "openrouter.ai" in a.base_url
    }
    assert {"or-deepseek-flash", "or-deepseek-pro", "or-glm", "or-sonnet"} <= set(
        openrouter
    )
    for name, arm in openrouter.items():
        assert arm.extra_body["provider"]["data_collection"] == "deny", name
        assert arm.extra_body["usage"] == {"include": True}, name
        assert arm.reports_cost and arm.price is not None, name


def test_repo_ladder_arms_hash_apart() -> None:
    arms = load_arms(REPO_ARMS)
    names = [
        "or-deepseek-flash",
        "or-deepseek-pro",
        "or-glm",
        "or-sonnet",
        "or-gpt-sol",
    ]
    assert len({arms[n].config_hash() for n in names}) == len(names)
