"""Restricted contexts handed to plugins (Host plugin SDK v1).

Plugins never receive raw SQLite connections, root directories, plaintext API
keys, the Docker socket or subprocess handles. They get
these narrow Protocol surfaces instead.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Cancellation(Protocol):
    """Cooperative cancellation token passed into a running workflow."""

    @property
    def is_cancelled(self) -> bool: ...

    def check(self) -> None:
        """Raise if the run was cancelled (the plugin abort checkpoint)."""
        ...


@runtime_checkable
class RunContext(Protocol):
    """Run identity + provider metadata exposed to a workflow."""

    @property
    def run_id(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def input(self) -> dict[str, Any]: ...

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...


@runtime_checkable
class HostContext(Protocol):
    """Process-level context: no current user lives here.

    The Host owns the shared services; a plugin only sees the capability
    accessors the Host chooses to expose. The concrete object is supplied by
    the integrator and is intentionally opaque to the SDK.
    """

    @property
    def name(self) -> str: ...

    def capability(self, name: str) -> Any:
        """Return a granted capability handle or raise ``KeyError``."""
        ...


@runtime_checkable
class ScopedContext(Protocol):
    """Per-session/task context: ``principal`` belongs *only* here.

    ``container`` is the isolated per-session object graph, including nodes and edges; ``run``/``cancel``
    describe the in-flight task and ``emit`` publishes a run event.
    """

    @property
    def session_id(self) -> str: ...

    @property
    def container(self) -> Any: ...

    @property
    def run(self) -> RunContext: ...

    @property
    def cancel(self) -> Cancellation: ...

    @property
    def valid(self) -> Any:
        """Callable returning ``True`` while the session is still committable."""
        ...

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None: ...


__all__ = ["Cancellation", "HostContext", "RunContext", "ScopedContext"]
