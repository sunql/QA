"""M5-1 询价草稿状态机（计划 5.1：复用 workflow_state_machine 的矩阵模式）。

七状态主链（设计稿 §6.2）：DRAFT → VALIDATING → PENDING_APPROVAL →
READY_TO_SEND → SENDING → SENT → CLOSED；分支：NEEDS_CORRECTION（校验打回）、
REJECTED（审批驳回）、CANCELLED（取消）。

**D-4（DRAFT-only）**：SENDING/SENT/CLOSED 在 M5 **不可达**——矩阵形态保留
（M6 接外部系统时启用），负向用例钉死（READY_TO_SEND 之后无出口 ⇒ 尝试推进
⇒ 拒绝）。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

RFQ_DRAFT_STATUS = "rfq"

_RFQ_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType({
    # VALIDATING = 「校验已通过、待提交」的驻留态（M5-2 submit 从这里进审批）；
    # VALIDATING→NEEDS_CORRECTION：校验不通过打回。
    "DRAFT": frozenset({"VALIDATING", "CANCELLED"}),
    "VALIDATING": frozenset({"NEEDS_CORRECTION", "PENDING_APPROVAL"}),
    "NEEDS_CORRECTION": frozenset({"VALIDATING", "CANCELLED"}),
    "PENDING_APPROVAL": frozenset({"READY_TO_SEND", "REJECTED"}),
    # M6-1：接入 ERP/SRM 命令端口后允许进入 SENDING（**前置 = 已审批到 READY_TO_SEND**，
    # 且发送必须走端口 + 幂等键 + 二次确认）。M5 阶段（D-4）无发送端点 ⇒ 不可达。
    "READY_TO_SEND": frozenset({"SENDING", "CANCELLED"}),
    "SENDING": frozenset({"SENT", "FAILED"}),
    "SENT": frozenset({"CLOSED"}),  # M6 启用
    "REJECTED": frozenset(),  # 终态
    "CANCELLED": frozenset(),  # 终态
    "CLOSED": frozenset(),  # 终态
})

RFQ_TRANSITIONS: Mapping[str, Mapping[str, frozenset[str]]] = MappingProxyType({
    RFQ_DRAFT_STATUS: _RFQ_TRANSITIONS,
})

_LIVE_RFQ_STATUSES = frozenset({
    "DRAFT", "VALIDATING", "NEEDS_CORRECTION", "PENDING_APPROVAL", "READY_TO_SEND",
    "SENDING",
})


def rfqStatuses() -> frozenset[str]:
    """询价草稿全部合法状态。"""
    return frozenset(_RFQ_TRANSITIONS.keys())


def isLiveRfq(status: str) -> bool:
    """草稿是否仍在活跃链上（终态 = REJECTED/CANCELLED/CLOSED/SENT）。"""
    return status in _LIVE_RFQ_STATUSES


def allowedRfqTransitions(status: str) -> frozenset[str]:
    """给定状态的合法后继（未知状态 ⇒ 空集，fail-closed）。"""
    return _RFQ_TRANSITIONS.get(status, frozenset())


def assertRfqTransition(current: str, target: str) -> None:
    """状态迁移合法性断言（非法 ⇒ ValueError，调用方转 409/422）。"""
    allowed = allowedRfqTransitions(current)
    if target not in allowed:
        raise ValueError(
            f"非法状态迁移：{current} → {target}（允许：{sorted(allowed)}）"
        )
