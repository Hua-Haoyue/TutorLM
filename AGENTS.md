# Development Boundaries

- Read `docs/architecture.md` and `docs/development.md` before changing ownership boundaries.
- `runtime-host/` owns admission, leases, scheduling, container lifecycle and gateway deployment.
- `instance-app/` owns the in-container application, including `backend/host`, plugins and frontend.
- Neither side imports the other's private modules or reads the other's databases.
- Cross-boundary changes require a versioned contract change in `contracts/` and agreement from both owners.
- Use the fake runtime adapter for ordinary control-plane tests. Real Docker tests are explicitly opt-in.
- Never access remote infrastructure, deploy, run paid model calls, or download application images without explicit user authorization for that operation.
- Keep documentation focused on current local structure and workflows; do not add import provenance or deployment-history sections.
- Never commit `.env`, credentials, databases, Docker archives or `reference/`.
- Run control-plane tests and `python scripts/check-boundaries.py` before finishing relevant changes.
- Do not create a shared backend package to bypass HTTP contracts.
