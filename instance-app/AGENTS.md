# Application Runtime

- This tree is reserved for the in-container application; implementation is not present yet.
- Keep application layering explicit: backend/host, domain, application, infrastructure, adapters, plugins and plugin_contracts.
- Never mount the Docker socket, import control_plane, or access the control-plane database here.
- Build an immutable application image and export frontend artifacts for the outer gateway.
- Keep app Bearer sessions independent of lease cookies. Do not invent automatic token exchange.
- Readiness must satisfy `contracts/instance-runtime-v1.md` without a user Bearer token.
