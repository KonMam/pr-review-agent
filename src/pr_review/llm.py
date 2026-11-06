"""One chat call to an arm, with its cost, and the budget guard every call goes through."""

import os
import time
from dataclasses import dataclass
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from openai.types.completion_usage import CompletionUsage

from pr_review.arms import Arm

DEFAULT_BUDGET_USD = 1.0


class BudgetExceeded(RuntimeError):
    """The run's budget is spent; no further paid call may start."""


class CostUnknown(RuntimeError):
    """The arm says the provider reports cost, but a reply came back without it."""


class Budget:
    """Hard spend ceiling for one run, in USD.

    `check()` runs before every call and refuses once spend has reached the
    limit. A call already in flight can't be stopped, so a run can overshoot by
    at most one call; each call is capped by the arm's `max_tokens`.
    """

    def __init__(self, limit_usd: float = DEFAULT_BUDGET_USD) -> None:
        if limit_usd <= 0:
            raise ValueError("budget must be positive")
        self.limit_usd = limit_usd
        self.spent_usd = 0.0

    def check(self) -> None:
        if self.spent_usd >= self.limit_usd:
            raise BudgetExceeded(
                f"budget ${self.limit_usd:.4f} spent (${self.spent_usd:.4f})"
            )

    def charge(self, usd: float) -> None:
        self.spent_usd += usd


@dataclass(frozen=True)
class Reply:
    content: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_s: float


def make_client(arm: Arm) -> OpenAI:
    key = os.environ.get(arm.api_key_env)
    if not key:
        raise RuntimeError(f"arm {arm.name!r}: env var {arm.api_key_env} is not set")
    # max_retries=0: retries are this harness's call, and each one must pass
    # the budget check.
    return OpenAI(
        base_url=arm.base_url, api_key=key, timeout=arm.timeout_s, max_retries=0
    )


def chat(
    client: OpenAI,
    arm: Arm,
    messages: list[ChatCompletionMessageParam],
    budget: Budget,
) -> Reply:
    """Send one chat request. Charges the budget before returning."""
    budget.check()
    options: dict[str, Any] = {}
    if arm.temperature is not None:
        options["temperature"] = arm.temperature
    if arm.json_mode:
        options["response_format"] = {"type": "json_object"}
    if arm.extra_body:
        options["extra_body"] = arm.extra_body
    started = time.perf_counter()
    resp = client.chat.completions.create(
        model=arm.model, messages=messages, max_tokens=arm.max_tokens, **options
    )
    latency = time.perf_counter() - started
    usage = resp.usage
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0
    cost = _cost(arm, usage, prompt_tokens, completion_tokens)
    budget.charge(cost)
    content = resp.choices[0].message.content if resp.choices else None
    return Reply(
        content=content or "",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        latency_s=latency,
    )


def _cost(
    arm: Arm, usage: CompletionUsage | None, prompt_tokens: int, completion_tokens: int
) -> float:
    reported = _reported_cost(usage)
    if reported is not None:
        return reported
    if arm.price is not None:
        return arm.price.cost(prompt_tokens, completion_tokens)
    raise CostUnknown(
        f"arm {arm.name!r} has reports_cost = true but the reply had no usage.cost; "
        "set a price in arms.toml"
    )


def _reported_cost(usage: CompletionUsage | None) -> float | None:
    """OpenRouter puts the request's USD cost at `usage.cost`."""
    if usage is None:
        return None
    extra = usage.model_extra or {}
    value = extra.get("cost")
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None
