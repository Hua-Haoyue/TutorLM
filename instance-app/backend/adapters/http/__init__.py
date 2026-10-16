"""HTTP adapter: routing, request schemas, error mapping and safety headers.

Everything here is transport: it validates input, calls ``host.services`` and
renders the documented envelopes. No business rule and no storage access lives in
this package (``docs/instance-app-design.md`` sections 2 and 6).
"""
from .app import build_app, create_app

__all__ = ["build_app", "create_app"]