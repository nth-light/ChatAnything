"""Helpers for publishing incremental output to the ``stream`` capability.

Streaming is strictly best effort: a missing broker or a broken subscriber must
never break the agent loop.  Every helper therefore degrades to a no-op.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..models import Session, SessionKind
from ..registry import Capability
from ..runtime import Runtime


def stream_key(trigger_id: str) -> str:
    """The canonical stream key for one trigger."""

    return f"trigger:{trigger_id}"


def stream_key_for_session(session: Session) -> str | None:
    """Resolve the stream key from the most recent user message."""

    for message in reversed(session.messages):
        if message.role == "user":
            trigger_id = message.metadata.get("trigger_id")
            if trigger_id:
                return stream_key(str(trigger_id))
    trigger_id = session.metadata.get("trigger_id")
    return stream_key(str(trigger_id)) if trigger_id else None


def resolve_broker(runtime: Runtime) -> Any | None:
    try:
        return runtime.resolve(Capability.STREAM)
    except Exception:  # noqa: BLE001 - streaming is optional
        return None


def stream_target(runtime: Runtime, session: Session) -> tuple[Any, str] | None:
    """Return ``(broker, key)`` for Worker sessions, else ``None``."""

    if session.kind is not SessionKind.WORKER:
        return None
    key = stream_key_for_session(session)
    if key is None:
        return None
    broker = resolve_broker(runtime)
    if broker is None:
        return None
    return broker, key


def publish(target: tuple[Any, str] | None, event: Mapping[str, Any]) -> None:
    """Publish ``event`` through a target produced by :func:`stream_target`."""

    if target is None:
        return
    broker, key = target
    try:
        broker.publish(key, dict(event))
    except Exception:  # noqa: BLE001 - never fail the agent loop on a stream error
        pass


def publish_trigger(runtime: Runtime, trigger_id: str, event: Mapping[str, Any]) -> None:
    """Publish an event for a Router-level decision (no Worker session yet)."""

    broker = resolve_broker(runtime)
    if broker is None:
        return
    try:
        broker.publish(stream_key(trigger_id), dict(event))
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "publish",
    "publish_trigger",
    "resolve_broker",
    "stream_key",
    "stream_key_for_session",
    "stream_target",
]
