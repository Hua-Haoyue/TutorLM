"""Result / operation DTOs shared with the Host run pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class OperationResult:
    """What a run ``operation`` may return to the RunManager.

    ``result`` is stored as the run's terminal payload and echoed by
    ``GET /api/runs/{id}``. ``provider``/``model`` are optional overrides so an
    operation can report the provider that actually produced the output.
    """

    result: Mapping[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = dict(self.result)
        if self.provider:
            payload.setdefault("provider", self.provider)
        if self.model:
            payload.setdefault("model", self.model)
        return payload


@dataclass(frozen=True, slots=True)
class RunResult:
    """Convenience alias carrying a run id alongside an :class:`OperationResult`."""

    run_id: str
    result: Mapping[str, Any] = field(default_factory=dict)


__all__ = ["OperationResult", "RunResult"]
