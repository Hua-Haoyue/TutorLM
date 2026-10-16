"""生命周期状态机：就绪门禁、失败隔离、回收与对账。"""

from __future__ import annotations

import pytest

from control_plane.domain.models import (
    LeaseState,
    SlotState,
    WorkloadState,
    can_transition,
)
from control_plane.errors import RuntimeUnavailable
from control_plane.runtime.fake import FakeRuntimeAdapter


def _allocate(components, lease_id="L1"):
    store = components["store"]
    store.allocate(lease_id=lease_id, token_hash="h", ttl_seconds=60)
    return store.require_slot(0)


# ------------------------------------------------------------------ 状态机
@pytest.mark.parametrize(
    "current, target, allowed",
    [
        (WorkloadState.ABSENT, WorkloadState.CREATED, True),
        (WorkloadState.CREATED, WorkloadState.STARTING, True),
        (WorkloadState.STARTING, WorkloadState.READY, True),
        (WorkloadState.READY, WorkloadState.STOPPING, True),
        (WorkloadState.STOPPING, WorkloadState.STOPPED, True),
        (WorkloadState.STOPPED, WorkloadState.REMOVED, True),
        # 跳过启动直接 ready 必须被拒绝
        (WorkloadState.ABSENT, WorkloadState.READY, False),
        (WorkloadState.CREATED, WorkloadState.READY, False),
        # 已删除是终态
        (WorkloadState.REMOVED, WorkloadState.CREATED, False),
    ],
)
def test_transition_table(current, target, allowed):
    assert can_transition(current, target) is allowed


def test_same_state_is_idempotent():
    for state in WorkloadState:
        assert can_transition(state, state) is True


# --------------------------------------------------------------- provision
def test_provision_reaches_ready(components):
    slot = _allocate(components)
    result = components["lifecycle"].provision(slot)
    assert result.ok is True
    assert result.upstream == "http://tutorlm-instance-s00-g0000:8000"
    assert result.slot.state == SlotState.ACTIVE
    assert result.slot.workload_state == WorkloadState.READY
    assert result.ready_payload == {"status": "ok", "mode": "demo"}


def test_provision_quarantines_when_start_fails(components, adapter):
    slot = _allocate(components)
    adapter.fail_start.add("tutorlm-instance-s00-g0000")
    result = components["lifecycle"].provision(slot)
    assert result.ok is False
    assert result.slot.state == SlotState.QUARANTINED
    assert "启动失败" in (result.slot.quarantine_reason or "")
    # 隔离的槽位绝不能再被分配
    assert result.slot.is_allocatable is False


def test_provision_quarantines_when_volume_creation_fails(components, adapter):
    slot = _allocate(components)
    adapter.fail_create.add("tutorlm-instance-s00-g0000")
    result = components["lifecycle"].provision(slot)
    assert result.ok is False
    assert result.slot.state == SlotState.QUARANTINED


def test_provision_never_returns_unready_instance(components, adapter):
    """探针始终不通过时，必须停掉容器并隔离，而不是交给访客。"""

    slot = _allocate(components)
    name = "tutorlm-instance-s00-g0000"
    adapter.never_ready.add(name)
    result = components["lifecycle"].provision(slot)
    assert result.ok is False
    assert result.slot.state == SlotState.QUARANTINED
    assert "就绪" in (result.slot.quarantine_reason or "")
    # 容器已被停掉，不会留在后台占资源
    assert adapter.snapshot()["containers"][name]["running"] is False


def test_provision_waits_for_cold_start(components, adapter):
    """需要多次探测才就绪时仍能成功（模拟真实冷启动）。"""

    adapter.ready_after_probes = 3
    components["config"] = components["config"].with_overrides(
        ready_timeout_seconds=5.0, ready_poll_interval_seconds=0.001
    )
    from control_plane.lifecycle import SlotLifecycle

    components["lifecycle"] = SlotLifecycle(
        components["config"], components["store"], adapter
    )
    slot = _allocate(components)
    result = components["lifecycle"].provision(slot)
    assert result.ok is True
    assert adapter.snapshot()["containers"]["tutorlm-instance-s00-g0000"]["probes"] >= 3


# ------------------------------------------------------------------- drain
def test_begin_drain_is_idempotent(components):
    slot = _allocate(components)
    components["lifecycle"].provision(slot)
    first = components["lifecycle"].begin_drain(0)
    second = components["lifecycle"].begin_drain(0)
    assert first.state == SlotState.DRAINING
    assert second.state == SlotState.DRAINING


