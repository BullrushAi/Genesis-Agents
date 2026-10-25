"""Fake transport + in-memory local evaluation. No test in this package touches the network."""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from eval.genesis_client import RawResponse, TransportFailure


class FakeTransport:
    """Scripted transport.

    ``script`` is a list of either :class:`RawResponse`, :class:`TransportFailure`
    (raised), or a callable taking the recorded request dict. The last entry
    repeats once the script is exhausted, so a "always 503" case needs one entry.
    """

    def __init__(self, script: list[Any], health: Any = None) -> None:
        self.script = list(script)
        self.health = health if health is not None else RawResponse(200, '{"status":"ok"}')
        self.calls: list[dict[str, Any]] = []
        self.health_calls = 0
        self.closed = False
        self._idx = 0

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json_body: Any | None,
        timeout_s: float,
    ) -> RawResponse:
        record = {
            "method": method,
            "url": url,
            "headers": dict(headers),
            "json": json_body,
            "timeout_s": timeout_s,
        }
        if url.endswith("/health"):
            self.health_calls += 1
            if isinstance(self.health, BaseException):
                raise self.health
            return self.health

        self.calls.append(record)
        if not self.script:
            return RawResponse(200, '{"response":"ok"}')
        item = self.script[min(self._idx, len(self.script) - 1)]
        self._idx += 1
        if callable(item) and not isinstance(item, (RawResponse, BaseException)):
            item = item(record)
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True

    # -- assertions helpers ------------------------------------------------

    @property
    def run_call_count(self) -> int:
        return len(self.calls)

    def serialised_requests(self) -> str:
        return json.dumps(self.calls, default=str)


def ok_response(text: str = "hello", **extra: Any) -> RawResponse:
    body = {"response": text, "agentSlug": "genesis-knowledge-evidence",
            "agentName": "Genesis Research Agent"}
    body.update(extra)
    return RawResponse(200, json.dumps(body))


class RecordingSleep:
    """Replacement for asyncio.sleep that records delays instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)






class ExplodingAtEndTracer:
    """A tracer whose spans fail on exit — the "flush failed" shape."""

    class _Span:
        def set_attribute(self, *_a: Any, **_k: Any) -> None:
            return None

        def set_status(self, *_a: Any, **_k: Any) -> None:
            return None

        def get_span_context(self) -> Any:
            raise RuntimeError("no context")

        def end(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("failed to export span batch")

    class _Ctx:
        def __enter__(self) -> Any:
            return ExplodingAtEndTracer._Span()

        def __exit__(self, *_a: Any) -> None:
            raise RuntimeError("failed to export span batch")

    def start_as_current_span(self, *_a: Any, **_k: Any) -> Any:
        return self._Ctx()

    def start_span(self, *_a: Any, **_k: Any) -> Any:
        return self._Span()


def read_timeout(msg: str = "timed out waiting for response") -> TransportFailure:
    return TransportFailure("read", msg)


def connect_error(msg: str = "connection refused") -> TransportFailure:
    return TransportFailure("connect", msg)
