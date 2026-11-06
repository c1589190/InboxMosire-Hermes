"""``inbox.push`` delivery handling: verbatim text, delivery-id de-duplication, immediate ack.

Why this module exists and why it looks nothing like :mod:`template`
-------------------------------------------------------------------
``inbox.changed`` only carries counters, so the old path rebuilds a wake from ``/status`` +
``/items`` and renders it itself. ``inbox.push`` is the opposite: the server already rendered the
**whole body** (``data.text``) for the agent, and the operator ruling (D-002/D-015) is that the agent
sees exactly that one string — no header, no summary, no re-formatting, no trailing metadata line.
So :func:`parse_push` hands the text over untouched, and nothing here calls :mod:`safe` on it.

Everything in this module is pure (no I/O except :func:`local_media`'s ``os.path.isfile``), has no
import-side effects, and never logs a token.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from . import safe
from .client import InboxError
from .state import Store

logger = logging.getLogger(__name__)

#: SSE event name for one pushed delivery (``CloudEvents.sseEventName``).
PUSH_EVENT = "inbox.push"
#: CloudEvents ``type`` carried inside the SSE ``data`` envelope.
PUSH_TYPE = "io.mosire.inbox.push.v1"
#: The legacy counter-only event. Kept working: it drives the ``build_wake`` path.
CHANGED_EVENT = "inbox.changed"
CHANGED_TYPE = "io.mosire.inbox.changed.v1"

#: Bounded memory for de-duplication (a ``ctx.state`` value must stay small).
DELIVERY_LIMIT = 200
ITEM_LIMIT = 500
#: ``deliveryId`` is opaque to us; only bound its length before it is logged/stored.
DELIVERY_ID_MAX = 128

#: HTTP statuses worth one retry only; anything else in 4xx is permanent (wrong token scope ...).
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_REMOTE_PREFIXES = ("http://", "https://", "ftp://", "data:", "file://")


@dataclass(frozen=True)
class PushEnvelope:
    """One parsed ``inbox.push`` delivery (never carries credentials)."""

    delivery_id: str
    text: str
    item_ids: tuple[str, ...] = ()
    attachments: tuple[Any, ...] = ()
    important: bool = False
    remaining: int = 0
    revision: int = 0
    inbox: str = ""


# ---- event parsing -----------------------------------------------------


def is_push(event: Mapping[str, Any]) -> bool:
    """True when *event* (``{"event": name, "data": payload}``) is a push delivery."""
    if not isinstance(event, Mapping):
        return False
    if str(event.get("event") or "") == PUSH_EVENT:
        return True
    data = event.get("data")
    if isinstance(data, Mapping) and str(data.get("type") or "") == PUSH_TYPE:
        return True
    # A flat payload (no CloudEvents envelope) still names its delivery id.
    return isinstance(data, Mapping) and "deliveryId" in data and "text" in data


def unwrap_payload(data: Any) -> Mapping[str, Any]:
    """Return the push payload from a CloudEvents envelope or from a flat ``data`` object.

    The server sends ``data`` as a CloudEvents 1.0 envelope whose own ``data`` field is the
    contract payload (``CloudEvents.envelope`` in InboxMosire). Some producers/tests hand the
    payload over directly, so both shapes are accepted.
    """
    if not isinstance(data, Mapping):
        return {}
    inner = data.get("data")
    if isinstance(inner, Mapping) and ("deliveryId" in inner or "text" in inner):
        return inner
    return data


def parse_push(event: Mapping[str, Any]) -> Optional[PushEnvelope]:
    """Parse one push event, or ``None`` when it cannot be delivered safely.

    ``text`` is returned **verbatim** — no stripping, no re-wrapping, no truncation.
    """
    payload = unwrap_payload(event.get("data") if isinstance(event, Mapping) else None)
    if not payload:
        return None
    delivery_id = safe.clean_text(payload.get("deliveryId"), DELIVERY_ID_MAX).strip()
    if not safe.is_safe_id(delivery_id):
        return None
    raw_text = payload.get("text")
    if raw_text is None:
        return None
    text = raw_text if isinstance(raw_text, str) else str(raw_text)
    if not text.strip():
        return None
    item_ids = tuple(
        item for item in _as_list(payload.get("itemIds")) if isinstance(item, str) and safe.is_safe_id(item)
    )
    return PushEnvelope(
        delivery_id=delivery_id,
        text=text,
        item_ids=item_ids,
        attachments=tuple(_as_list(payload.get("attachments"))),
        important=bool(payload.get("important")),
        remaining=_as_int(payload.get("remaining")),
        revision=_as_int(payload.get("revision")),
        inbox=safe.clean_text(payload.get("inbox"), 64),
    )


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---- attachments -------------------------------------------------------


def attachment_ref(entry: Any) -> str:
    """One attachment's reference string (the contract ships ``{"name","ref"}`` records)."""
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, Mapping):
        for key in ("ref", "path", "url", "file"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def local_media(attachments: Iterable[Any]) -> tuple[list[str], list[str]]:
    """Split attachments into ``(local absolute paths, skipped refs)``.

    Only files this process can actually hand to ``inject_message(media_urls=...)`` are returned:
    the host validates ``os.path.isabs`` + ``os.path.isfile`` and refuses to fetch remote URLs.
    Nothing is downloaded here (the plugin has no business fetching producer-supplied URLs).
    """
    urls: list[str] = []
    skipped: list[str] = []
    for entry in attachments:
        ref = attachment_ref(entry)
        if not ref:
            continue
        lowered = ref.lower()
        if lowered.startswith(_REMOTE_PREFIXES) or "://" in ref:
            skipped.append(ref)
            continue
        if not os.path.isabs(ref) or not os.path.isfile(ref):
            skipped.append(ref)
            continue
        if ref not in urls:
            urls.append(ref)
    return urls, skipped


# ---- de-duplication (persisted per session) ----------------------------


def _blank_state(inbox_id: str) -> dict[str, Any]:
    return {"inboxId": inbox_id, "deliveryIds": [], "itemIds": []}


def delivery_state(store: Store, session_key: str, inbox_id: str = "") -> dict[str, Any]:
    """Persisted dedupe state for one binding; a different inbox starts from scratch."""
    state = store.push_state(session_key)
    if not isinstance(state, dict) or not state:
        return _blank_state(inbox_id)
    stored_inbox = str(state.get("inboxId") or "")
    if inbox_id and stored_inbox and stored_inbox != inbox_id:
        return _blank_state(inbox_id)
    return {
        "inboxId": stored_inbox or inbox_id,
        "deliveryIds": [i for i in _as_list(state.get("deliveryIds")) if isinstance(i, str)][-DELIVERY_LIMIT:],
        "itemIds": [i for i in _as_list(state.get("itemIds")) if isinstance(i, str)][-ITEM_LIMIT:],
    }


def delivery_seen(store: Store, session_key: str, delivery_id: str, inbox_id: str = "") -> bool:
    """True when this ``deliveryId`` was already injected (survives a process restart)."""
    return delivery_id in set(delivery_state(store, session_key, inbox_id)["deliveryIds"])


def mark_delivered(
    store: Store,
    session_key: str,
    delivery_id: str,
    item_ids: Sequence[str] = (),
    inbox_id: str = "",
) -> dict[str, Any]:
    """Record one injected delivery; the ``itemIds`` feed ``build_wake(skip_item_ids=...)``."""
    state = delivery_state(store, session_key, inbox_id)
    deliveries = list(state["deliveryIds"])
    if delivery_id not in deliveries:
        deliveries.append(delivery_id)
    items = list(state["itemIds"])
    for item_id in item_ids:
        if isinstance(item_id, str) and item_id and item_id not in items:
            items.append(item_id)
    updated = {
        "inboxId": state.get("inboxId") or inbox_id,
        "deliveryIds": deliveries[-DELIVERY_LIMIT:],
        "itemIds": items[-ITEM_LIMIT:],
    }
    store.put_push_state(session_key, updated)
    return updated


def skipped_item_ids(store: Store, session_key: str, inbox_id: str = "") -> list[str]:
    """Items already delivered verbatim by a push — the ``inbox.changed`` render must skip them."""
    return list(delivery_state(store, session_key, inbox_id)["itemIds"])


# ---- ack ---------------------------------------------------------------

AckCall = Callable[[str, str], Mapping[str, Any]]


def ack_delivery(
    call: AckCall,
    *,
    inbox_id: str,
    delivery_id: str,
    retries: int = 1,
) -> tuple[bool, str, int]:
    """Ack one delivery through *call*; at most ``retries`` extra attempts, never raises.

    Returns ``(ok, error, attempts)``. Transport failures and 5xx get one retry; a 4xx is
    permanent (a token without ``items:manage`` cannot be fixed by trying again). This never
    re-injects anything — the text was already delivered before the ack was attempted.
    """
    attempts = 0
    error = ""
    budget = max(0, int(retries))
    while True:
        attempts += 1
        try:
            result = call(inbox_id, delivery_id)
        except InboxError as exc:
            error = f"{exc.code}: {safe.clean_text(str(exc), 200)}"
            retryable = exc.status is None or exc.status in _TRANSIENT_STATUS
            if not retryable or attempts > budget:
                return False, error, attempts
        except Exception as exc:  # noqa: BLE001 - an ack must never break the SSE thread
            error = f"{type(exc).__name__}: {safe.clean_text(str(exc), 200)}"
            if attempts > budget:
                return False, error, attempts
        else:
            if isinstance(result, Mapping) and result.get("acked") is False:
                return True, "acked=false", attempts
            return True, "", attempts
