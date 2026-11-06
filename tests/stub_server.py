"""A local OpenAI-compatible chat server for tests. No network beyond loopback."""

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Given the request body, return (assistant content, extra usage fields), or
# raise `StubError` to answer with an HTTP error. It may sleep: the server
# handles requests on separate threads.
Responder = Callable[[dict[str, Any]], tuple[str, dict[str, Any]]]


class StubError(Exception):
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(status)
        self.status = status
        self.headers = headers or {}


@dataclass
class Stub:
    base_url: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    auth_headers: list[str] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)  # names lowercased


@contextmanager
def stub_server(respond: Responder) -> Iterator[Stub]:
    stub = Stub(base_url="")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            stub.requests.append(body)
            stub.auth_headers.append(self.headers.get("Authorization", ""))
            # Lowercased names: HTTP header names are case-insensitive.
            stub.headers.append({k.lower(): v for k, v in self.headers.items()})
            try:
                content, usage_extra = respond(body)
            except StubError as e:
                err = json.dumps({"error": {"message": f"stub {e.status}"}}).encode()
                self.send_response(e.status)
                for name, value in e.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            payload = {
                "id": f"stub-{len(stub.requests)}",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 200,
                    "total_tokens": 1200,
                    **usage_extra,
                },
            }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    stub.base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()
