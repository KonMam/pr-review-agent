"""One structured model call: send, validate the JSON reply, and retry once.

Every stage that asks a model for JSON (generate, grade) goes through `ask_json`,
so they share the retry rules:

- A reply cut off at `max_tokens` (finish reason "length") is retried with the
  same messages and the arm's `truncation_extra_body` merged over its
  `extra_body`. On OpenRouter reasoning models the cap is usually eaten by
  hidden reasoning that never converges, and asking for more of it again just
  repeats the cut-off; turning reasoning off for the retry gets an answer.
- Any other malformed reply is retried once with the error appended, as before.

Either way there are at most two attempts, and a reply still malformed after
the second is reported as `malformed`, not raised.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from pr_review.arms import Arm
from pr_review.llm import Budget, chat

RETRY_PROMPT = """\
Your reply was not valid. Error: {error}
Reply again with only the JSON object described above.\
"""

# Bump when the retry rules change: the replies they produce change too.
ASK_VERSION = "ask-2"


class MalformedReply(ValueError):
    """The model's reply isn't the JSON shape the prompt asks for."""


_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def load_json(content: str) -> Any:
    """Parse a reply's JSON, allowing a ```json fence around it. Raises `MalformedReply`."""
    text = content.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise MalformedReply(f"not JSON: {e.msg} at char {e.pos}") from e


@dataclass(frozen=True)
class AskResult[T]:
    value: T | None  # None when still malformed after the retry
    attempts: int
    truncated: int  # attempts cut off at max_tokens
    retries: int  # 429/5xx retries across attempts
    retry_wait_s: float  # their failed attempts and backoff, not in latency_s
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cost_usd: float
    latency_s: float

    @property
    def malformed(self) -> bool:
        return self.value is None


def ask_json[T](
    client: OpenAI,
    arm: Arm,
    messages: list[ChatCompletionMessageParam],
    budget: Budget,
    parse: Callable[[str], T],
) -> AskResult[T]:
    """Call the arm and `parse` the reply (raising `MalformedReply`), retrying once."""
    first_messages = messages
    extra_body = arm.extra_body
    prompt_tokens = completion_tokens = reasoning_tokens = retries = truncated = 0
    cost = latency = retry_wait = 0.0
    value: T | None = None
    attempt = 0
    for attempt in (1, 2):
        reply = chat(client, arm, messages, budget, extra_body=extra_body)
        prompt_tokens += reply.prompt_tokens
        completion_tokens += reply.completion_tokens
        reasoning_tokens += reply.reasoning_tokens
        cost += reply.cost_usd
        latency += reply.latency_s
        retries += reply.retries
        retry_wait += reply.retry_wait_s
        cut_off = reply.finish_reason == "length"
        truncated += cut_off
        try:
            value = parse(reply.content)
            break
        except MalformedReply as e:
            if cut_off and arm.truncation_extra_body:
                messages = first_messages
                extra_body = {**arm.extra_body, **arm.truncation_extra_body}
            else:
                messages = [
                    *messages,
                    {"role": "assistant", "content": reply.content},
                    {
                        "role": "user",
                        "content": RETRY_PROMPT.format(error=str(e)[:500]),
                    },
                ]
    return AskResult(
        value=value,
        attempts=attempt,
        truncated=truncated,
        retries=retries,
        retry_wait_s=retry_wait,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        cost_usd=cost,
        latency_s=latency,
    )
