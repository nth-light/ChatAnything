"""Best-effort resource accounting for loaded plugins.

CPU time is measured with :func:`time.process_time` while plugin lifecycle
stages and hooks run.  Memory is a process-wide ``tracemalloc`` approximation;
Python offers no reliable per-object accounting across arbitrary extensions.
"""

from __future__ import annotations

import time
import tracemalloc
from dataclasses import dataclass
from typing import Any


class ResourceMeter:
    """Tracks process CPU time and optionally traced memory."""

    def __init__(self, *, track_memory: bool = False) -> None:
        self.track_memory = track_memory
        self._owns_tracing = False
        if track_memory and not tracemalloc.is_tracing():
            tracemalloc.start(1)
            self._owns_tracing = True

    # ------------------------------------------------------------------
    def cpu_seconds(self) -> float:
        return time.process_time()

    def memory_snapshot(self) -> dict[str, int] | None:
        if not tracemalloc.is_tracing():
            return None
        current, peak = tracemalloc.get_traced_memory()
        return {"current": current, "peak": peak}

    def baseline(self) -> int | None:
        snapshot = self.memory_snapshot()
        return snapshot["current"] if snapshot else None

    def delta(self, baseline: int | None) -> int | None:
        snapshot = self.memory_snapshot()
        if snapshot is None or baseline is None:
            return None
        return max(0, snapshot["current"] - baseline)

    def process_usage(self) -> dict[str, Any]:
        usage: dict[str, Any] = {"cpu_seconds": self.cpu_seconds()}
        snapshot = self.memory_snapshot()
        if snapshot is not None:
            usage["traced_memory_bytes"] = snapshot["current"]
            usage["peak_memory_bytes"] = snapshot["peak"]
        return usage

    def close(self) -> None:
        if self._owns_tracing and tracemalloc.is_tracing():
            tracemalloc.stop()
        self._owns_tracing = False


@dataclass(slots=True)
class StageMeasurement:
    """A single measured lifecycle stage."""

    label: str
    seconds: float
    cpu_seconds: float
