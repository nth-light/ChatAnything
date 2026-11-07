"""Two-stage filter pipeline used before routing."""

from __future__ import annotations

from ..lifecycle import LifecyclePoint
from ..models import FilterDecision, FilterStage, Trigger
from ..registry import Capability
from ..runtime import Runtime


class FilterPipeline:
    """Run every registered filter of a stage, in priority order."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def _stage_filters(self, stage: FilterStage) -> list[object]:
        filters = [
            candidate
            for candidate in self.runtime.resolve_all(Capability.FILTER)
            if getattr(candidate, "stage", None) == stage
        ]
        filters.sort(key=lambda item: getattr(item, "priority", 100))
        return filters

    async def evaluate(self, trigger: Trigger, stage: FilterStage) -> FilterDecision:
        filters = self._stage_filters(stage)
        if not filters:
            return FilterDecision(True, f"no {stage.value} filters registered", stage)

        accepted = True
        reasons: list[str] = []
        confidence = 1.0
        decisions: list[FilterDecision] = []
        for filter_ in filters:
            try:
                decision = await filter_.evaluate(trigger, self.runtime)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - fail open for observability
                self.runtime.logger.exception("filter failed: %s", exc)
                decision = FilterDecision(True, f"filter error: {exc}", stage, 0.0)
            decisions.append(decision)
            if not decision.accepted:
                accepted = False
                reasons.append(decision.reason)
            confidence = min(confidence, decision.confidence)

        result = FilterDecision(
            accepted=accepted,
            reason="; ".join(reasons) if reasons else f"passed {stage.value} filters",
            stage=stage,
            confidence=confidence,
            metadata={
                "filters": [getattr(item, "name", type(item).__name__) for item in filters],
                "decisions": decisions,
            },
        )
        await self.runtime.lifecycle.emit(
            LifecyclePoint.TRIGGER_FILTERED,
            trigger=trigger,
            stage=stage.value,
            decision=result,
        )
        if not accepted:
            await self.runtime.lifecycle.emit(
                LifecyclePoint.TRIGGER_IGNORED,
                trigger=trigger,
                stage=stage.value,
                reason=result.reason,
            )
        return result
