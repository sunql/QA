"""KPI / 指标状态流转矩阵（P5 治理收口）。

纯函数，不查库。service 在写库 / 入队 / 审计之前调用：非法流转抛
``ConflictError``（HTTP 409，detail=``INVALID_STATUS_TRANSITION``），拒绝即零副作用。
矩阵之外的未知状态值一律失败关闭（未知 ≠ 放行）。

矩阵：
- KPI：DRAFT → PUBLISHED → DEPRECATED → DRAFT
- 指标：DRAFT ↔ PUBLISHED，但 PUBLISHED → DRAFT 仅当没有已发布 KPI 挂靠
  （否则已发布 KPI 会指向草稿口径）。

同态（src == dst）是 no-op；未知状态即便同态也拒绝。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import NoReturn

from app.domain.exceptions import ConflictError
from app.services.messages_zh import (
    MSG_KPI_STATUS_TRANSITION_INVALID,
    MSG_METRIC_STATUS_TRANSITION_INVALID,
)

DETAIL_INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"

_KPI_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "DRAFT": frozenset({"PUBLISHED"}),
        "PUBLISHED": frozenset({"DEPRECATED"}),
        "DEPRECATED": frozenset({"DRAFT"}),
    }
)

_METRIC_TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "DRAFT": frozenset({"PUBLISHED"}),
        "PUBLISHED": frozenset({"DRAFT"}),
    }
)

_METRIC_PUBLISHED = "PUBLISHED"


def _statusValue(value: object) -> str:
    """把 DB 值（str）或枚举成员归一为状态字符串。

    枚举成员取 ``.value``；``str`` 原样返回；其它非 None 类型 ``str(value)``
    （矩阵查不到 ⇒ 失败关闭）；仅 ``None`` 映射为 ``""``。
    """
    raw = getattr(value, "value", None)
    if isinstance(raw, str):
        return raw
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _raiseInvalid(
    messageTemplate: str,
    src: str,
    dst: str,
    allowed: frozenset[str],
) -> NoReturn:
    allowedList = sorted(allowed)
    raise ConflictError(
        messageTemplate.format(
            src=src or "<unknown>",
            dst=dst or "<unknown>",
            allowed=", ".join(allowedList) if allowedList else "无",
        ),
        detail=DETAIL_INVALID_STATUS_TRANSITION,
        details={"from": src, "to": dst, "allowed": allowedList},
    )


def assertKpiTransition(fromStatus: str, toStatus: str) -> None:
    """校验 KPI 状态流转；非法（含未知状态）⇒ ConflictError(409)。"""
    src = _statusValue(fromStatus)
    dst = _statusValue(toStatus)
    if (
        src in _KPI_TRANSITIONS
        and dst in _KPI_TRANSITIONS
        and (src == dst or dst in _KPI_TRANSITIONS[src])
    ):
        return
    _raiseInvalid(
        MSG_KPI_STATUS_TRANSITION_INVALID,
        src,
        dst,
        _KPI_TRANSITIONS.get(src, frozenset()),
    )


def assertMetricTransition(
    fromStatus: str,
    toStatus: str,
    *,
    publishedKpiCount: int,
) -> None:
    """校验指标状态流转；PUBLISHED → DRAFT 仅当无已发布 KPI 挂靠。"""
    src = _statusValue(fromStatus)
    dst = _statusValue(toStatus)
    allowed = _METRIC_TRANSITIONS.get(src, frozenset())
    if src == _METRIC_PUBLISHED and publishedKpiCount > 0:
        # 有已发布 KPI 挂靠 ⇒ 本状态不允许退回草稿（同态 no-op 不受影响）
        allowed = frozenset()
    if (
        src in _METRIC_TRANSITIONS
        and dst in _METRIC_TRANSITIONS
        and (src == dst or dst in allowed)
    ):
        return
    _raiseInvalid(MSG_METRIC_STATUS_TRANSITION_INVALID, src, dst, allowed)
