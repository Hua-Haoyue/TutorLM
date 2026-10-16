"""RunManager: run state machine plus bounded per-workload executors.

Documented state machine (docs/instance-app-design.md section 5):

    queued -> running -> completed | failed | cancelled | interrupted

Hard requirements implemented here:

* bounded executors with a fixed worker count and a bounded queue; a full queue
  fails fast with 503 instead of growing without limit;
* workload classes are separate (`model` / `extract` / `tex_compile`), so a long
  TeX job cannot starve the model queue;
* **cancel prevents commit**: the result of a cancelled run is never written;
* a hard stop/reap marks in-flight runs `interrupted` and never replays a paid
  provider call.

Provider calls run outside any database transaction: the worker reads its input,
releases the connection, calls the provider, then re-opens a transaction and
re-validates state before committing.
"""
from __future__ import annotations

import json
import queue
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .errors import AppError, IdempotencyConflict, NotFound, RunQueueFull
from backend.plugin_contracts import Draft
from backend.domain.errors import DomainError

WORKLOAD_MODEL = "model"
WORKLOAD_EXTRACT = "extract"
WORKLOAD_TEX_COMPILE = "tex_compile"

TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})


class RunStore(Protocol):
    """Port the RunManager needs from the repository layer."""

    def create_run(self, **kwargs: Any) -> tuple[dict[str, Any], bool]: ...
    def get_run(self, run_id: str) -> dict[str, Any] | None: ...
    def list_runs(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    def count_runs(self, session_id: str) -> int: ...
    def mark_run_state(self, run_id: str, state: str, **fields: Any) -> dict[str, Any] | None: ...
    def interrupt_active_runs(self) -> int: ...


@dataclass
class RunContext:
    """What a handler is allowed to see. No store handle is handed out blindly."""

    run_id: str
    kind: str
    workload: str
    project_id: str | None
    session_id: str | None
    target_id: str | None
    payload: dict[str, Any]
    store: RunStore


class RunHandler(Protocol):
    def __call__(self, ctx: RunContext) -> Mapping[str, Any] | Draft | RunDraft:
        """Return the result payload to commit, or raise `AppError`."""


class CancelledRun(Exception):
    """Raised internally so a handler can stop early when its run was cancelled."""


@dataclass
class RunDraft:
    publish: Callable[[], Mapping[str, Any]]


class BoundedExecutor:
    """Fixed worker pool over a bounded queue; never blocks the caller."""

    def __init__(self, name: str, workers: int, queue_size: int) -> None:
        self.name = name
        self.workers = max(1, int(workers))
        self.queue: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._start()

    def _start(self) -> None:
        for index in range(self.workers):
            thread = threading.Thread(target=self._run, name=f"{self.name}-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                task = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                task()
            except Exception:  # pragma: no cover - defensive: workers never die
                pass
            finally:
                self.queue.task_done()

    def submit(self, task: Callable[[], None]) -> bool:
        try:
            self.queue.put_nowait(task)
        except queue.Full:
            return False
        return True

    @property
    def queued(self) -> int:
        return self.queue.qsize()

    def shutdown(self, *, wait: bool = False, timeout: float = 5.0) -> None:
        self._stopping.set()
        if wait:
            for thread in self._threads:
                thread.join(timeout=timeout)


class RunManager:
    """Owns run lifecycle bookkeeping for one Host instance."""

    def __init__(
        self,
        store: RunStore,
        *,
        max_runs_per_session: int = 20,
        executors: Mapping[str, tuple[int, int]] | None = None,
    ) -> None:
        self.store = store
        self.max_runs_per_session = int(max_runs_per_session)
        self._handlers: dict[str, RunHandler] = {}
        self._lock = threading.RLock()
        self.session_valid: Callable[[str], bool] = lambda _: True
        self.draft_committer = None
        config = executors or {
            WORKLOAD_MODEL: (2, 32),
            WORKLOAD_EXTRACT: (1, 8),
            WORKLOAD_TEX_COMPILE: (1, 4),
        }
        self.executors: dict[str, BoundedExecutor] = {
            name: BoundedExecutor(f"run-{name}", workers, size) for name, (workers, size) in config.items()
        }
        self._interrupted_on_start = 0

    # -- registration ----------------------------------------------------
    def register(self, kind: str, handler: RunHandler) -> None:
        with self._lock:
            self._handlers[kind] = handler

    def handler_for(self, kind: str) -> RunHandler | None:
        return self._handlers.get(kind)

    @contextmanager
    def publication_guard(self):
        with self._lock:
            yield

    # -- startup ---------------------------------------------------------
    def reclaim_interrupted(self) -> int:
        """Called once at startup: active runs from a previous life are closed."""
        self._interrupted_on_start = self.store.interrupt_active_runs()
        return self._interrupted_on_start

    # -- submit / cancel -------------------------------------------------
    def submit(
        self,
        *,
        kind: str,
        workload: str,
        input_payload: Mapping[str, Any],
        project_id: str | None,
        session_id: str | None,
        idempotency_key: str | None,
        idempotency_scope: str | None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        handler = self.handler_for(kind)
        if handler is None:
            raise NotFound(f"No handler registered for run kind {kind!r}", code="unknown_run_kind")

        if session_id is not None and self.max_runs_per_session:
            from .errors import RunLimitReached

            if self.store.count_runs(session_id) >= self.max_runs_per_session:
                raise RunLimitReached()

        run, created = self.store.create_run(
            kind=kind,
            workload=workload,
            project_id=project_id,
            input_payload=input_payload,
            idempotency_key=idempotency_key,
            idempotency_scope=idempotency_scope,
            session_id=session_id,
            target_id=target_id,
        )

        if not created:
            # Same key, same scope: replay only when the body matches exactly.
            previous = _input_of(run)
            if previous != dict(input_payload):
                raise IdempotencyConflict(
                    details={"run_id": run.get("id"), "kind": run.get("kind")}
                )
            return run

        executor = self.executors.get(workload)
        if executor is None:
            raise NotFound(f"Unknown workload class {workload!r}", code="unknown_workload")

        accepted = executor.submit(lambda: self._execute(run["id"], kind, workload, handler))
        if not accepted:
            self.store.mark_run_state(
                run["id"],
                "failed",
                error_code="run_queue_full",
                error_message="Run queue is full; the task was rejected before starting",
            )
            raise RunQueueFull()
        return run

    def cancel(self, run_id: str) -> dict[str, Any]:
        """Mark a queued/running run cancelled. Its result is never committed."""
        with self._lock:
            return self._cancel(run_id)

    def _cancel(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise NotFound("Run not found", code="run_not_found")
        if run.get("state") in TERMINAL_STATES:
            return run
        updated = self.store.mark_run_state(
            run_id,
            "cancelled",
            error_code="cancelled",
            error_message="Run cancelled by the client; its result was discarded",
        )
        return updated or run

    def is_cancelled(self, run_id: str) -> bool:
        run = self.store.get_run(run_id)
        if run is None:
            return True
        return run.get('state') in TERMINAL_STATES

    # -- execution -------------------------------------------------------
    def _execute(self, run_id: str, kind: str, workload: str, handler: RunHandler) -> None:
        run = self.store.get_run(run_id)
        if run is None or run.get("state") != "queued":
            return  # cancelled or already handled before we started

        self.store.mark_run_state(run_id, "running", progress=None)

        # Re-read state right before doing paid work; a cancel that arrived
        # between submit and dequeue must not spend a provider call.
        if self.is_cancelled(run_id):
            return

        ctx = RunContext(
            run_id=run_id,
            kind=kind,
            workload=workload,
            project_id=run.get("project_id"),
            session_id=run.get("session_id"),
            target_id=run.get("target_id"),
            payload=_input_of(run),
            store=self.store,
        )

        try:
            result = handler(ctx)
        except CancelledRun:
            self.store.mark_run_state(
                run_id,
                "cancelled",
                error_code="cancelled",
                error_message="Run cancelled by the client; its result was discarded",
            )
            return
        except AppError as exc:
            self._fail(run_id, exc)
            return
        except DomainError as exc:
            self._fail(run_id, AppError(str(exc), code=exc.code))
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._fail(run_id, AppError("Run failed", code="run_failed", details={"reason": type(exc).__name__}))
            return

        # Cancel-aware commit: no result is written after a cancellation.
        with self._lock:
            latest = self.store.get_run(run_id)
            if latest is None or latest.get('state') != 'running':
                return
            if ctx.session_id and not self.session_valid(ctx.session_id):
                self._cancel(run_id)
                return
            try:
                transaction = getattr(self.store, 'transaction', nullcontext)
                with transaction():
                    if ctx.session_id and not self.session_valid(ctx.session_id):
                        raise CancelledRun()
                    if isinstance(result, Draft):
                        if self.draft_committer is None:
                            raise AppError('Draft committer is not configured')
                        result = self.draft_committer(ctx, result)
                    elif isinstance(result, RunDraft):
                        result = result.publish()
                    self.store.mark_run_state(
                        run_id, 'completed', progress=100,
                        result_json=json.dumps(dict(result), ensure_ascii=False),
                        error_code=None, error_message=None,
                    )
            except AppError as exc:
                self._fail(run_id, exc)
            except CancelledRun:
                self._cancel(run_id)
            except DomainError as exc:
                self._fail(run_id, AppError(str(exc), code=exc.code))
            except Exception:
                self._fail(run_id, AppError('Run publication failed', code='run_failed'))

    def _fail(self, run_id: str, exc: AppError) -> None:
        latest = self.store.get_run(run_id)
        if latest is None or latest.get("state") in TERMINAL_STATES:
            return
        self.store.mark_run_state(
            run_id,
            "failed",
            error_code=exc.code,
            error_message=exc.message_text,
        )

    # -- reads -----------------------------------------------------------
    def get(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise NotFound("Run not found", code="run_not_found")
        return run

    def shutdown(self, *, wait: bool = False) -> None:
        """Stop workers; in-flight rows stay `running` until the next start
        marks them `interrupted` (no automatic replay of provider calls)."""
        with self._lock:
            self.store.interrupt_active_runs()
            for executor in self.executors.values():
                executor.shutdown(wait=False)
        if wait:
            for executor in self.executors.values():
                executor.shutdown(wait=True)


def _input_of(run: Mapping[str, Any]) -> dict[str, Any]:
    raw = run.get("input_json")
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        payload = json.loads(str(raw))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "BoundedExecutor",
    "CancelledRun",
    "RunContext",
    "RunHandler",
    "RunManager",
    "RunStore",
    "TERMINAL_STATES",
    "WORKLOAD_EXTRACT",
    "WORKLOAD_MODEL",
    "WORKLOAD_TEX_COMPILE",
]
