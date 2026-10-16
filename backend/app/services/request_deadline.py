"""整轮 deadline SSOT（pure infrastructure，本任务不接进 pipeline）。

P2-1：为后续 wiring 任务提供 RequestDeadline 不可变 dataclass 与
remainingSeconds / isExpired 计算；当前任务**不**改 chat_service
入口（避免在 P2-1 中触碰 1985 行的巨型文件 + 不影响其他任务 baseline）。

使用场景（后续 PR 接入）：
    deadline = RequestDeadline(ttl_seconds=120.0)
    if deadline.isExpired():
        raise LlmClientError("整轮 deadline 已耗尽")
    # ... sub-step ...
    if deadline.remainingSeconds() < 5.0:
        # 留给收尾的最小预算
        ...
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RequestDeadline:
    """整轮 deadline（不可变，per global immutability rule）。

    字段：
    - ttl_seconds：总预算（秒）
    - started_at：构造时 monotonic 时间戳

    设计要点：
    - 不可变：所有方法返回新值或 bool，不修改 self
    - 用 monotonic clock：不受系统时间调整影响
    """
    ttl_seconds: float
    started_at: float = field(default_factory=time.monotonic)

    def isExpired(self) -> bool:
        """是否已超过 ttl_seconds。"""
        return time.monotonic() - self.started_at >= self.ttl_seconds

    def remainingSeconds(self) -> float:
        """剩余秒数（已过期返回 0.0）。"""
        remaining = self.ttl_seconds - (time.monotonic() - self.started_at)
        return max(0.0, remaining)
