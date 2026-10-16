"""配置解析与校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.config import (
    ADAPTER_FAKE,
    ControlPlaneConfig,
    instance_env_from_environ,
)
from control_plane.errors import ConfigError


def test_defaults_are_valid():
    config = ControlPlaneConfig()
    assert config.slots >= 1
    assert config.instance_port == 8000
    assert config.public_port == 47823


def test_env_overrides_are_read(tmp_path: Path):
    environ = {
        "CONTROL_PLANE_SLOTS": "5",
        "CONTROL_PLANE_INSTANCE_IMAGE": "tutorlm-host:1.2.3",
        "CONTROL_PLANE_ADAPTER": "fake",
        "CONTROL_PLANE_ADMIN_TOKEN": "s3cret",
        "CONTROL_PLANE_LEASE_TTL_SECONDS": "120",
        "CONTROL_PLANE_PUBLIC_PORT": "47823",
        "CONTROL_PLANE_DB_PATH": str(tmp_path / "db.sqlite3"),
    }
    config = ControlPlaneConfig.from_env(environ)
    assert config.slots == 5
    assert config.instance_image == "tutorlm-host:1.2.3"
    assert config.adapter == ADAPTER_FAKE
    assert config.admin_token == "s3cret"
    assert config.lease_ttl_seconds == 120
    assert config.resolved_db_path() == tmp_path / "db.sqlite3"


def test_instance_env_prefix_is_rewritten():
    """CONTROL_PLANE_INSTANCE_ENV_* -> TUTORLM_*，其余变量不受影响。"""

    environ = {
        "CONTROL_PLANE_INSTANCE_ENV_RUN_MODE": "demo",
        "CONTROL_PLANE_INSTANCE_ENV_MODEL_NAME": "qwen",
        "CONTROL_PLANE_SLOTS": "3",
        "PATH": "/usr/bin",
    }
    forwarded = instance_env_from_environ(environ)
    assert forwarded == {"TUTORLM_RUN_MODE": "demo", "TUTORLM_MODEL_NAME": "qwen"}


def test_empty_instance_env_suffix_is_ignored():
    assert instance_env_from_environ({"CONTROL_PLANE_INSTANCE_ENV_": "x"}) == {}


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"slots": 0}, "SLOTS"),
        ({"adapter": "kubernetes"}, "ADAPTER"),
        ({"instance_port": 0}, "INSTANCE_PORT"),
        ({"public_port": 70000}, "PUBLIC_PORT"),
        ({"ready_timeout_seconds": 0}, "READY_TIMEOUT"),
        ({"lease_ttl_seconds": 0}, "LEASE_TTL"),
        ({"lease_idle_ttl_seconds": -1}, "LEASE_IDLE"),
        ({'workspace_idle_ttl_seconds': 0}, 'WORKSPACE_IDLE_TTL'),
        ({'page_heartbeat_timeout_seconds': 0}, 'PAGE_HEARTBEAT_TIMEOUT'),
        ({'page_close_grace_seconds': -1}, 'PAGE_CLOSE_GRACE'),
        ({"instance_mount_path": "data"}, "MOUNT_PATH"),
        ({"container_prefix": ""}, "CONTAINER_PREFIX"),
        ({"volume_prefix": ""}, "VOLUME_PREFIX"),
    ],
)
def test_invalid_config_is_rejected(overrides: dict, message: str):
    with pytest.raises(ConfigError) as exc:
        ControlPlaneConfig(**overrides)
    assert message in str(exc.value)


def test_reclaim_on_idle_requires_idle_ttl():
    with pytest.raises(ConfigError):
        ControlPlaneConfig(reclaim_on_idle=True, lease_idle_ttl_seconds=0)
    # 给了空闲阈值就允许
    config = ControlPlaneConfig(reclaim_on_idle=True, lease_idle_ttl_seconds=60)
    assert config.reclaim_on_idle is True


def test_non_integer_env_is_rejected():
    with pytest.raises(ConfigError):
        ControlPlaneConfig.from_env({"CONTROL_PLANE_SLOTS": "three"})


def test_volume_and_container_names_are_derived():
    config = ControlPlaneConfig(volume_prefix="vol", container_prefix="inst")
    assert config.volume_name(3) == "vol-s03"
    assert config.container_name(3, 7) == "inst-s03-g0007"
    assert config.labels() == {"app": "tutorlm-instance"}


def test_describe_never_leaks_tokens():
    config = ControlPlaneConfig(admin_token="top-secret", token_pepper="pepper")
    described = config.describe()
    assert "top-secret" not in repr(described)
    assert "pepper" not in repr(described)
    assert described["admin_token_configured"] is True


def test_with_overrides_rejects_unknown_fields():
    with pytest.raises(TypeError):
        ControlPlaneConfig().with_overrides(nonsense=1)


def test_workspace_retention_and_volume_names():
    config = ControlPlaneConfig.from_env({'CONTROL_PLANE_WORKSPACE_IDLE_TTL_SECONDS': '14400'})
    assert config.workspace_idle_ttl_seconds == 14400
    workspace_id = '00000000-0000-4000-8000-000000000001'
    assert config.workspace_volume_name(workspace_id).endswith('workspace-00000000000040008000000000000001')
    with pytest.raises(ValueError):
        config.workspace_volume_name('../../arbitrary-volume')
