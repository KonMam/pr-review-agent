"""Model arms: named entries in `arms.toml`, so swapping a model is a config change.

Each `[arms.<name>]` table sets a base URL, the env var that holds the API key,
the model id, and how cost is known: either the provider reports it per request
(`reports_cost = true`, as OpenRouter does) or the arm sets a `price` in USD per
million tokens. An arm with neither is rejected, because the budget guard could
not count its spend.
"""

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ArmsError(ValueError):
    """`arms.toml` is malformed or names an unknown arm."""


class Price(BaseModel):
    """USD per million tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.input_per_mtok
            + completion_tokens * self.output_per_mtok
        ) / 1_000_000


class Arm(BaseModel):
    """One model endpoint and the settings every call to it uses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    api_key_env: str = Field(min_length=1)
    model: str = Field(min_length=1)
    price: Price | None = None
    reports_cost: bool = False
    temperature: float | None = Field(default=None, ge=0)
    max_tokens: int = Field(default=4096, ge=1)
    timeout_s: float = Field(default=180, gt=0)
    json_mode: bool = True
    # Sent as-is in the request body, e.g. OpenRouter's `provider` routing
    # options (`data_collection = "deny"`).
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cost_known(self) -> Self:
        if self.price is None and not self.reports_cost:
            raise ValueError(
                "set `price` or `reports_cost = true`; the budget guard needs one"
            )
        return self

    def config_hash(self) -> str:
        """Hash of everything that can change a reply. The name and price are left out:
        renaming an arm or correcting its price shouldn't throw away cached replies."""
        relevant = self.model_dump(exclude={"name", "price", "reports_cost"})
        blob = json.dumps(relevant, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def load_arms(path: Path) -> dict[str, Arm]:
    """Parse every `[arms.<name>]` table in the file."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ArmsError(f"{path}: invalid TOML: {e}") from e
    tables = data.get("arms")
    if not isinstance(tables, dict) or not tables:
        raise ArmsError(f"{path}: no [arms.<name>] tables")
    arms: dict[str, Arm] = {}
    for name, table in tables.items():
        if not isinstance(table, dict):
            raise ArmsError(f"{path}: arms.{name} is not a table")
        try:
            arms[name] = Arm.model_validate({**table, "name": name})
        except ValidationError as e:
            raise ArmsError(f"{path}: arms.{name}: {e}") from e
    return arms


def load_arm(path: Path, name: str) -> Arm:
    arms = load_arms(path)
    if name not in arms:
        raise ArmsError(f"{path}: no arm {name!r}; known: {', '.join(sorted(arms))}")
    return arms[name]
