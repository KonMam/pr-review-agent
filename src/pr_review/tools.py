"""Generate with read-only tools: the item-5 ablation.

The generator gets the same PR text as single-pass, plus two OpenAI-compatible
function tools over the repository at the base commit:

- `read_file(path, start_line, end_line)`: lines of one file;
- `grep(pattern, path_glob)`: regex matches across files.

Both only read a case's offline snapshot (`pr-review snapshot` builds it from
local clones: non-test `.go` files at base, with the case's own `base/` files
laid over them), so a review needs no clone and no network. Test files are left
out of the snapshot because a fix commit's regression test would name the bug.

The model gets at most `max_turns` turns that may call tools. After that, one
more call with `tool_choice = "none"` asks for the answer. A malformed or
cut-off answer gets the same one retry as `ask_json`.
"""

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from pr_review.arms import Arm
from pr_review.ask import ASK_VERSION, RETRY_PROMPT, MalformedReply
from pr_review.generate import (
    SYSTEM_PROMPT,
    GenerateResult,
    ReviewInput,
    parse_reply,
    pr_text,
)
from pr_review.llm import Budget, Reply, chat

DEFAULT_MAX_TURNS = 4
MAX_READ_LINES = 300
MAX_GREP_MATCHES = 40
MAX_LINE_CHARS = 200
MAX_RESULT_CHARS = 16_000

TOOLS_PROMPT = f"""

You can call read-only tools on the repository at the base commit (before this \
diff): `read_file` and `grep`. Test files are not available. Use them only when \
a possible bug depends on code outside the files shown, such as a callee, a type \
or the other users of a changed function. You have at most {{max_turns}} turns of \
tool calls; then reply with the JSON object. Results are capped: \
read_file returns at most {MAX_READ_LINES} lines, grep at most \
{MAX_GREP_MATCHES} matches."""

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read lines of a file in the repository at the base commit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "repo-relative path"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search files in the repository at the base commit "
            "with a Python regular expression. Returns path:line: text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path_glob": {
                        "type": "string",
                        "description": "fnmatch glob on the repo-relative path, "
                        "default *.go",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
]

FINAL_PROMPT = "No more tool calls. Reply now with only the JSON object."


