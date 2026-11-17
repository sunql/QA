"""P5 工作流状态流转矩阵（纯函数）。

照 `app/services/kpi_status_machine.py` 的形态（D-4）：转移矩阵是
`MappingProxyType`（不可变）+ ``assert*Transition()`` 抛
``ConflictError(409, "INVALID_STATUS_TRANSITION")``；**未知状态失败关闭**
（未知 ≠ 放行）。纯函数 ⇒ 不吃 DB，可穷举全部路径。

四类实体各有一套矩阵：

- **instance**（工作流实例）：PENDING → RUNNING → （WAITING_APPROVAL → APPROVED）
  → COMPLETED；失败可重试（FAILED → RUNNING）或补偿（→ COMPENSATED）。
- **step**（步骤）：PENDING → RUNNING → SUCCEEDED / FAILED；失败可重试或补偿；
  命令执行前被取消 ⇒ RUNNING → SKIPPED（这一步其实没跑）。
- **command**（业务命令）：PENDING → EXECUTING → SUCCEEDED / FAILED；
  失败可重试（FAILED → EXECUTING）或补偿（→ COMPENSATED）；
  **未执行即可取消**（PENDING → CANCELLED ⇒ 实例取消 / 补偿后不再执行）。
- **approval**（审批任务）：PENDING → APPROVED / REJECTED / CANCELLED / TIMEOUT。

终态（无出边）：instance 的 REJECTED / CANCELLED / COMPENSATED；step 的
SKIPPED / COMPENSATED；command 的 CANCELLED / COMPENSATED；approval 的四个终态。

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
        # RUNNING → SKIPPED：**命令在执行前被取消**（实例取消 / 补偿）
        # ⇒ 这一步其实没跑 ⇒ 记「跳过」，而不是吊在 RUNNING。
        "RUNNING": frozenset({"SUCCEEDED", "FAILED", "SKIPPED"}),
        "SUCCEEDED": frozenset({"COMPENSATED"}),
        "FAILED": frozenset({"RUNNING", "COMPENSATED"}),
        "SKIPPED": frozenset(),
        "COMPENSATED": frozenset(),
    }
)

_COMMAND: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "PENDING": frozenset({"EXECUTING", "FAILED", "CANCELLED", "ABORTED"}),
        # EXECUTING → CANCELLED：Worker 领走命令后、真正调适配器**之前**，
        # 发现实例已被取消 ⇒ 不执行（外部写不再发生），命令落终态。
        # EXECUTING → ABORTED：**终态失败**（不可重试 / 次数耗尽）⇒ 落库即终态。
        #   （失败收敛用 SAVEPOINT ⇒ claim 保留 ⇒ 失败终态从 EXECUTING 直接落。）
        "EXECUTING": frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "ABORTED"}),
        "SUCCEEDED": frozenset({"COMPENSATED"}),
        # FAILED → CANCELLED：可重试失败的命令也必须能被取消
        # （否则取消后它仍在可领取集合里 ⇒ Worker 照样写外部系统）。
        # FAILED → ABORTED：重试到终态失败 ⇒ 落终态，不再被领取。
        "FAILED": frozenset({"EXECUTING", "COMPENSATED", "CANCELLED", "ABORTED"}),
        "CANCELLED": frozenset(),
        # ABORTED：**终态失败**（业务上不可重试 / 尝试次数耗尽）
        # ⇒ 落库即终态 ⇒ Worker 不会再把它当「可重试失败」重领。
        "ABORTED": frozenset(),
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

# 实例状态的**两个口径**（单一出口：编排器 / 服务层 / 执行器都从这里取，
# 避免「终态集合」在三处各写一份、越写越歪）。
#
# - ``LIVE_INSTANCE_STATUSES``：实例还在跑 ⇒ 允许继续推进、允许领取它的命令。
# - ``STOPPED_INSTANCE_STATUSES``：实例已停止 ⇒ **不得**再发起新的外部写
#   （取消 / 补偿 / 完成 / 拒绝 / 失败后的实例都不许再动外部系统）。
#
# ⚠️ ``FAILED`` 归入 STOPPED：状态机允许 ``FAILED → RUNNING``（显式重试），
# 但**在状态被显式改回 RUNNING 之前**，不得领取它的命令。
LIVE_INSTANCE_STATUSES = frozenset({"PENDING", "RUNNING", "WAITING_APPROVAL", "APPROVED"})
STOPPED_INSTANCE_STATUSES = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED", "REJECTED", "COMPENSATED"}
)


def isLiveInstance(status: str) -> bool:
    """实例是否还在跑（可继续推进 / 可领取它的命令）。"""

    return status in LIVE_INSTANCE_STATUSES


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
