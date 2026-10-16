"""Workflow contract (Host plugin SDK v1).

The canonical generation flow (docs/plugin-sdk.md)::

    validated input -> context builder -> prompt version -> model gateway
    -> output validator -> draft -> Host transaction commit

A workflow returns a :class:`Draft` and never writes to the database itself; the
Host validates session epoch / revision / cancellation and commits atomically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class WorkflowInput:
    """A validated, immutable request snapshot for one workflow invocation."""

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    session_id: str = ""
    run_id: str = ""

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


@dataclass(slots=True)
class Draft:
    """A not-yet-persisted workflow result.

    ``entity`` is the domain object (e.g. an ``Explanation``/``Narration``) the
    Host may commit through its own repository inside a short transaction;
    ``result`` is the JSON-safe payload returned to the client.
    """

    entity: Any = None
    result: Mapping[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    artifact_type: str = ""
    warnings: tuple[str, ...] = ()
    # Recheck mutable source data in the same transaction as the Host commit.
    precommit: Callable[[], None] | None = None


@runtime_checkable
class Workflow(Protocol):
    """A plugin-provided generation workflow."""

    @property
    def id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    def run(self, context: Any, data: WorkflowInput) -> Draft:
        """Execute against a :class:`ScopedContext` and return a draft."""


__all__ = ["Draft", "Workflow", "WorkflowInput"]
