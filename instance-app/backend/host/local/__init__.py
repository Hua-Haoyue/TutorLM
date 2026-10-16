"""Compatibility fallback implementations for Host ports.

The foundation workstream owns `domain/`, `application/ports` and
`infrastructure/` (docs/instance-app-design.md section 2). Until those modules
exist in this checkout, the HTTP host needs *something* to run against, so this
package provides minimal, explicitly-scoped implementations of exactly the ports
the HTTP layer consumes:

    LocalStore      ~ infrastructure/sqlite repositories + UnitOfWork
    LocalStorage    ~ infrastructure/storage (immutable sources, artifacts)
    SessionStore    ~ volume-external application session store
    FakeProvider    ~ infrastructure/providers/fake

Replacement rule: `create_app(...)` accepts these objects as keyword arguments.
When the foundation lands, inject the real repositories/services there; routes in
`adapters/http` only talk to the Host-facing services and do not change. These
classes must never grow domain rules that belong in `domain/`.
"""
from .provider import FakeProvider
from .sessions import SessionStore
from .store import LocalStorage, LocalStore, PathEscape

__all__ = ["FakeProvider", "LocalStorage", "LocalStore", "PathEscape", "SessionStore"]