"""分配器：令牌、解析、释放、收割。"""

from __future__ import annotations

import pytest

from control_plane.domain.models import LeaseState, SlotState
from control_plane.errors import (
    CapacityError,
    InvalidToken,
    LeaseNotFound,
    RuntimeUnavailable,
)
from control_plane.runtime.fake import FakeRuntimeAdapter


class CountingProbe:
    """可控的活动探针，用于测试空闲回收。"""

    def __init__(self, busy: bool = False) -> None:
        self.busy = busy
        self.calls: list[str] = []

    def has_active_runs(self, upstream: str) -> bool:
        self.calls.append(upstream)
        return self.busy


@pytest.fixture()
def clock():
    class Clock:
        def __init__(self) -> None:
            self.now = 1_000_000.0

        def __call__(self) -> float:
            return self.now

    return Clock()


def test_allocate_returns_working_token(components):
    allocator = components["allocator"]
    result = allocator.allocate(label="a")
    assert result.token
    assert result.upstream.startswith("http://")
    # 明文令牌绝不出现在持久层
    assert result.token not in repr(components["store"].require_lease(result.lease.lease_id))
    lease, upstream = allocator.resolve_target(result.token)
    assert lease.lease_id == result.lease.lease_id
    assert upstream == result.upstream


def test_token_hash_is_stored_not_plaintext(components):
    allocator = components["allocator"]
    result = allocator.allocate()
    stored = components["store"].require_lease(result.lease.lease_id)
    assert stored.token_hash
    assert stored.token_hash != result.token
    assert len(stored.token_hash) == 64  # sha256 hex


def test_invalid_and_missing_tokens_are_rejected(components):
    allocator = components["allocator"]
    allocator.allocate()
    with pytest.raises(InvalidToken):
        allocator.authenticate(None)
    with pytest.raises(InvalidToken):
        allocator.authenticate("not-a-real-token")


def test_allocate_fails_cleanly_when_capacity_full(components):
    allocator = components["allocator"]
    allocator.allocate()
    allocator.allocate()
    with pytest.raises(CapacityError):
        allocator.allocate()


def test_allocate_fails_when_instance_cannot_start(components, adapter):
    allocator = components["allocator"]
    adapter.fail_start.add("tutorlm-instance-s00-g0000")
    with pytest.raises(RuntimeUnavailable):
        allocator.allocate()
    # 失败不能留下可用的租约，也不能让槽位看起来空闲
    assert components["store"].list_live_leases() == []
    assert components["store"].require_slot(0).state == SlotState.QUARANTINED


def test_release_reclaims_and_allows_reallocation(components):
    allocator = components["allocator"]
    first = allocator.allocate()
    final = allocator.release(first.lease.lease_id)
    assert final.state == LeaseState.RECLAIMED
    assert components["store"].require_slot(first.lease.slot_id).state == SlotState.FREE
    second = allocator.allocate()
    assert second.lease.generation == first.lease.generation + 1
    # 旧令牌必须彻底失效
    with pytest.raises(InvalidToken):
        allocator.authenticate(first.token)


def test_release_is_idempotent(components):
    allocator = components["allocator"]
    result = allocator.allocate()
    allocator.release(result.lease.lease_id)
    again = allocator.release(result.lease.lease_id)
    assert again.state == LeaseState.RECLAIMED


def test_resolve_target_rejects_pending_lease(components):
    """启动中/未就绪的租约不得被转发流量。"""

    store = components["store"]
    store.allocate(lease_id="L1", token_hash="h1", ttl_seconds=60)
    with pytest.raises(InvalidToken):
        components["allocator"].resolve_target("whatever")


def test_token_pepper_is_persisted_when_not_configured(tmp_path):
    from control_plane.api.app import build_components
    from control_plane.config import ControlPlaneConfig

    config = ControlPlaneConfig.for_tests(tmp_path, token_pepper="")
    built = build_components(config, adapter=FakeRuntimeAdapter())
    try:
        resource = built["allocator"].allocate()
        pepper = built["store"].get_meta("token_pepper")
        assert pepper
        # 重新加载同一个库仍能验证旧令牌（盐已持久化）
        built2 = build_components(config, adapter=FakeRuntimeAdapter())
        try:
            lease = built2["allocator"].authenticate(resource.token)
            assert lease.lease_id == resource.lease.lease_id
        finally:
            built2["store"].close()
    finally:
        built["store"].close()


