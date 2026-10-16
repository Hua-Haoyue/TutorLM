"""Host composition root and Host-level concerns (docs/instance-app-design.md section 2).

Owns: HTTP assembly, application sessions and quotas, RunManager, plugin
lifecycle, storage wiring, /health and /health/ready.

Must not own: Docker socket access, lease database access, invitation/share
implementation, outer identity translation, upstream address selection.
"""