# Outer Runtime

- Keep this tree independent of `instance-app/` Python and JavaScript implementations.
- Do not add application sessions, prompts, document parsing or plugin execution to the control plane.
- Preserve per-slot volume isolation and generation checks; failures must not silently free dirty slots.
- Admission returns only server-derived upstream addresses. Keep admin endpoints private at the gateway.
- Changes to readiness, application environment or instance lifecycle require `contracts/` review.