# ------------------------------------------------------------------ reclaim
def test_reclaim_removes_data_and_advances_generation(components, adapter):
    slot = _allocate(components)
    result = components["lifecycle"].provision(slot)
    assert result.ok
    assert adapter.snapshot()["volumes"]

    reclaimed = components["lifecycle"].reclaim(0)
    assert reclaimed.state == SlotState.FREE
    assert reclaimed.generation == 1
    assert reclaimed.lease_id is None
    assert reclaimed.workload_state == WorkloadState.ABSENT
    # 关键：数据卷被删除，上一位访客的数据不再存在
    snapshot = adapter.snapshot()
    assert snapshot["volumes"] == []
    assert snapshot["containers"] == {}


def test_reclaim_is_safe_when_container_already_gone(components, adapter):
    slot = _allocate(components)
    components["lifecycle"].provision(slot)
    adapter.remove("tutorlm-instance-s00-g0000")
    reclaimed = components["lifecycle"].reclaim(0)
    assert reclaimed.state == SlotState.FREE
    assert reclaimed.generation == 1


def test_reclaim_failure_quarantines_and_raises(components, adapter):
    slot = _allocate(components)
    components["lifecycle"].provision(slot)
    adapter.fail_stop.add("tutorlm-instance-s00-g0000")
    with pytest.raises(RuntimeUnavailable):
        components["lifecycle"].reclaim(0)
    slot_after = components["store"].require_slot(0)
    # 停止失败绝不能把槽位当干净放回
    assert slot_after.state == SlotState.QUARANTINED


def test_volume_delete_failure_quarantines(components, adapter):
    slot = _allocate(components)
    components["lifecycle"].provision(slot)
    adapter.fail_remove_volume.add("tutorlm-instance-data-s00")
    with pytest.raises(RuntimeUnavailable):
        components["lifecycle"].reclaim(0)
    assert components["store"].require_slot(0).state == SlotState.QUARANTINED


def test_release_quarantine_converges_to_free(components, adapter):
    """隔离后人工确认，必须真的清干净并回到 FREE。"""

    slot = _allocate(components)
    adapter.fail_start.add("tutorlm-instance-s00-g0000")
    result = components["lifecycle"].provision(slot)
    assert result.slot.state == SlotState.QUARANTINED

    adapter.fail_start.clear()
    released = components["lifecycle"].release_quarantine(0, reason="ops checked")
    assert released.state == SlotState.FREE
    assert released.quarantine_reason is None
    assert released.generation == 1
    assert adapter.snapshot()["volumes"] == []


def test_release_quarantine_requires_quarantined_state(components):
    _allocate(components)
    with pytest.raises(Exception):
        components["lifecycle"].release_quarantine(0, reason="nope")


# ---------------------------------------------------------------- reconcile
def test_reconcile_quarantines_slot_without_lease(components):
    """槽位被标记占用但没有有效租约 -> 状态不一致，必须隔离。"""

    components["store"].update_slot(0, state=SlotState.ACTIVE, container_name="ghost")
    report = components["lifecycle"].reconcile()
    assert 0 in report["quarantined"]
    assert components["store"].require_slot(0).state == SlotState.QUARANTINED


def test_reconcile_quarantines_slot_whose_container_vanished(components, adapter):
    slot = _allocate(components)
    result = components["lifecycle"].provision(slot)
    assert result.ok
    # 模拟有人手工删掉了容器，但租约还活着
    adapter.remove("tutorlm-instance-s00-g0000")
    report = components["lifecycle"].reconcile()
    assert 0 in report["quarantined"]
    assert components["store"].require_slot(0).state == SlotState.QUARANTINED


def test_reconcile_leaves_healthy_slot_alone(components):
    slot = _allocate(components)
    assert components["lifecycle"].provision(slot).ok
    report = components["lifecycle"].reconcile()
    assert report["quarantined"] == []
    assert components["store"].require_slot(0).state == SlotState.ACTIVE


def test_reconcile_ignores_terminal_lease(components, adapter):
    """租约已终结但槽位没释放 -> 隔离，等人工确认后再回收。"""

    slot = _allocate(components)
    assert components["lifecycle"].provision(slot).ok
    components["store"].update_lease("L1", state=LeaseState.RECLAIMED)
    report = components["lifecycle"].reconcile()
    assert 0 in report["quarantined"]
