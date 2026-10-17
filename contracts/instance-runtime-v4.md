# Managed Admission and Sharing v4

This extends v3 without changing the project data layout or application Bearer
protocol. A share string authorizes a content copy, not compute allocation.

## Admission

`POST /control/leases` accepts `workspace_id` and optional `invitation_code`.
When `invitation_only=true`, every new allocation or allocation request that
resumes a lease must provide a valid system invitation. Imported copies and
historical trial grants do not bypass this requirement. In open mode an omitted
invitation is allowed; a supplied invalid invitation is rejected, not ignored.
Valid system invitations receive highest priority and may preempt a lower-priority
active lease when full; equal highest-priority leases are not preempted.
Reading an existing lease and ordinary gateway requests do not reallocate slots.

## Application Sessions

The control plane injects a server-only, domain-separated, per-lease and per-
generation `TUTORLM_APP_BOOTSTRAP_TOKEN` and an external temporary session path.
It is neither a user credential nor a system invitation and must never appear
in public configuration, logs, frontend assets or copied project data.

After allocation the browser calls `POST /control/app-session` using its lease
cookie (or lease Bearer for scripts). No client upstream, workspace or owner is
accepted. The control plane validates the active lease and exact controlled
upstream, then calls `POST /internal/app-session` on that instance with the
`X-App-Bootstrap-Token` header. The app checks the configured token with a
constant-time comparison and issues a session bound to its own workspace.
The control plane revalidates the lease after the call and checks the returned
workspace before returning `{access_token,session}` with `Cache-Control:no-store`.
The gateway denies public `/internal/` access. Invalid/unconfigured bootstrap
tokens are denied. App business endpoints still require the app Bearer; the
browser never receives the bootstrap credential. Public app session issuance is
disabled when this credential is configured. Standalone demo sessions are unchanged.
No automatic fallback to unauthenticated or implicit application sessions exists.

## Share Strings

Owners call `POST /control/workspaces/{uuid}/invitations` and receive the unique
`code`, plus invitation metadata for expiry and revocation. The UI displays only
the copyable string, not a QR code or a fabricated application share URL.
The identifier is an unguessable capability, stored hashed; do not use a public
workspace UUID as a copy authorization token.

Receivers authenticate their own control-plane user and call
`POST /control/workspace-imports` with `{invitation_code:code}`. Import is
idempotent and creates the receiver's independent workspace and volume. It may
stop source compute to obtain a consistent copy. It does not allocate compute
or grant invitation-only access. Receivers then separately request a lease for
the destination workspace, supplying a system invitation if required.

Clone startup includes the immediate source workspace ID and the unique copy ID.
The app validates these against the copied current binding, preserves original
lineage and stable business IDs, and rebinds project ownership to the destination.
New empty data directories are initialized by an isolated no-network helper
before the non-root app starts; existing project contents are never recursively
chowned or replaced. All session credentials remain outside the project volume.
