# Outer Runtime

- Keep this tree independent of `instance-app/` Python and JavaScript implementations.
- Do not add application sessions, prompts, document parsing or plugin execution to the control plane.
- Preserve per-workspace volume ownership and generation checks; compute reclaim must retain durable project volumes.
- Delete durable data only after the workspace idle TTL and after all compute/quarantine references are gone. Anonymous v1 leases remain ephemeral.
- Admission returns only server-derived upstream addresses. Keep admin endpoints private at the gateway.
- Changes to readiness, application environment or instance lifecycle require `contracts/` review.
