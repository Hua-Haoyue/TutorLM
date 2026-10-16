"""In-container application package (instance-app/backend).

Layering (see docs/instance-app-design.md section 2 and backend/README.md):

    host/            composition root: HTTP assembly, HostConfig, sessions,
                     RunManager, plugin lifecycle, health
    domain/          pure domain models (owned by the foundation workstream)
    application/     ports and use cases (owned by the foundation workstream)
    infrastructure/  sqlite/storage/format/providers (foundation workstream)
    adapters/http    routes, schemas, error mapping (this package)
    adapters/plugins static plugins (foundation workstream)
    runtime/         run-mode wiring (foundation workstream)

This package never imports the control plane, never talks to Docker and never
reads the control-plane database. `scripts/check-boundaries.py` enforces the
static part of that rule.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"