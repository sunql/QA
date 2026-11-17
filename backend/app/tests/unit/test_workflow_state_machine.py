"""P5 工作流状态机（纯函数）单测。

状态机是**纯函数矩阵**（D-4，照 `app/services/kpi_status_machine.py` 的形态）：
不查库、不吃 IO ⇒ 可以穷举全部路径。

被测纪律：

1. 转移矩阵**不可变**（`MappingProxyType`）⇒ 运行时改不了；
2. 非法流转抛 `ConflictError(409, "INVALID_STATUS_TRANSITION")` ⇒ 拒绝即零副作用；
3. **未知状态失败关闭**（未知 ≠ 放行；即便同态也拒绝）；
4. 同态是 no-op；
5. 矩阵里的状态集合必须与**库层 CHECK 表达式**的字面量集合**完全一致**
   ⇒ 否则「状态机允许、库层拒绝」或反之 ⇒ 最后一条用例把两侧钉在一起。
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from app.domain.exceptions import ConflictError
from app.domain.workflow_models import (
    ApprovalTask,
    BusinessCommand,
    WorkflowInstance,
    WorkflowStep,
)
from app.services.workflow_state_machine import (
    DETAIL_INVALID_STATUS_TRANSITION,
    LIVE_INSTANCE_STATUSES,
    STOPPED_INSTANCE_STATUSES,
    WORKFLOW_TRANSITIONS,
    assertWorkflowTransition,
    workflowStatuses,
)

_KIND_BY_MODEL = {
    WorkflowInstance: "instance",
    WorkflowStep: "step",
    BusinessCommand: "command",
    ApprovalTask: "approval",
}


# ---------------------------------------------------------------- 矩阵形状


def test_矩阵不可变():
    """运行时改矩阵 ⇒ 状态机的行为不可预测 ⇒ 矩阵必须冻结。"""

    assert isinstance(WORKFLOW_TRANSITIONS, MappingProxyType)
    for kind, transitions in WORKFLOW_TRANSITIONS.items():
        assert isinstance(transitions, MappingProxyType), kind
        for targets in transitions.values():
            assert isinstance(targets, frozenset), kind


def test_四类实体各有独立矩阵():
    assert set(WORKFLOW_TRANSITIONS) == {"instance", "step", "command", "approval"}
    for kind, transitions in WORKFLOW_TRANSITIONS.items():
        assert transitions, kind


def test_矩阵的键集合等于目标状态集合():
    """矩阵的键（源状态）必须覆盖全部状态，否则某些状态「查不到允许目标」。"""

    for kind, transitions in WORKFLOW_TRANSITIONS.items():
        assert set(transitions) == workflowStatuses(kind), kind


# ---------------------------------------------------------------- 合法流转


@pytest.mark.parametrize(
    ("kind", "src", "dst"),
    [
        ("instance", "PENDING", "RUNNING"),
        ("instance", "PENDING", "CANCELLED"),
        ("instance", "RUNNING", "WAITING_APPROVAL"),
        ("instance", "RUNNING", "COMPLETED"),
        ("instance", "WAITING_APPROVAL", "APPROVED"),
        ("instance", "WAITING_APPROVAL", "REJECTED"),
        ("instance", "APPROVED", "RUNNING"),
        ("instance", "COMPLETED", "COMPENSATED"),
        ("instance", "FAILED", "RUNNING"),
        ("instance", "FAILED", "COMPENSATED"),
        ("step", "PENDING", "RUNNING"),
        ("step", "PENDING", "SKIPPED"),
        ("step", "RUNNING", "SUCCEEDED"),
        ("step", "RUNNING", "FAILED"),
        ("step", "FAILED", "RUNNING"),
        ("step", "SUCCEEDED", "COMPENSATED"),
        ("command", "PENDING", "EXECUTING"),
        ("command", "EXECUTING", "SUCCEEDED"),
        ("command", "EXECUTING", "FAILED"),
        ("command", "FAILED", "EXECUTING"),
        ("command", "SUCCEEDED", "COMPENSATED"),
        # 取消：PENDING / FAILED 都必须能被取消（否则取消后仍可被领取）
        ("command", "PENDING", "CANCELLED"),
        ("command", "FAILED", "CANCELLED"),
        # Worker 领走后、真正执行前发现实例已取消 ⇒ 不执行
        ("command", "EXECUTING", "CANCELLED"),
        # 终态失败（不可重试 / 次数耗尽）⇒ 落库即终态
        ("command", "PENDING", "ABORTED"),
        ("command", "FAILED", "ABORTED"),
        ("approval", "PENDING", "APPROVED"),
        ("approval", "PENDING", "REJECTED"),
        ("approval", "PENDING", "TIMEOUT"),
        ("approval", "PENDING", "CANCELLED"),
    ],
)
def test_合法流转放行(kind: str, src: str, dst: str):
    assertWorkflowTransition(kind, src, dst)


# ---------------------------------------------------------------- 非法流转


def test_非法流转抛409():
    with pytest.raises(ConflictError) as excinfo:
        assertWorkflowTransition("instance", "PENDING", "COMPLETED")

    assert excinfo.value.detail == DETAIL_INVALID_STATUS_TRANSITION
    assert excinfo.value.details == {
        "kind": "instance",
        "from": "PENDING",
        "to": "COMPLETED",
        "allowed": ["CANCELLED", "FAILED", "RUNNING"],
    }


def test_终态没有出边():
    """REJECTED / CANCELLED / COMPENSATED / TIMEOUT 是终态 ⇒ 不能复活。"""

    for kind, terminal in (
        ("instance", "REJECTED"),
        ("instance", "CANCELLED"),
        ("instance", "COMPENSATED"),
        ("step", "SKIPPED"),
        ("step", "COMPENSATED"),
        ("command", "COMPENSATED"),
        ("command", "CANCELLED"),
        ("command", "ABORTED"),
        ("approval", "APPROVED"),
        ("approval", "REJECTED"),
        ("approval", "CANCELLED"),
        ("approval", "TIMEOUT"),
    ):
        assert WORKFLOW_TRANSITIONS[kind][terminal] == frozenset(), (kind, terminal)
        with pytest.raises(ConflictError):
            assertWorkflowTransition(kind, terminal, "RUNNING")


def test_未知状态失败关闭():
    """未知状态**不放行**（即便 src == dst）。"""

    with pytest.raises(ConflictError):
        assertWorkflowTransition("instance", "NOT_A_STATE", "RUNNING")
    with pytest.raises(ConflictError):
        assertWorkflowTransition("instance", "PENDING", "NOT_A_STATE")
    with pytest.raises(ConflictError):
        assertWorkflowTransition("instance", "NOT_A_STATE", "NOT_A_STATE")


def test_未知实体类别失败关闭():
    with pytest.raises(ConflictError):
        assertWorkflowTransition("not_a_kind", "PENDING", "RUNNING")


def test_同态是空操作():
    """同态放行（幂等重放：重复推进同一状态不算错）。"""

    for kind, statuses in (
        ("instance", ("PENDING", "RUNNING", "COMPLETED")),
        ("step", ("PENDING", "SUCCEEDED")),
        ("command", ("PENDING", "SUCCEEDED")),
        ("approval", ("PENDING", "APPROVED")),
    ):
        for status in statuses:
            assertWorkflowTransition(kind, status, status)


def test_错误信息含来源与允许目标():
    with pytest.raises(ConflictError) as excinfo:
        assertWorkflowTransition("step", "PENDING", "SUCCEEDED")

    message = str(excinfo.value)
    assert "PENDING" in message
    assert "SUCCEEDED" in message
    assert "RUNNING" in message  # 允许目标之一
    assert "SKIPPED" in message


# ---------------------------------------------------------------- 与库层 CHECK 对齐


def test_状态集合与库层check字面量完全一致():
    """状态机允许的状态集合 == DB CHECK 的字面量集合。

    两侧不一致 ⇒ 两种坏结果之一：
    ① 状态机放行但库层 CHECK 拒绝 ⇒ 运行时 500；
    ② 库层允许但状态机拒绝 ⇒ 合法路径走不通。
    """

    for model, kind in _KIND_BY_MODEL.items():
        table = model.__table__
        checks = [
            str(constraint.sqltext)
            for constraint in table.constraints
            if constraint.name == f"ck_{table.name}_status"
        ]
        assert len(checks) == 1, table.name
        literals = _parseStatusLiterals(checks[0])
        assert literals == workflowStatuses(kind), table.name


def _parseStatusLiterals(expression: str) -> frozenset[str]:
    """从 ``status IN ('A','B')`` 里抽出字面量集合。"""

    assert expression.startswith("status IN (")
    inner = expression[len("status IN (") : expression.rindex(")")]
    return frozenset(part.strip().strip("'") for part in inner.split(","))


# ------------------------------------------------- LIVE / STOPPED 是实例状态的全划分


def test_live与stopped恰好划分实例状态():
    """两个口径必须是**互斥且完备**的划分。

    ⚠️ 这是「终态集合单一出口」的证据：服务层 / 编排器 / 存储层都从
    ``workflow_state_machine`` 取这两个集合 ⇒ 只要它们仍是对实例状态全集的
    划分，任何「终态判定」都不会漏掉某个状态、也不会把活着的实例当终态。
    """

    allStatuses = workflowStatuses("instance")

    assert LIVE_INSTANCE_STATUSES & STOPPED_INSTANCE_STATUSES == frozenset()
    assert LIVE_INSTANCE_STATUSES | STOPPED_INSTANCE_STATUSES == allStatuses


def test_终态判定都从状态机模块取():
    """服务层 / 编排器**不得**再自带一份终态集合（单一出口的回归守卫）。

    早期两份副本（``workflow_service._TERMINAL_INSTANCE_STATUSES`` /
    ``workflow_orchestrator._TERMINAL_INSTANCE_STATUSES``）各写一遍 ⇒ 状态机
    加了终态时，两份副本漏改 ⇒ 「实例已终态」判定在某一层失效（取消会打到
    已完成的实例上）。
    """

    from app.services import workflow_orchestrator, workflow_service

    allowed = {LIVE_INSTANCE_STATUSES, STOPPED_INSTANCE_STATUSES}
    for module in (workflow_service, workflow_orchestrator):
        for name in dir(module):
            if not name.endswith("_INSTANCE_STATUSES"):
                continue
            value = getattr(module, name)
            assert value in allowed, (
                f"{module.__name__}.{name} 是自带副本，不是状态机模块那一份：{value}"
            )
