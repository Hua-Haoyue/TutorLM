'''Unified TOML config file: layout, precedence, strict validation.

Every test uses an injected ``environ`` and an explicit ``config_path``; none of
them read an ambient file or touch the process environment.
'''

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.config import (
    ControlPlaneConfig,
    is_valid_system_invitation_code,
    parse_system_invitation_codes,
)
from control_plane.config_file import (
    CONFIG_PATH_ENV,
    load_config_file,
    parse_config_document,
    resolve_config_path,
)
from control_plane.errors import ConfigError
from control_plane.invitations import generate_system_code


def write_toml(tmp_path: Path, text: str, name: str = "cp.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------- path resolution


def test_resolve_config_path_prefers_explicit_and_reads_env(tmp_path: Path):
    explicit = write_toml(tmp_path, "", "explicit.toml")
    from_env_path = write_toml(tmp_path, "", "env.toml")
    assert resolve_config_path(explicit, {CONFIG_PATH_ENV: str(from_env_path)}) == explicit
    assert resolve_config_path(None, {CONFIG_PATH_ENV: str(from_env_path)}) == from_env_path
    assert resolve_config_path(None, {}) is None


def test_resolve_config_path_rejects_missing_file(tmp_path: Path):
    missing = tmp_path / "nope.toml"
    with pytest.raises(ConfigError) as exc:
        resolve_config_path(None, {CONFIG_PATH_ENV: str(missing)})
    assert "不存在" in str(exc.value)


def test_configured_path_is_not_silently_ignored(tmp_path: Path):
    """A typo'd CONTROL_PLANE_CONFIG must fail loudly, not fall back to defaults."""

    with pytest.raises(ConfigError):
        ControlPlaneConfig.from_env({CONFIG_PATH_ENV: str(tmp_path / "typo.toml")})


def test_from_env_without_path_never_reads_a_default_file(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("[control_plane]\nslots = 99\n", encoding="utf-8")
    config = ControlPlaneConfig.from_env({})
    assert config.slots == 3  # nothing was picked up implicitly


# ------------------------------------------------------------------- loading


def test_load_config_file_rejects_non_toml_suffix(tmp_path: Path):
    path = tmp_path / "cp.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config_file(path)
    assert "TOML" in str(exc.value)


def test_load_config_file_reports_syntax_error(tmp_path: Path):
    path = write_toml(tmp_path, "this is not = = toml\n")
    with pytest.raises(ConfigError):
        load_config_file(path)


# ------------------------------------------------------------------ precedence


def test_precedence_defaults_file_env_overrides(tmp_path: Path):
    path = write_toml(
        tmp_path,
        '''
[control_plane]
slots = 4
adapter = "docker"

[instance_env]
RUN_MODE = "file"
''',
    )
    from_file = ControlPlaneConfig.from_env({}, config_path=path)
    assert from_file.slots == 4
    assert from_file.instance_env == {"TUTORLM_RUN_MODE": "file"}

    from_env = ControlPlaneConfig.from_env(
        {"CONTROL_PLANE_SLOTS": "9", "CONTROL_PLANE_INSTANCE_ENV_RUN_MODE": "env"},
        config_path=path,
    )
    assert from_env.slots == 9
    assert from_env.instance_env == {"TUTORLM_RUN_MODE": "env"}

    from_overrides = ControlPlaneConfig.from_env({}, config_path=path, slots=7)
    assert from_overrides.slots == 7

    # defaults still apply for keys absent from the file
    assert from_file.instance_port == 8000


def test_env_blank_string_keeps_legacy_semantics(tmp_path: Path):
    """Env presence wins, and blank still means "unset" for optional strings."""

    path = write_toml(
        tmp_path,
        '''
[control_plane]
network = "file-net"
mem_limit = "1g"
tmpfs_size = "64m"
''',
    )
    config = ControlPlaneConfig.from_env(
        {
            "CONTROL_PLANE_NETWORK": "",
            "CONTROL_PLANE_MEM_LIMIT": "",
            "CONTROL_PLANE_TMPFS_SIZE": "",
        },
        config_path=path,
    )
    assert config.network is None
    assert config.mem_limit is None
    assert config.tmpfs_size is None


def test_file_values_used_when_env_absent(tmp_path: Path):
    path = write_toml(
        tmp_path,
        '''
[control_plane]
network = "file-net"
mem_limit = "1g"
cpus = 1.5
pids_limit = 128
tmpfs_size = "64m"
gc_enabled = false
reclaim_on_idle = true
lease_idle_ttl_seconds = 60
''',
    )
    config = ControlPlaneConfig.from_env({}, config_path=path)
    assert config.network == "file-net"
    assert config.mem_limit == "1g"
    assert config.cpus == 1.5
    assert config.pids_limit == 128
    assert config.tmpfs_size == "64m"
    assert config.gc_enabled is False
    assert config.reclaim_on_idle is True


# ------------------------------------------------------------------- invitation


def test_invitationonly_root_and_table_are_accepted(tmp_path: Path):
    root = write_toml(tmp_path, "invitationonly = true\n", "root.toml")
    table = write_toml(tmp_path, "[control_plane]\ninvitationonly = true\n", "table.toml")
    assert ControlPlaneConfig.from_env({}, config_path=root).invitation_only is True
    assert ControlPlaneConfig.from_env({}, config_path=table).invitation_only is True
    assert ControlPlaneConfig().invitation_only is False


def test_invitationonly_conflict_is_rejected(tmp_path: Path):
    path = write_toml(
        tmp_path,
        "invitationonly = true\n[control_plane]\ninvitationonly = false\n",
    )
    with pytest.raises(ConfigError) as exc:
        ControlPlaneConfig.from_env({}, config_path=path)
    assert "冲突" in str(exc.value)


def test_invitation_only_env_overrides_file(tmp_path: Path):
    path = write_toml(tmp_path, "[control_plane]\ninvitationonly = true\n")
    config = ControlPlaneConfig.from_env({"CONTROL_PLANE_INVITATION_ONLY": "false"}, config_path=path)
    assert config.invitation_only is False


@pytest.mark.parametrize("value", ["yes", "no", "1", "0", "on", "off", "true", "false"])
def test_invitation_only_env_boolean_spellings(value: str):
    config = ControlPlaneConfig.from_env({"CONTROL_PLANE_INVITATION_ONLY": value})
    assert config.invitation_only is (value in {"yes", "1", "on", "true"})


def test_invitation_only_env_rejects_unknown_spelling():
    with pytest.raises(ConfigError):
        ControlPlaneConfig.from_env({"CONTROL_PLANE_INVITATION_ONLY": "maybe"})


# ------------------------------------------------------------ system codes


def test_system_codes_from_file_and_env(tmp_path: Path):
    code = generate_system_code()
    path = write_toml(
        tmp_path,
        f'[control_plane]\nsystem_invitation_codes = ["{code}"]\n',
    )
    from_file = ControlPlaneConfig.from_env({}, config_path=path)
    assert from_file.system_invitation_codes == (code,)

    other = generate_system_code()
    from_env = ControlPlaneConfig.from_env(
        {"CONTROL_PLANE_SYSTEM_INVITATION_CODES": f'["{other}"]'},
        config_path=path,
    )
    assert from_env.system_invitation_codes == (other,)


def test_system_code_format_matches_core():
    """The config-layer check must agree with the invitation core on every code.

    Codes with a ``-`` inside the base64url payload are the interesting case: they
    are valid, so trimming the last dash would wrongly reject them.
    """

    from control_plane.invitations import SYSTEM_CODE_RE

    codes = [
        generate_system_code() for _ in range(50)
    ] + [
        "TLM-SYS-v1-fh_tDzQw76ZM-RJ6uEonPamE3oB0y7uW1AqZvgYHiEg",
        "TLM-SYS-v1-" + "A" * 43,
        "TLM-SYS-v1-short",
        "TLM-SYS-weak",
        "TLM-SYS-ABCD-EFGH-IJKM-NPQR",
        "TLM-SYS-v2-" + "A" * 43,
        "TLM-SYS-v1-" + "A" * 44,
    ]
    for code in codes:
        core = SYSTEM_CODE_RE.match(code) is not None
        assert is_valid_system_invitation_code(code) is core, code
    assert is_valid_system_invitation_code(generate_system_code()) is True


def test_system_code_parse_rejects_weak_and_duplicates():
    with pytest.raises(ConfigError):
        parse_system_invitation_codes(["TLM-SYS-weak"])
    with pytest.raises(ConfigError):
        parse_system_invitation_codes(["not-a-code"])
    code = generate_system_code()
    with pytest.raises(ConfigError):
        parse_system_invitation_codes([code, code])
    with pytest.raises(ConfigError):
        parse_system_invitation_codes({"a": "b"})
    assert parse_system_invitation_codes(None) == ()
    assert parse_system_invitation_codes("") == ()


# -------------------------------------------------------------- strict typing


def test_unknown_file_field_is_rejected(tmp_path: Path):
    path = write_toml(tmp_path, "[control_plane]\nnonsense = 1\n")
    with pytest.raises(ConfigError) as exc:
        ControlPlaneConfig.from_env({}, config_path=path)
    assert "nonsense" in str(exc.value)


def test_unknown_top_level_table_is_rejected(tmp_path: Path):
    path = write_toml(tmp_path, "[nonsense]\nx = 1\n")
    with pytest.raises(ConfigError):
        ControlPlaneConfig.from_env({}, config_path=path)


@pytest.mark.parametrize(
    "body",
    [
        'gc_enabled = "yes"',
        "gc_enabled = 1",
        "invitation_only = \"true\"",
        "reclaim_on_idle = \"no\"",
        "lease_cookie_secure = 1",
    ],
)
def test_file_booleans_must_be_real_booleans(tmp_path: Path, body: str):
    """A truthy string/int must never be accepted as a boolean security switch."""

    path = write_toml(tmp_path, f"[control_plane]\n{body}\n")
    with pytest.raises(ConfigError):
        ControlPlaneConfig.from_env({}, config_path=path)


@pytest.mark.parametrize(
    "body",
    ["slots = \"4\"", "instance_port = 1.5", "slots = true", "cpus = \"2\""],
)
def test_file_numeric_fields_are_type_checked(tmp_path: Path, body: str):
    path = write_toml(tmp_path, f"[control_plane]\n{body}\n")
    with pytest.raises(ConfigError):
        ControlPlaneConfig.from_env({}, config_path=path)


def test_negative_numbers_are_still_rejected(tmp_path: Path):
    path = write_toml(tmp_path, "[control_plane]\nslots = -2\n")
    with pytest.raises(ConfigError):
        ControlPlaneConfig.from_env({}, config_path=path)


def test_direct_construction_rejects_truthy_boolean():
    with pytest.raises(ConfigError):
        ControlPlaneConfig(gc_enabled="yes")  # type: ignore[arg-type]


# ---------------------------------------------------------- instance_env table


def test_instance_env_table_is_prefixed_and_typed(tmp_path: Path):
    path = write_toml(
        tmp_path,
        '''
[instance_env]
RUN_MODE = "demo"
TUTORLM_PROVIDER = "fake"
MAX_TOKENS = 1024
ENABLED = true
''',
    )
    config = ControlPlaneConfig.from_env({}, config_path=path)
    assert config.instance_env == {
        "TUTORLM_RUN_MODE": "demo",
        "TUTORLM_PROVIDER": "fake",
        "TUTORLM_MAX_TOKENS": "1024",
        "TUTORLM_ENABLED": "true",
    }


def test_instance_env_table_rejects_nested_values(tmp_path: Path):
    path = write_toml(tmp_path, "[instance_env]\nNESTED = { a = 1 }\n")
    with pytest.raises(ConfigError):
        ControlPlaneConfig.from_env({}, config_path=path)


# ------------------------------------------------------------------ copy helper


def test_copy_helper_image_defaults_empty_and_falls_back(tmp_path: Path):
    config = ControlPlaneConfig(instance_image="app:1.0")
    assert config.copy_helper_image == ""
    assert config.resolved_copy_helper_image() == "app:1.0"


def test_copy_helper_image_from_file(tmp_path: Path):
    path = write_toml(
        tmp_path,
        '[control_plane]\ninstance_image = "app:1.0"\ncopy_helper_image = "helper:local"\n',
    )
    config = ControlPlaneConfig.from_env({}, config_path=path)
    assert config.resolved_copy_helper_image() == "helper:local"

    # An explicit blank env value still falls back to the app image.
    blanked = ControlPlaneConfig.from_env({"CONTROL_PLANE_COPY_HELPER_IMAGE": ""}, config_path=path)
    assert blanked.copy_helper_image == ""
    assert blanked.resolved_copy_helper_image() == "app:1.0"


# ----------------------------------------------------------------- describe


def test_describe_reports_counts_without_secrets(tmp_path: Path):
    code = generate_system_code()
    path = write_toml(
        tmp_path,
        f'''
[control_plane]
invitationonly = true
admin_token = "top-secret"
copy_helper_image = "helper:local"
system_invitation_codes = ["{code}"]
''',
    )
    described = ControlPlaneConfig.from_env({}, config_path=path).describe()
    assert described["invitation_only"] is True
    assert described["system_invitation_codes_count"] == 1
    assert described["copy_helper_image_configured"] is True
    assert described["admin_token_configured"] is True
    assert code not in repr(described)
    assert "top-secret" not in repr(described)


# ------------------------------------------------------ parse_config_document


def test_parse_config_document_root_key_conflict():
    with pytest.raises(ConfigError):
        parse_config_document({"invitationonly": True, "invitation_only": False})


def assert_redacted_error(exc: ConfigError, secret: str) -> None:
    import traceback

    assert secret not in str(exc)
    assert secret not in repr(exc)
    assert secret not in "".join(traceback.format_exception(exc))


@pytest.mark.parametrize("kind", ["duplicate", "invalid", "non_string"])
@pytest.mark.parametrize("source", ["file", "env", "direct"])
def test_system_code_errors_are_redacted(tmp_path: Path, kind: str, source: str):
    import json

    secret = generate_system_code()
    codes = {
        "duplicate": [secret, secret],
        "invalid": [secret + "-invalid"],
        "non_string": [{"nested": secret}],
    }[kind]
    with pytest.raises(ConfigError) as caught:
        if source == "file":
            entry = f'"{secret}"'
            body = {
                "duplicate": f"[{entry}, {entry}]",
                "invalid": f'["{secret}-invalid"]',
                "non_string": f'[{{ nested = {entry} }}]',
            }[kind]
            path = write_toml(tmp_path, f"[control_plane]\nsystem_invitation_codes = {body}\n")
            ControlPlaneConfig.from_env({}, config_path=path)
        elif source == "env":
            ControlPlaneConfig.from_env({"CONTROL_PLANE_SYSTEM_INVITATION_CODES": json.dumps(codes)})
        else:
            ControlPlaneConfig(system_invitation_codes=codes)
    assert_redacted_error(caught.value, secret)


@pytest.mark.parametrize("field", ["admin_token", "token_pepper"])
def test_secret_field_type_errors_are_redacted(tmp_path: Path, field: str):
    secret = "private-credential-do-not-print"
    path = write_toml(tmp_path, f'[control_plane]\n{field} = ["{secret}"]\n')
    with pytest.raises(ConfigError) as caught:
        ControlPlaneConfig.from_env({}, config_path=path)
    assert_redacted_error(caught.value, secret)


def test_instance_api_key_type_error_is_redacted(tmp_path: Path):
    secret = "private-model-api-key"
    path = write_toml(tmp_path, f'[instance_env]\nMODEL_API_KEY = {{ value = "{secret}" }}\n')
    with pytest.raises(ConfigError) as caught:
        ControlPlaneConfig.from_env({}, config_path=path)
    assert_redacted_error(caught.value, secret)


def test_malformed_json_env_suppresses_parser_exception():
    secret = generate_system_code()
    with pytest.raises(ConfigError) as caught:
        ControlPlaneConfig.from_env({"CONTROL_PLANE_SYSTEM_INVITATION_CODES": f'["{secret}"'})
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert_redacted_error(caught.value, secret)


@pytest.mark.parametrize("invalid_utf8", [False, True])
def test_file_parser_errors_are_redacted(tmp_path: Path, invalid_utf8: bool):
    secret = "private-model-key"
    path = tmp_path / "broken.toml"
    payload = f'[instance_env]\nMODEL_API_KEY = "{secret}"\n'.encode()
    path.write_bytes(payload + (b'\xff' if invalid_utf8 else secret.encode()))
    with pytest.raises(ConfigError) as caught:
        ControlPlaneConfig.from_env({}, config_path=path)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert_redacted_error(caught.value, secret)


def test_duplicate_config_codes_do_not_leak_through_cli(tmp_path: Path):
    import io
    import traceback

    from control_plane.cli import main

    secret = generate_system_code()
    path = write_toml(
        tmp_path,
        f'[control_plane]\nsystem_invitation_codes = ["{secret}", "{secret}"]\n',
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    # The CLI owner handles ConfigError. Also verify fallback traceback rendering
    # while that independent change is being integrated.
    try:
        result = main(["serve", "--config", str(path)], environ={}, stdout=stdout, stderr=stderr)
    except ConfigError as exc:
        assert_redacted_error(exc, secret)
        traceback.print_exception(exc, file=stderr)
    else:
        assert result != 0
    assert stderr.getvalue()
    assert secret not in stdout.getvalue() + stderr.getvalue()
