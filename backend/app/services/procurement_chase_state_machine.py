"""催货单状态机（feat/delivery-chase-closed-loop；与询价单同构）。

状态链与 `procurement_rfq_state_machine` 完全同构（复用矩阵模式）：
DRAFT → VALIDATING → PENDING_APPROVAL → READY_TO_SEND → SENDING → SENT → CLOSED；
分支：NEEDS_CORRECTION / REJECTED / CANCELLED。

**为什么复制矩阵而不 import RFQ 的**：两个状态机的**演进节奏不同** ——
询价后续要接「报价回流」、催货后续要接「答复提醒/自动升级」，混用一个矩阵后
每次演进都要评估另一半的影响（本仓已有跨 PR 语义冲突的先例）。复制是
**刻意的解耦**，不是偷懒。

**本期口径**（用户 2026-11-27 拍板）：
- 四眼审批链路保留（submit-approval → approve → send）；
- SEND 成功（`SENT`）即终态 —— 供应商答复回流不做（二期）；
  `SENT → CLOSED` 的迁移保留（将来手动归档用）。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

CHASE_DRAFT_STATUS = "chase"

_CHASE_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType({
    "DRAFT": frozenset({"VALIDATING", "CANCELLED"}),
    "VALIDATING": frozenset({"NEEDS_CORRECTION", "PENDING_APPROVAL"}),
    "NEEDS_CORRECTION": frozenset({"VALIDATING", "CANCELLED"}),
    "PENDING_APPROVAL": frozenset({"READY_TO_SEND", "REJECTED"}),
    "READY_TO_SEND": frozenset({"SENDING", "CANCELLED"}),
    "SENDING": frozenset({"SENT", "FAILED"}),
    "SENT": frozenset({"CLOSED"}),
    "REJECTED": frozenset(),  # 终态
    "CANCELLED": frozenset(),  # 终态
    "CLOSED": frozenset(),  # 终态
})

CHASE_TRANSITIONS: Mapping[str, Mapping[str, frozenset[str]]] = MappingProxyType({
    CHASE_DRAFT_STATUS: _CHASE_TRANSITIONS,
})

_LIVE_CHASE_STATUSES = frozenset({
    "DRAFT", "VALIDATING", "NEEDS_CORRECTION", "PENDING_APPROVAL", "READY_TO_SEND",
    "SENDING",
})


def chaseStatuses() -> frozenset[str]:
    """催货单全部合法状态。"""
    return frozenset(_CHASE_TRANSITIONS.keys())


def isLiveChase(status: str) -> bool:
    """草稿是否仍在活跃链上（终态 = REJECTED/CANCELLED/CLOSED/SENT）。"""
    return status in _LIVE_CHASE_STATUSES


def allowedChaseTransitions(status: str) -> frozenset[str]:
    """给定状态的合法后继（未知状态 ⇒ 空集，fail-closed）。"""
    return _CHASE_TRANSITIONS.get(status, frozenset())


def assertChaseTransition(current: str, target: str) -> None:
    """状态迁移合法性断言（非法 ⇒ ValueError；service 层包装为 ConflictError ⇒ 409）。"""
    allowed = allowedChaseTransitions(current)
    if target not in allowed:
        raise ValueError(f"非法状态迁移：{current} → {target}（允许：{sorted(allowed)}）")
