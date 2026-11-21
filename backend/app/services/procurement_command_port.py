"""M6-1 ERP/SRM 命令端口（Protocol）与模拟器实现。

**为什么需要端口**：发送询价是**外部副作用**（写 ERP/SRM）。D-4 阶段
（M5）刻意只做到 DRAFT——不发送。本模块把「发送」这条路径抽象成可替换的端口：

- 真实适配器（待外部账号/网络到位）⇒ 换实现即可，不动编排层；
- 模拟器（默认）⇒ 本地/CI 可跑通全链，不产生真实副作用。

**PROHIBITED_AUTO 在代码层不可达**（设计稿 §6.6）：自动定标 / 合同签署 / 付款 /
绕过 ERP 审批——本协议**不提供**这些方法 ⇒ 任何实现都无法凭空造出该能力。

**幂等**：端口方法一律带 idempotencyKey；同键重复调用必须返回**同一**外部引用
（否则「超时重试 ⇒ 重复发送」是采购域最贵的 bug）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# 发送状态（外部系统的最小状态集；真实适配器按 ERP 回传映射到此）。
RFQ_SEND_STATUSES = ("PENDING", "SENT", "FAILED", "REJECTED")


@dataclass(frozen=True)
class RfqSendResult:
    """一次发送尝试的结果（**外部引用 + 状态**，不携带业务数据）。"""

    externalRef: str
    status: str
    detail: str | None = None


@runtime_checkable
class ProcurementCommandPort(Protocol):
    """采购外部命令端口（**只读接口定义**，无实现）。

    ⚠️ 这里**故意没有** autoAward / signContract / payInvoice 等方法：
    PROHIBITED_AUTO 动作在协议层即不可达（设计稿 §6.6）。
    """

    async def sendRfq(
        self,
        draft: dict[str, Any],
        *,
        idempotencyKey: str,
    ) -> RfqSendResult:
        """发送询价草稿到 ERP/SRM（同键幂等）。"""
        ...

    async def querySendStatus(self, externalRef: str) -> RfqSendResult:
        """查询发送状态（状态回写用）。"""
        ...


class SimulatedErpSrmAdapter:
    """模拟器（默认注入）：**不产生任何真实副作用**。

    - 外部引用 = ``SIM-<sha256(draft+key)[:16]>``（确定性、可追溯）；
    - 同 idempotencyKey ⇒ 同 externalRef（幂等）；
    - 状态恒 SENT（模拟即时成功），失败路径由测试用子类覆盖。
    """

    def __init__(self, *, status: str = "SENT") -> None:
        self._status = status
        self.sentKeys: list[str] = []

    async def sendRfq(
        self,
        draft: dict[str, Any],
        *,
        idempotencyKey: str,
    ) -> RfqSendResult:
        self.sentKeys.append(idempotencyKey)
        digest = hashlib.sha256(
            f"{idempotencyKey}|{sorted(draft.items())}".encode()
        ).hexdigest()[:16]
        return RfqSendResult(
            externalRef=f"SIM-{digest}",
            status=self._status,
            detail="模拟发送（无真实副作用）",
        )

    async def querySendStatus(self, externalRef: str) -> RfqSendResult:
        return RfqSendResult(
            externalRef=externalRef, status=self._status, detail="模拟状态查询"
        )


def defaultCommandPort() -> ProcurementCommandPort:
    """默认端口 = 模拟器（真实适配器接入前，发送只在本地生效）。"""
    return SimulatedErpSrmAdapter()
