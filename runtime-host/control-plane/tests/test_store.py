"""持久化层：原子分配、代次、租约生命周期。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from control_plane.domain.models import LeaseState, SlotState, WorkloadState
from control_plane.errors import CapacityError, LeaseNotFound, SlotBusy
from control_plane.store import ControlPlaneStore


@pytest.fixture()
def store(tmp_path: Path):
    s = ControlPlaneStore(tmp_path / "cp.sqlite3")
    s.ensure_slots(2)
    try:
        yield s
    finally:
        s.close()


def test_ensure_slots_is_idempotent(store: ControlPlaneStore):
    assert store.ensure_slots(2) == 0
    assert [slot.slot_id for slot in store.list_slots()] == [0, 1]


def test_ensure_slots_extends_pool(store: ControlPlaneStore):
    assert store.ensure_slots(4) == 2
    assert [slot.slot_id for slot in store.list_slots()] == [0, 1, 2, 3]


def test_allocate_marks_slot_and_creates_lease(store: ControlPlaneStore):
    lease = store.allocate(lease_id="L1", token_hash="h1", ttl_seconds=60, label="x")
    assert lease.slot_id == 0
    assert lease.state == LeaseState.PENDING
    assert lease.label == "x"
    slot = store.require_slot(0)
    assert slot.state == SlotState.ALLOCATING
    assert slot.lease_id == "L1"


def test_allocate_uses_distinct_slots(store: ControlPlaneStore):
    a = store.allocate(lease_id="L1", token_hash="h1", ttl_seconds=60)
    b = store.allocate(lease_id="L2", token_hash="h2", ttl_seconds=60)
    assert {a.slot_id, b.slot_id} == {0, 1}


def test_allocate_raises_when_full(store: ControlPlaneStore):
    store.allocate(lease_id="L1", token_hash="h1", ttl_seconds=60)
    store.allocate(lease_id="L2", token_hash="h2", ttl_seconds=60)
    with pytest.raises(CapacityError):
        store.allocate(lease_id="L3", token_hash="h3", ttl_seconds=60)
    # 失败的分配不能留下半成品租约或占位
    assert store.get_lease("L3") is None
    assert store.get_lease_by_slot(0) is not None


def test_quarantined_slot_is_not_allocatable(store: ControlPlaneStore):
    store.update_slot(1, state=SlotState.QUARANTINED, quarantine_reason="boom")
    lease = store.allocate(lease_id="L1", token_hash="h1", ttl_seconds=60)
    assert lease.slot_id == 0
    with pytest.raises(CapacityError):
        store.allocate(lease_id="L2", token_hash="h2", ttl_seconds=60)


def test_concurrent_allocation_never_double_assigns(store: ControlPlaneStore):
    """并发分配的核心不变量：一个槽位只能属于一个租约。"""

    results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        try:
            lease = store.allocate(
                lease_id=f"L{index}", token_hash=f"h{index}", ttl_seconds=60
            )
            with lock:
                results.append(f"L{index}->{lease.slot_id}")
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 2 个槽位 -> 恰好 2 个成功，其余都是容量不足
    assert len(results) == 2
    assert all(isinstance(e, CapacityError) for e in errors)
    assert len(errors) == 6
    slots = {entry.split("->")[1] for entry in results}
    assert slots == {"0", "1"}


def test_reclaim_generation_advances_and_frees_slot(store: ControlPlaneStore):
    lease = store.allocate(lease_id="L1", token_hash="h1", ttl_seconds=60)
    assert store.require_slot(lease.slot_id).generation == 0
    updated = store.update_slot(
        lease.slot_id,
        state=SlotState.FREE,
        generation=1,
        workload_state=WorkloadState.ABSENT,
        clear_lease=True,
    )
    assert updated.generation == 1
    assert updated.lease_id is None
    # 回收后可以再分配，且拿到新代次
    again = store.allocate(lease_id="L2", token_hash="h2", ttl_seconds=60)
    assert again.generation == 1


def test_update_lease_and_touch(store: ControlPlaneStore):
    store.allocate(lease_id="L1", token_hash="h1", ttl_seconds=60)
    store.update_lease("L1", state=LeaseState.ACTIVE, upstream="http://c:8000")
    store.touch_lease("L1", at=123.0)
    lease = store.require_lease("L1")
    assert lease.state == LeaseState.ACTIVE
    assert lease.upstream == "http://c:8000"
    assert lease.last_seen_at == 123.0


def test_update_missing_lease_raises(store: ControlPlaneStore):
    with pytest.raises(LeaseNotFound):
        store.update_lease("nope", state=LeaseState.ACTIVE)


def test_live_leases_excludes_terminal(store: ControlPlaneStore):
    store.allocate(lease_id="L1", token_hash="h1", ttl_seconds=60)
    store.allocate(lease_id="L2", token_hash="h2", ttl_seconds=60)
    store.update_lease("L2", state=LeaseState.RECLAIMED)
    live = {lease.lease_id for lease in store.list_live_leases()}
    assert live == {"L1"}


def test_events_are_recorded(store: ControlPlaneStore):
    store.allocate(lease_id="L1", token_hash="h1", ttl_seconds=60)
    events = store.recent_events(limit=10)
    assert any(e["kind"] == "lease.allocated" for e in events)


def test_meta_roundtrip(store: ControlPlaneStore):
    assert store.get_meta("token_pepper") is None
    store.set_meta("token_pepper", "abc")
    assert store.get_meta("token_pepper") == "abc"


def test_store_persists_across_reopen(tmp_path: Path):
    path = tmp_path / "cp.sqlite3"
    first = ControlPlaneStore(path)
    first.ensure_slots(2)
    first.allocate(lease_id="L1", token_hash="h1", ttl_seconds=60)
    first.close()

    second = ControlPlaneStore(path)
    try:
        # 槽位占用与租约都必须保留，重启不会把实例当成空闲
        assert second.require_slot(0).state == SlotState.ALLOCATING
        assert second.require_lease("L1").slot_id == 0
        assert second.ensure_slots(2) == 0
    finally:
        second.close()
