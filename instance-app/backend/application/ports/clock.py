"""时间与 ID 端口。

领域层不读系统时间、不生成随机 id，因此并发与幂等行为可以在测试里确定复现。
"""

from __future__ import annotations

import time
import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """返回 Unix 秒（UTC），用于 ``created_at``/``updated_at``/``expires_at``。"""

    def now(self) -> float:
        ...


@runtime_checkable
class IdGenerator(Protocol):
    """生成不透明 id（UUID 字符串）。"""

    def new_id(self) -> str:
        ...


class SystemClock:
    """真实时钟。"""

    __slots__ = ()

    def now(self) -> float:
        return time.time()


class UuidGenerator:
    """真实 id 生成器（UUID4 规范字符串）。"""

    __slots__ = ()

    def new_id(self) -> str:
        return str(uuid.uuid4())
