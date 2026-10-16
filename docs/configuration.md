# Control-plane configuration

The control plane is configured by **defaults, then a TOML file, then
`CONTROL_PLANE_*` environment variables, then explicit code-level overrides**.
Later layers win. Environment variables stay authoritative, so existing
deployments that only set the environment keep working unchanged.

This document covers the control plane (`runtime-host/control-plane`). The
in-container application has its own configuration and is not described here.

## Where the file comes from

A config file is used only when a path is explicitly supplied. The control
plane never guesses a default path and never silently reads a file elsewhere on
disk.

* `python -m control_plane serve --config <path>` (or the bare `--config <path>`).
* `ControlPlaneConfig.from_env(environ, config_path="<path>")` from code.
* `CONTROL_PLANE_CONFIG=<path>` in the environment (also accepts the deployment
  alias `TUTORLM_CONTROL_PLANE_CONFIG`).

Precedence for the path itself: explicit `--config` / `config_path` beats
`CONTROL_PLANE_CONFIG`. A configured path that does not exist is an error; the
process refuses to start rather than run on defaults while the operator believes
the file took effect. When no path is configured, there is no file layer at all.

Only **TOML** is supported (standard-library `tomllib`). JSON and dotenv files
are deliberately not accepted: there is one format and one parser.

## File layout

```toml
# Optional root-level spelling of invitationonly (see below).
# invitationonly = false

[control_plane]
invitationonly = false
slots = 3
adapter = "docker"

[instance_env]
RUN_MODE = "demo"
DATA_DIR = "/data"
```

Two tables are recognised:

* `[control_plane]` — every scalar control-plane setting.
* `[instance_env]` — variables forwarded to instance containers. Keys are
  upper-cased and prefixed with `TUTORLM_` unless already prefixed, so
  `RUN_MODE = "demo"` becomes `TUTORLM_RUN_MODE=demo` in the container.

One optional root-level key is accepted:

* `invitationonly` — a bare boolean, equivalent to `invitationonly` inside
  `[control_plane]`. Root-level spellings are limited to unambiguous booleans;
  anything else must live in `[control_plane]`. Setting the root key and the
  table key with **different** values is rejected as ambiguous. The internal
  field is `invitation_only` (`CONTROL_PLANE_INVITATION_ONLY` in the
  environment).

A ready-to-copy template is `config.example.toml` at the repository root. It
contains no working credentials.

## Precedence

For every individual key, the effective value is chosen as:

| Layer | Source | Wins over |
| --- | --- | --- |
| 1 (lowest) | built-in defaults | — |
| 2 | `[control_plane]` / `[instance_env]` in the file | defaults |
| 3 | `CONTROL_PLANE_*` and `CONTROL_PLANE_INSTANCE_ENV_*` environment | file |
| 4 (highest) | explicit `**overrides` passed to `from_env(...)` | everything |

The environment wins even when it is blank. This preserves the historical
"blank means unset" behaviour for optional strings: `CONTROL_PLANE_NETWORK=""`
still disables the network setting even when the file sets `network = "..."`.

### Environment variable names

Each file key maps to an environment variable by upper-casing and prefixing
with `CONTROL_PLANE_`. Examples:

| File key (`[control_plane]`) | Environment variable |
| --- | --- |
| `slots` | `CONTROL_PLANE_SLOTS` |
| `instance_image` | `CONTROL_PLANE_INSTANCE_IMAGE` |
| `invitationonly` / `invitation_only` | `CONTROL_PLANE_INVITATION_ONLY` |
| `copy_helper_image` | `CONTROL_PLANE_COPY_HELPER_IMAGE` |
| `system_invitation_codes` | `CONTROL_PLANE_SYSTEM_INVITATION_CODES` (JSON array) |

`[instance_env]` keys use the existing `CONTROL_PLANE_INSTANCE_ENV_<NAME>`
form (`CONTROL_PLANE_INSTANCE_ENV_RUN_MODE=demo` → `TUTORLM_RUN_MODE=demo`) and
override file entries with the same target name.

## Strictness

Invalid configuration is rejected at load time; the process does not start on
guesswork.

* **Unknown keys are rejected.** A misspelled field in `[control_plane]` fails
  loudly instead of being ignored — a security switch must not silently stay
  off.
* **Types are enforced.** Booleans must be real TOML booleans (`true`/`false`);
  `gc_enabled = "yes"`, `gc_enabled = 1` and similar are rejected, never treated
  as truthy. Numeric fields reject strings and booleans; string fields reject
  numbers.
* **Ranges are enforced.** Negative or zero values for counts and timeouts are
  rejected with the same rules that already applied to the environment.

For environment booleans, only `1/0`, `true/false`, `yes/no`, `on/off`
(any case) are accepted; anything else is an error.

## Invitations

`invitationonly = true` (environment `CONTROL_PLANE_INVITATION_ONLY=true`)
requires every new visitor to present a valid invitation code. It defaults to
`false`.

`system_invitation_codes` is an optional, pre-provisioned list of system codes
that the control plane seeds at startup:

```toml
[control_plane]
system_invitation_codes = ["TLM-SYS-v1-<43 base64url chars>"]
```

* Codes must be **generated**, not hand-written. The single generator is
  `control_plane.invitations.generate_system_code()` (32 random bytes / 256
  bit). The format is `TLM-SYS-v<version>-<43 base64url chars>`.
* Weak or malformed strings are rejected at load time. The configuration layer
  only validates this format; it never generates codes.
* Via the environment the value is a JSON array:
  `CONTROL_PLANE_SYSTEM_INVITATION_CODES=["TLM-SYS-v1-..."]`.
* The admin config summary (`GET /control/admin/config`) reports only the
  **count** of configured codes and never their plaintext.

## Sharing helper image

`copy_helper_image` names a **local** image used only to copy a workspace volume
when a project is shared. It must already exist on the Docker host; the control
plane never pulls it. When empty it falls back to `instance_image`
(`resolved_copy_helper_image()`). The default is empty.

## Secrets

Keep `admin_token`, `token_pepper` and any instance API keys in the environment
or a protected file, not in a shared or committed template. `config.example.toml`
ships with empty secrets, and local config files are ignored by `.gitignore`
(`config.toml`, `config.*.toml`, `control-plane.toml`, `runtime-host/deploy/*.toml`).
The admin config summary never returns secret values — only boolean
"configured" flags.

## Local development

`scripts/dev-runtime-host.ps1` runs the control plane with the fake adapter and
does not require Docker or application sources.

```powershell
.\scripts\dev-runtime-host.ps1
```

Without a config file it sets development defaults (fake adapter, two slots,
local admin token and pepper). When a config file is in effect — via
`-ConfigPath` or an existing `CONTROL_PLANE_CONFIG` — the script leaves the file
authoritative and injects none of those overrides, so what the file says is what
runs. Environment values an operator sets still take precedence in both cases.
