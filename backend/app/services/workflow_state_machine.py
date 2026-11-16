"""P5 工作流状态流转矩阵（纯函数）。

照 `app/services/kpi_status_machine.py` 的形态（D-4）：转移矩阵是
`MappingProxyType`（不可变）+ ``assert*Transition()`` 抛
``ConflictError(409, "INVALID_STATUS_TRANSITION")``；**未知状态失败关闭**
（未知 ≠ 放行）。纯函数 ⇒ 不吃 DB，可穷举全部路径。

四类实体各有一套矩阵：

- **instance**（工作流实例）：PENDING → RUNNING → （WAITING_APPROVAL → APPROVED）
  → COMPLETED；失败可重试（FAILED → RUNNING）或补偿（→ COMPENSATED）。
- **step**（步骤）：PENDING → RUNNING → SUCCEEDED / FAILED；失败可重试或补偿。
- **command**（业务命令）：PENDING → EXECUTING → SUCCEEDED / FAILED；
  失败可重试（FAILED → EXECUTING）或补偿（→ COMPENSATED）。
- **approval**（审批任务）：PENDING → APPROVED / REJECTED / CANCELLED / TIMEOUT。

终态（无出边）：instance 的 REJECTED / CANCELLED / COMPENSATED；step 的
SKIPPED / COMPENSATED；command 的 COMPENSATED；approval 的四个终态。

⚠️ 矩阵里的状态集合必须与**库层 CHECK 表达式**的字面量集合一致
（`app/tests/unit/test_workflow_state_machine.py` 的最后一条用例把两侧钉在一起）
⇒ 否则「状态机放行、库层拒绝」或反之。

同态是 no-op（幂等重放：重复推进同一状态不算错）。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import NoReturn

from app.domain.exceptions import ConflictError
from app.services.messages_zh import MSG_WORKFLOW_STATUS_TRANSITION_INVALID

DETAIL_INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"

_INSTANCE: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "PENDING": frozenset({"RUNNING", "CANCELLED", "FAILED"}),
        "RUNNING": frozenset({"WAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED"}),
        "WAITING_APPROVAL": frozenset({"APPROVED", "REJECTED", "CANCELLED", "FAILED", "RUNNING"}),
        "APPROVED": frozenset({"RUNNING", "COMPLETED", "FAILED", "CANCELLED"}),
        "REJECTED": frozenset(),
        "COMPLETED": frozenset({"COMPENSATED"}),
        "FAILED": frozenset({"RUNNING", "COMPENSATED"}),
        "CANCELLED": frozenset(),
        "COMPENSATED": frozenset(),
    }
)

_STEP: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "PENDING": frozenset({"RUNNING", "SKIPPED"}),
        "RUNNING": frozenset({"SUCCEEDED", "FAILED"}),
        "SUCCEEDED": frozenset({"COMPENSATED"}),
        "FAILED": frozenset({"RUNNING", "COMPENSATED"}),
        "SKIPPED": frozenset(),
        "COMPENSATED": frozenset(),
    }
)

_COMMAND: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "PENDING": frozenset({"EXECUTING", "FAILED"}),
        "EXECUTING": frozenset({"SUCCEEDED", "FAILED"}),
        "SUCCEEDED": frozenset({"COMPENSATED"}),
        "FAILED": frozenset({"EXECUTING", "COMPENSATED"}),
        "COMPENSATED": frozenset(),
    }
)

_APPROVAL: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "PENDING": frozenset({"APPROVED", "REJECTED", "CANCELLED", "TIMEOUT"}),
        "APPROVED": frozenset(),
        "REJECTED": frozenset(),
        "CANCELLED": frozenset(),
        "TIMEOUT": frozenset(),
    }
)

WORKFLOW_TRANSITIONS: Mapping[str, Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "instance": _INSTANCE,
        "step": _STEP,
        "command": _COMMAND,
        "approval": _APPROVAL,
    }
)


def workflowStatuses(kind: str) -> frozenset[str]:
    """返回某类实体的**全部**状态字面量（含终态）。

    未知类别返回空集合（失败关闭：调用方拿不到任何状态 ⇒ 放行不了）。
    """
    transitions = WORKFLOW_TRANSITIONS.get(kind)
    if transitions is None:
        return frozenset()
    return frozenset(transitions)


def _raiseInvalid(
    kind: str,
    src: str,
    dst: str,
    allowed: frozenset[str],
) -> NoReturn:
    allowedList = sorted(allowed)
    raise ConflictError(
        MSG_WORKFLOW_STATUS_TRANSITION_INVALID.format(
            kind=kind,
            src=src or "<unknown>",
            dst=dst or "<unknown>",
            allowed=", ".join(allowedList) if allowedList else "无",
        ),
        detail=DETAIL_INVALID_STATUS_TRANSITION,
        details={
            "kind": kind,
            "from": src,
            "to": dst,
            "allowed": allowedList,
        },
    )


def assertWorkflowTransition(kind: str, fromStatus: str, toStatus: str) -> None:
    """校验工作流状态流转；非法（含未知状态 / 未知类别）⇒ ConflictError(409)。

    同态（src == dst）是 no-op。
    """
    transitions = WORKFLOW_TRANSITIONS.get(kind)
    if transitions is None:
        _raiseInvalid(kind, fromStatus, toStatus, frozenset())

    src = fromStatus if isinstance(fromStatus, str) else ""
    dst = toStatus if isinstance(toStatus, str) else ""

    if src in transitions and dst in transitions and (src == dst or dst in transitions[src]):
        return
    _raiseInvalid(kind, src, dst, transitions.get(src, frozenset()))
