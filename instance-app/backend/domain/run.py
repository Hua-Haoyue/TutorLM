"""run 状态机与幂等规则（纯函数）。

约束（设计 §5）：

- 迁移：``queued -> running -> completed | failed | cancelled | interrupted``；
  ``cancelled/interrupted/failed/completed`` 为终态。
- **取消后不 commit**：取消即置 ``cancelled`` 并丢弃在途模型结果；已返回的 provider
  结果也不落库。
- **硬停止/重启不重放付费调用**：重启时把进行中的 run 置 ``interrupted``，不自动重试。
- 幂等键作用域是 **workspace 身份**（同 workspace 同 key 幂等）；同 key 不同输入 → 409。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from backend.domain.enums import RunState, RunWorkload, is_run_terminal
from backend.domain.errors import Conflict, RunNotRunning


def canonical_input_fingerprint(payload: Mapping[str, Any] | None) -> str:
    """输入指纹：排序键、紧凑分隔，保证同输入得同指纹。

    只用它判"同 key 是否同输入"，不做签名（幂等键的作用域是 workspace 身份，
    不是安全边界）。
    """

    body = json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def resolve_idempotent_create(
    *,
    existing_state: RunState | None,
    existing_fingerprint: str | None,
    requested_fingerprint: str,
    existing_run_id: str | None = None,
) -> str | None:
    """同 key 重复提交时的判定。

    返回 ``None`` 表示"应当新建 run"；返回 run id 表示"命中幂等，复用该 run"。
    同 key 不同输入一律 ``409 conflict``。
    """

    if existing_run_id is None:
        return None
    if existing_fingerprint != requested_fingerprint:
        raise Conflict(
            "同一 Idempotency-Key 对应的输入不同",
            details={"run_id": existing_run_id},
        )
    return existing_run_id


def assert_transition(current: RunState, target: RunState) -> None:
    """非法迁移抛 ``RunNotRunning``（终态或跳步）。"""

    from backend.domain.enums import can_transition_run

    if not can_transition_run(current, target):
        raise RunNotRunning(
            f"run 状态不允许迁移：{current} -> {target}",
            details={"current": str(current), "target": str(target)},
        )


def may_commit_result(state: RunState) -> bool:
    """只有仍然 ``running`` 的 run 才允许写入 provider 结果。

    取消/硬停止后到达的结果必须丢弃（设计 §5），因此这里必须是
    "state == running"，而不是"未到终态"。
    """

    return state is RunState.RUNNING


def interrupt_in_flight(state: RunState) -> RunState:
    """重启/硬停止时的收尾：进行中的 run 置 ``interrupted``。

    终态保持不变（幂等）；``queued`` 也置 ``interrupted``——重启不重放付费调用，
    宁可让用户显式重跑，也不自动重试。
    """

    if is_run_terminal(state):
        return state
    return RunState.INTERRUPTED


def initial_state_for(workload: RunWorkload) -> RunState:
    """所有 workload 都从 ``queued`` 开始；executor 由 host 按 workload 分离。"""

    return RunState.QUEUED