# --------------------------------------------------------------------- GC
def test_gc_reclaims_expired_absolute_ttl(tmp_path, clock):
    from control_plane.allocator import Allocator
    from control_plane.api.app import build_components
    from control_plane.config import ControlPlaneConfig
    from control_plane.lifecycle import SlotLifecycle

    config = ControlPlaneConfig.for_tests(tmp_path, lease_ttl_seconds=10)
    adapter = FakeRuntimeAdapter()
    # 关键：store 与 allocator 必须共用同一个时钟，否则 TTL 判定会
    # 混用"假时钟分配时刻"与"真实时间当前时刻"。
    built = build_components(config, adapter=adapter, clock=clock)
    allocator = built["allocator"]
    try:
        result = allocator.allocate()
        assert built["store"].require_slot(0).state == SlotState.ACTIVE
        clock.now += 11  # 越过绝对 TTL
        report = allocator.gc_once()
        assert result.lease.lease_id in report["reclaimed"]
        assert built["store"].require_slot(0).state == SlotState.FREE
    finally:
        built["store"].close()


def test_gc_idle_respects_active_runs(tmp_path, clock):
    """有活动任务时，空闲回收必须放弃本次回收（不能打断长生成）。"""

    from control_plane.allocator import Allocator
    from control_plane.api.app import build_components
    from control_plane.config import ControlPlaneConfig

    config = ControlPlaneConfig.for_tests(
        tmp_path, lease_ttl_seconds=10_000, lease_idle_ttl_seconds=60, reclaim_on_idle=True
    )
    adapter = FakeRuntimeAdapter()
    probe = CountingProbe(busy=True)
    built = build_components(config, adapter=adapter, activity_probe=probe, clock=clock)
    allocator = built["allocator"]
    try:
        result = allocator.allocate()
        clock.now += 120  # 超过空闲阈值
        report = allocator.gc_once()
        assert result.lease.lease_id in report["skipped_busy"]
        assert built["store"].require_slot(0).state == SlotState.ACTIVE
        assert probe.calls  # 确实问过任务状态
    finally:
        built["store"].close()


def test_gc_idle_reclaims_when_idle(tmp_path, clock):
    from control_plane.allocator import Allocator
    from control_plane.api.app import build_components
    from control_plane.config import ControlPlaneConfig

    config = ControlPlaneConfig.for_tests(
        tmp_path, lease_ttl_seconds=10_000, lease_idle_ttl_seconds=60, reclaim_on_idle=True
    )
    adapter = FakeRuntimeAdapter()
    built = build_components(
        config, adapter=adapter, activity_probe=CountingProbe(busy=False), clock=clock
    )
    allocator = built["allocator"]
    try:
        allocator.allocate()
        clock.now += 120
        report = allocator.gc_once()
        assert len(report["reclaimed"]) == 1
        assert built["store"].require_slot(0).state == SlotState.FREE
    finally:
        built["store"].close()


def test_gc_treats_probe_failure_as_busy(tmp_path, clock):
    """探针抛异常时必须按"忙"处理，宁可漏回收也不能误杀。"""

    from control_plane.allocator import Allocator
    from control_plane.api.app import build_components
    from control_plane.config import ControlPlaneConfig

    class BrokenProbe:
        def has_active_runs(self, upstream: str) -> bool:
            raise RuntimeError("探针炸了")

    config = ControlPlaneConfig.for_tests(
        tmp_path, lease_ttl_seconds=10_000, lease_idle_ttl_seconds=60, reclaim_on_idle=True
    )
    built = build_components(
        config, adapter=FakeRuntimeAdapter(), activity_probe=BrokenProbe(), clock=clock
    )
    allocator = built["allocator"]
    try:
        result = allocator.allocate()
        clock.now += 120
        report = allocator.gc_once()
        assert result.lease.lease_id in report["skipped_busy"]
    finally:
        built["store"].close()


def test_default_probe_is_conservative(tmp_path, clock):
    """默认探针认为一直忙，因此不会自动空闲回收。"""

    from control_plane.allocator import AlwaysBusyProbe, Allocator
    from control_plane.api.app import build_components
    from control_plane.config import ControlPlaneConfig

    config = ControlPlaneConfig.for_tests(
        tmp_path, lease_ttl_seconds=10_000, lease_idle_ttl_seconds=60, reclaim_on_idle=True
    )
    built = build_components(config, adapter=FakeRuntimeAdapter(), clock=clock)
    allocator = built["allocator"]
    try:
        result = allocator.allocate()
        clock.now += 120
        report = allocator.gc_once()
        assert result.lease.lease_id in report["skipped_busy"]
    finally:
        built["store"].close()
