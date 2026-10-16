# Application Runtime

- This tree owns the in-container application. Frontend lease lifecycle helpers are present; business backend and full workspace UI are not implemented yet.
- Attach frontend/lease lifecycle once per document after obtaining a lease; use distinct page IDs per tab and never extend absolute compute TTL with presence heartbeats.
- Keep application layering explicit: backend/host, domain, application, infrastructure, adapters, plugins and plugin_contracts.
- Never mount the Docker socket, import control_plane, or access the control-plane database here.
- Build an immutable application image and export frontend artifacts for the outer gateway.
- Keep app Bearer sessions independent of lease cookies. Do not invent automatic token exchange.
- Durable mode must satisfy `contracts/instance-runtime-v2.md`, including workspace-bound readiness without a user Bearer token.
- Store project content under stable workspace paths, not ephemeral session directories. Session GC must never delete a persistent workspace.
