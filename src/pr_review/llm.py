"""One chat call to an arm, with its cost, and the budget guard every call goes through."""

import json
import os
import random
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Any

from openai import APIStatusError, OpenAI
from openai.types.chat import ChatCompletionMessageParam
from openai.types.completion_usage import CompletionUsage

from pr_review.arms import Arm

DEFAULT_BUDGET_USD = 1.0

# Retries for rate limits (429) and provider errors (5xx), done here because the
# client runs with max_retries=0. A refused request isn't billed, so a retry
# costs time, not money; that time counts in the request's latency.
MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0
MAX_RETRY_AFTER_S = 30.0


def _sleep(seconds: float, cancelled: threading.Event) -> None:
    """Back off, waking early if the run is cancelled. Tests replace this."""
    cancelled.wait(seconds)


# OpenRouter may route a request to a provider dearer than the arm's list
# price, so a reported-cost arm reserves this multiple of its worst case.
REPORTED_COST_MARGIN = 2.0
# Per-message overhead on top of content bytes when bounding prompt tokens.
_MESSAGE_OVERHEAD_TOKENS = 16


class BudgetExceeded(RuntimeError):
    """The run's budget is spent; no further paid call may start."""


class Cancelled(RuntimeError):
    """Another call in this run failed, or the run was interrupted; stop."""


class CostUnknown(RuntimeError):
    """The arm says the provider reports cost, but a reply came back without it."""


class Budget:
    """Hard spend ceiling for one run, in USD, safe to share between threads.

    Every call reserves its worst-case cost before it starts and settles to its
    actual cost when it finishes, so calls in flight together can't overshoot:
    spent + reserved stays within the limit as long as no call costs more than
    it reserved. A reservation that doesn't fit waits while other calls are in
    flight (they usually settle below their reservation) and is refused once
    nothing is.

    A call can cost more than it reserved only if the provider charges above
    the arm's worst case (see `worst_case_cost`); `settle` warns when it does.
    An arm with no price has no worst case: its calls reserve all that is left,
    so they run one at a time and the run can overshoot by at most one call.

    The first failed call cancels the budget: calls waiting to reserve, and
    retries waiting to resend, stop with `Cancelled` instead of spending more.
    """

    def __init__(self, limit_usd: float = DEFAULT_BUDGET_USD) -> None:
        if limit_usd <= 0:
            raise ValueError("budget must be positive")
        self.limit_usd = limit_usd
        self.spent_usd = 0.0
        self.reserved_usd = 0.0
        self._in_flight = 0  # counted, not inferred from reserved_usd (float drift)
        self._cond = threading.Condition()
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        """Refuse every further reservation and wake anyone waiting."""
        with self._cond:
            self.cancelled.set()
            self._cond.notify_all()

    def reserve(self, usd: float | None) -> float:
        """Hold `usd` (None: everything left) until `settle`. Returns the amount held."""
        with self._cond:
            while True:
                if self.cancelled.is_set():
                    raise Cancelled("run cancelled after a failed call")
                free = self.limit_usd - self.spent_usd - self.reserved_usd
                amount = free if usd is None else usd
                if free > 0 and amount <= free:
                    self.reserved_usd += amount
                    self._in_flight += 1
                    return amount
                if self._in_flight == 0:
                    raise BudgetExceeded(
                        f"budget ${self.limit_usd:.4f} spent "
                        f"(${self.spent_usd:.4f}; next call may cost "
                        f"${amount:.4f})"
                    )
                self._cond.wait()

    def settle(self, reserved: float, actual: float, *, failed: bool = False) -> None:
        """Release a reservation and charge what the call actually cost.

        `failed` cancels the budget in the same step, so no waiting call can
        take the freed room first.
        """
        if actual > reserved:
            warnings.warn(
                f"call cost ${actual:.6f}, above its ${reserved:.6f} reservation",
                stacklevel=2,
            )
        with self._cond:
            if failed:
                self.cancelled.set()
            self._in_flight -= 1
            self.reserved_usd = self.reserved_usd - reserved if self._in_flight else 0.0
            self.spent_usd += actual
            self._cond.notify_all()


