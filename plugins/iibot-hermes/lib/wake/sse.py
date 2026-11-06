"""Server-Sent-Events reader for one binding.

One daemon thread per bound session. The thread never claims, completes or pushes anything: it only
advances a cursor and reports "something changed" so the wake decision can be made elsewhere.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any, Callable, Optional

from .client import InboxClient, InboxError

logger = logging.getLogger(__name__)

STATE_CONNECTING = "connecting"
STATE_OPEN = "open"
STATE_RETRYING = "retrying"
STATE_STOPPED = "stopped"


class SseWorker(threading.Thread):
    """Reconnect-loop SSE reader bound to one inbox/session pair."""

    def __init__(
        self,
        *,
        client: InboxClient,
        inbox_id: str,
        last_event_id: str,
        on_event: Callable[[str, dict], None],
        on_state: Callable[[str, Optional[str]], None],
        stop_event: threading.Event,
        reconnect_base_ms: int = 1000,
        reconnect_max_ms: int = 60000,
        read_timeout_seconds: float = 45.0,
    ) -> None:
        super().__init__(name=f"inbox-wake-sse[{inbox_id}]", daemon=True)
        self.client = client
        self.inbox_id = inbox_id
        self.last_event_id = last_event_id or ""
        self.on_event = on_event
        self.on_state = on_state
        self.stop_event = stop_event
        self.reconnect_base_ms = max(100, int(reconnect_base_ms))
        self.reconnect_max_ms = max(self.reconnect_base_ms, int(reconnect_max_ms))
        # Idle time tolerated on one SSE connection; must exceed the server's heartbeat cadence.
        self.read_timeout_seconds = max(1.0, float(read_timeout_seconds))

    # ---- loop -----------------------------------------------------------

    def run(self) -> None:  # pragma: no cover - thread body
        attempt = 0
        while not self.stop_event.is_set():
            self.on_state(STATE_CONNECTING, None)
            try:
                response = self.client.open_events(
                    self.inbox_id,
                    last_event_id=self.last_event_id,
                    timeout=self.read_timeout_seconds,
                )
            except InboxError as exc:
                attempt += 1
                self.on_state(STATE_RETRYING, f"{exc.code}: {exc}")
                self._sleep_backoff(attempt)
                continue
            attempt = 0
            self.on_state(STATE_OPEN, None)
            try:
                self._consume(response)
            except (socket.timeout, TimeoutError):
                self.on_state(STATE_RETRYING, "heartbeat timeout")
            except Exception as exc:  # stream died
                self.on_state(STATE_RETRYING, f"stream error: {exc}")
            finally:
                try:
                    response.close()
                except Exception:
                    pass
            self._sleep_backoff(attempt + 1)
        self.on_state(STATE_STOPPED, None)

    def _consume(self, response: Any) -> None:
        event_id = ""
        event_name = "message"
        data_lines: list[str] = []
        for raw in response:
            if self.stop_event.is_set():
                return
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            for line in text.splitlines():
                if self.stop_event.is_set():
                    return
                if not line:
                    if data_lines:
                        self._dispatch(event_id, event_name, "\n".join(data_lines))
                        if event_id:
                            self.last_event_id = event_id
                    event_id, event_name, data_lines = "", "message", []
                    continue
                if line.startswith(":"):
                    continue  # heartbeat comment
                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "id":
                    event_id = value.strip()
                elif field == "event":
                    event_name = value.strip() or "message"
                elif field == "data":
                    data_lines.append(value)

    def _dispatch(self, event_id: str, event_name: str, data: str) -> None:
        payload: dict[str, Any] = {}
        if data:
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    payload = parsed
                else:
                    payload = {"value": parsed}
            except json.JSONDecodeError:
                payload = {"raw": data[:2000]}
        try:
            self.on_event(event_id, {"event": event_name, "data": payload})
        except Exception as exc:  # a bad consumer must not kill the stream
            logger.warning("inbox-wake: event handler failed: %s", exc)

    def _sleep_backoff(self, attempt: int) -> None:
        delay_ms = min(self.reconnect_max_ms, self.reconnect_base_ms * (2 ** max(0, attempt - 1)))
        self.stop_event.wait(delay_ms / 1000.0)