def tools_version(max_turns: int) -> str:
    """Changes whenever anything that shapes a tools reply changes."""
    blob = json.dumps(
        [
            SYSTEM_PROMPT,
            TOOLS_PROMPT,
            TOOL_SCHEMAS,
            FINAL_PROMPT,
            RETRY_PROMPT,
            ASK_VERSION,
            max_turns,
            [MAX_READ_LINES, MAX_GREP_MATCHES, MAX_LINE_CHARS, MAX_RESULT_CHARS],
        ],
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


class RepoTools:
    """Read-only access to one snapshot directory. Never reads outside it."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._files: list[str] | None = None

    def files(self) -> list[str]:
        if self._files is None:
            self._files = sorted(
                p.relative_to(self.root).as_posix()
                for p in self.root.rglob("*")
                if p.is_file()
            )
        return self._files

    def digest(self) -> str:
        """Hash of every path and its bytes, so a changed snapshot isn't served from cache."""
        h = hashlib.sha256()
        for rel in self.files():
            h.update(rel.encode() + b"\0")
            h.update(hashlib.sha256((self.root / rel).read_bytes()).digest())
        return h.hexdigest()[:16]

    def _resolve(self, path: str) -> Path | None:
        p = (self.root / path.lstrip("/")).resolve()
        if not p.is_relative_to(self.root) or not p.is_file():
            return None
        return p

    def read_file(
        self, path: str, start_line: int = 1, end_line: int | None = None
    ) -> str:
        p = self._resolve(path)
        if p is None:
            return (
                f"error: no file {path!r} in the repository (test files are excluded)"
            )
        lines = p.read_bytes().decode("utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        end = min(len(lines), end_line or len(lines), start + MAX_READ_LINES - 1)
        if start > len(lines):
            return f"error: {path} has {len(lines)} lines"
        body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
        more = f"\n[{len(lines)} lines in total]" if end < len(lines) else ""
        return _cap(body + more)

    def grep(self, pattern: str, path_glob: str = "*.go") -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"error: bad regex: {e}"
        out: list[str] = []
        for rel in self.files():
            if not fnmatch.fnmatch(rel, path_glob or "*.go"):
                continue
            text = (self.root / rel).read_bytes().decode("utf-8", errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    out.append(f"{rel}:{n}: {line.strip()[:MAX_LINE_CHARS]}")
                    if len(out) >= MAX_GREP_MATCHES:
                        return _cap("\n".join(out) + "\n[more matches cut]")
        return _cap("\n".join(out)) if out else "no matches"

    def call(self, name: str, arguments: str) -> str:
        """Run one tool call; bad arguments come back as an error string, never raise."""
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise TypeError("arguments must be an object")
            if name == "read_file":
                return self.read_file(
                    str(args["path"]),
                    int(args.get("start_line") or 1),
                    int(args["end_line"]) if args.get("end_line") else None,
                )
            if name == "grep":
                return self.grep(
                    str(args["pattern"]), str(args.get("path_glob") or "*.go")
                )
            return f"error: unknown tool {name!r}"
        except (ValueError, TypeError, KeyError, OverflowError) as e:
            return f"error: bad arguments: {e}"
        except OSError as e:  # e.g. a path too long for the filesystem
            return f"error: cannot read: {e.strerror or e}"


def _cap(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[:MAX_RESULT_CHARS] + "\n[output cut]"


@dataclass
class _Tally:
    calls: int = 0
    tool_calls: int = 0
    truncated: int = 0
    retries: int = 0
    retry_wait_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0

    def add(self, r: Reply) -> None:
        self.calls += 1
        self.truncated += r.finish_reason == "length"
        self.retries += r.retries
        self.retry_wait_s += r.retry_wait_s
        self.prompt_tokens += r.prompt_tokens
        self.completion_tokens += r.completion_tokens
        self.reasoning_tokens += r.reasoning_tokens
        self.cost_usd += r.cost_usd
        self.latency_s += r.latency_s


def _assistant_turn(r: Reply) -> ChatCompletionMessageParam:
    msg: dict[str, Any] = {"role": "assistant", "content": r.content or None}
    if r.tool_calls:
        msg["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments},
            }
            for c in r.tool_calls
        ]
    if r.reasoning_details:
        msg["reasoning_details"] = list(r.reasoning_details)
    return msg  # type: ignore[return-value]


def generate_with_tools(
    client: OpenAI,
    arm: Arm,
    review: ReviewInput,
    budget: Budget,
    repo: RepoTools,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> GenerateResult:
    """One review where the model may call read-only tools for up to `max_turns` turns."""
    messages: list[ChatCompletionMessageParam] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT + TOOLS_PROMPT.format(max_turns=max_turns),
        },
        {"role": "user", "content": pr_text(review)},
    ]
    t = _Tally()
    reply: Reply | None = None
    for _ in range(max_turns):
        reply = chat(
            client, arm, messages, budget, tools=TOOL_SCHEMAS, tool_choice="auto"
        )
        t.add(reply)
        if not reply.tool_calls:
            break
        messages.append(_assistant_turn(reply))
        for c in reply.tool_calls:
            t.tool_calls += 1
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": c.id,
                    "content": repo.call(c.name, c.arguments),
                }
            )
        reply = None
    if reply is None:  # turns used up on tool calls: ask for the answer
        messages.append({"role": "user", "content": FINAL_PROMPT})
        reply = chat(
            client, arm, messages, budget, tools=TOOL_SCHEMAS, tool_choice="none"
        )
        t.add(reply)

    findings = None
    answers = 1
    try:
        findings = parse_reply(reply.content)
    except MalformedReply as e:
        answers = 2
        extra_body = None
        if reply.finish_reason == "length" and arm.truncation_extra_body:
            extra_body = {**arm.extra_body, **arm.truncation_extra_body}
        else:
            messages = [
                *messages,
                {"role": "assistant", "content": reply.content},
                {"role": "user", "content": RETRY_PROMPT.format(error=str(e)[:500])},
            ]
        retry = chat(
            client,
            arm,
            messages,
            budget,
            extra_body=extra_body,
            tools=TOOL_SCHEMAS,
            tool_choice="none",
        )
        t.add(retry)
        try:
            findings = parse_reply(retry.content)
        except MalformedReply:
            findings = None
    return GenerateResult(
        findings=findings or [],
        malformed=findings is None,
        attempts=answers,
        truncated=t.truncated,
        reasoning_tokens=t.reasoning_tokens,
        retries=t.retries,
        retry_wait_s=t.retry_wait_s,
        prompt_tokens=t.prompt_tokens,
        completion_tokens=t.completion_tokens,
        cost_usd=t.cost_usd,
        latency_s=t.latency_s,
        calls=t.calls,
        tool_calls=t.tool_calls,
    )