@dataclass(frozen=True)
class ToolCall:
    """One function call the model asked for: its id, name and raw JSON arguments."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Reply:
    content: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_s: float  # the successful attempt only
    retries: int = 0  # 429/5xx retries before it
    retry_wait_s: float = 0.0  # failed attempts plus backoff, kept apart
    finish_reason: str = ""  # "length" when the reply hit max_tokens
    reasoning_tokens: int = 0  # hidden reasoning, counted in completion_tokens
    tool_calls: tuple[ToolCall, ...] = ()
    # Provider reasoning blocks (OpenRouter's `reasoning_details`), sent back
    # with the assistant turn in a tool loop so reasoning models keep context.
    reasoning_details: tuple[Any, ...] = ()


def make_client(arm: Arm) -> OpenAI:
    key = os.environ.get(arm.api_key_env)
    if not key:
        raise RuntimeError(f"arm {arm.name!r}: env var {arm.api_key_env} is not set")
    # max_retries=0: retries are this harness's call (see `chat`), and each one
    # must stay within the call's budget reservation.
    return OpenAI(
        base_url=arm.base_url,
        api_key=key,
        timeout=arm.timeout_s,
        max_retries=0,
        default_headers=arm.extra_headers or None,
    )


def worst_case_cost(
    arm: Arm,
    messages: list[ChatCompletionMessageParam],
    tools: list[dict[str, Any]] | None = None,
) -> float | None:
    """An upper bound on one call's cost, or None if the arm has no price.

    Prompt tokens are bounded by content bytes (a token is at least one byte),
    plus the bytes of any tool calls and reasoning blocks in the history and of
    the tool schemas,
    and completion tokens by `max_tokens`.
    """
    if arm.price is None:
        return None
    prompt_bound = sum(
        len(str(m.get("content") or "").encode())
        + (len(json.dumps(calls).encode()) if (calls := m.get("tool_calls")) else 0)
        # Reasoning blocks sent back in a tool loop are billed as input too.
        + (
            len(json.dumps(blocks).encode())
            if (blocks := m.get("reasoning_details"))
            else 0
        )
        + _MESSAGE_OVERHEAD_TOKENS
        for m in messages
    )
    if tools:
        prompt_bound += len(json.dumps(tools).encode())
    cost = arm.price.cost(prompt_bound, arm.max_tokens)
    return cost * REPORTED_COST_MARGIN if arm.reports_cost else cost


def chat(
    client: OpenAI,
    arm: Arm,
    messages: list[ChatCompletionMessageParam],
    budget: Budget,
    *,
    extra_body: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
) -> Reply:
    """Send one chat request within a budget reservation, retrying 429 and 5xx.

    `extra_body` replaces the arm's `extra_body` for this request only.
    `tools` (OpenAI function schemas) turns off JSON mode for the request:
    providers differ on combining the two, and the prompt asks for JSON anyway.
    """
    reserved = budget.reserve(worst_case_cost(arm, messages, tools))
    try:
        reply = _send(
            client,
            arm,
            messages,
            budget.cancelled,
            arm.extra_body if extra_body is None else extra_body,
            tools,
            tool_choice,
        )
    except (APIStatusError, Cancelled):
        # The provider refused the request, or we never resent it: not billed.
        budget.settle(reserved, 0.0, failed=True)
        raise
    except BaseException:
        # Timeouts, dropped connections, missing cost: the request may have
        # been billed and we can't tell for how much, so keep the worst case.
        budget.settle(reserved, reserved, failed=True)
        raise
    budget.settle(reserved, reply.cost_usd)
    return reply


def _send(
    client: OpenAI,
    arm: Arm,
    messages: list[ChatCompletionMessageParam],
    cancelled: threading.Event,
    extra_body: dict[str, Any],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
) -> Reply:
    options: dict[str, Any] = {}
    if arm.temperature is not None:
        options["temperature"] = arm.temperature
    if tools:
        options["tools"] = tools
        if tool_choice is not None:
            options["tool_choice"] = tool_choice
    elif arm.json_mode:
        options["response_format"] = {"type": "json_object"}
    if extra_body:
        options["extra_body"] = extra_body
    first_started = time.perf_counter()
    retries = 0
    while True:
        started = time.perf_counter()
        try:
            resp = client.chat.completions.create(
                model=arm.model, messages=messages, max_tokens=arm.max_tokens, **options
            )
            break
        except APIStatusError as e:
            if not _retryable(e) or retries >= MAX_RETRIES:
                raise
            _sleep(_backoff(e, retries), cancelled)
            if cancelled.is_set():
                raise Cancelled("run cancelled while backing off") from e
            retries += 1
    latency = time.perf_counter() - started
    usage = resp.usage
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0
    cost = _cost(arm, usage, prompt_tokens, completion_tokens)
    message = resp.choices[0].message if resp.choices else None
    content = message.content if message else None
    finish_reason = (resp.choices[0].finish_reason or "") if resp.choices else ""
    details = usage.completion_tokens_details if usage else None
    calls = tuple(
        ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "")
        for c in (message.tool_calls or [] if message else [])
        if c.type == "function"
    )
    reasoning_details = (
        (message.model_extra or {}).get("reasoning_details") if message else None
    )
    return Reply(
        content=content or "",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        latency_s=latency,
        retries=retries,
        retry_wait_s=started - first_started if retries else 0.0,
        finish_reason=finish_reason,
        reasoning_tokens=(details.reasoning_tokens or 0) if details else 0,
        tool_calls=calls,
        reasoning_details=tuple(reasoning_details)
        if isinstance(reasoning_details, list)
        else (),
    )


def _retryable(e: APIStatusError) -> bool:
    return e.status_code == 429 or e.status_code >= 500


def _backoff(e: APIStatusError, retries: int) -> float:
    """Retry-After if the provider sent one (capped), else jittered exponential."""
    header = e.response.headers.get("retry-after")
    if header is not None:
        try:
            return min(max(float(header), 0.0), MAX_RETRY_AFTER_S)
        except ValueError:
            pass
    base: float = BACKOFF_BASE_S * 2**retries
    return base + random.uniform(0, base)


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
