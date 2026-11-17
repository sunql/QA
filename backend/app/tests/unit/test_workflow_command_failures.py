"""P5 命令**失败收敛**单测（错误净化 / 错误归类 / 超时 / 次数写回）。

为什么单独成文件：失败收敛是执行器的一条**独立轴**（不是「执行成功」也不是
「补偿」）⇒ 与执行、补偿各占一个文件 ⇒ 三个文件都在行数上限内。

被测纪律：

1. **错误消息落库前必净化**（M6）：适配器抛的消息可能带**凭据 / PII**
   （连接串里的密码、token、下游整段报文）⇒ 直接落库 = 凭据进库（**不可逆**）
   ⇒ 落库前抹值留键、控长度、压控制字符。
2. **错误必须可归类**（L4）：**基础设施错误**（DB / 网络 / 超时）与
   **业务适配器错误**（下游明确拒绝）必须可区分 —— 前者「等一等就好」，
   后者「多半要改代码或数据」⇒ 混成一句话 ⇒ 处置动作反了。
3. **适配器必须有时限**（超时）：没有超时 ⇒ 一个卡死的下游能把 Worker 那一轮
   永远占住（事务不提交 ⇒ 连接不释放 ⇒ 连接池耗尽 ⇒ 全部命令停摆）。
4. **尝试次数只增不减**（L1）：并发失败时两次写回可能带着**不同的**次数
   ⇒ 若直接覆盖，次数会**往回退** ⇒ 毒丸命令永远达不到上限 ⇒ 无限重试。

全程走**真实 PostgreSQL**（``workflow_*`` 表是真表、真事务）。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, update

from app.domain.workflow_models import (
    BusinessCommand,
    WorkflowDefinition,
    WorkflowOutbox,
)
from app.services.workflow_command_executor import CommandExecutor
from app.services.workflow_command_ports import (
    CommandExecutionError,
    CommandHandlerRegistry,
)
from app.services.workflow_errors import (
    ERROR_KIND_ADAPTER,
    ERROR_KIND_INFRA,
    MAX_ERROR_MESSAGE_LEN,
    classifyError,
)
from app.services.workflow_store import WorkflowStore

_store = WorkflowStore()


@pytest.fixture()
async def definition(dbSession) -> WorkflowDefinition:
    row = WorkflowDefinition(
        domain_code="procurement",
        capability_code="po_write",
        version="v1",
        definition_json={"steps": [{"code": "write", "order": 0}]},
        status="PUBLISHED",
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


async def _command(dbSession, definition, *, key: str) -> int:
    """建实例 + 步骤 + 一条待执行命令，返回命令 id。"""

    instance, _ = await _store.createInstance(
        dbSession,
        definitionId=definition.id,
        idempotencyKey=f"fail-{key}",
        requestJson={},
        actor="submitter",
    )
    steps = await _store.createSteps(
        dbSession,
        instanceId=instance.id,
        steps=[{"code": "write", "order": 0}],
    )
    await dbSession.commit()
    await _store.advanceInstance(dbSession, instance.id, "RUNNING")
    await dbSession.commit()
    await _store.advanceStep(dbSession, steps[0].id, "RUNNING")
    await dbSession.commit()

    command, _ = await _store.enqueueCommand(
        dbSession,
        instanceId=instance.id,
        stepId=steps[0].id,
        commandType="po.write",
        idempotencyKey=key,
        payload={},
    )
    await dbSession.commit()
    return command.id


def _executor(handler, *, maxAttempts: int = 3, timeoutSeconds: float = 5.0) -> CommandExecutor:
    registry = CommandHandlerRegistry()
    registry.register("po.write", handler)
    return CommandExecutor(
        registry,
        maxAttempts=maxAttempts,
        commandTimeoutSeconds=timeoutSeconds,
    )


async def _errorMessage(dbSession, commandId: int) -> str:
    row = (
        await dbSession.execute(
            select(BusinessCommand)
            .where(BusinessCommand.id == commandId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row.error_message or ""


class _Raising:
    """按剧本抛异常的适配器。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def execute(self, session, *, command):
        raise self._exc

    async def compensate(self, session, *, command):
        return {"compensated": True}


# =============================================================================
# M6：错误消息净化（落库前）
# =============================================================================


async def test_连接串里的密码落库前被抹掉(dbSession, definition):
    """适配器抛的连接串里带密码 ⇒ 落库消息里**不得**出现该密码。

    回归点：适配器异常消息被原样写进 ``business_command.error_message``
    ⇒ 凭据进库 ⇒ 不可逆（导出 / 备份 / 巡检日志全带着它）。
    """

    commandId = await _command(dbSession, definition, key="fail-url")
    executor = _executor(
        _Raising(
            CommandExecutionError(
                "连不上下游：postgres://qa_user:s3cr3tPwd@db.internal:5432/qa"
            )
        )
    )

    await executor.execute(dbSession, commandId, workerId="worker-1")
    await dbSession.commit()

    message = await _errorMessage(dbSession, commandId)
    assert "s3cr3tPwd" not in message
    assert "postgres://***@db.internal:5432/qa" in message


async def test_键值形态的凭据落库前被抹值留键(dbSession, definition):
    """``password=xxx`` / ``token: xxx`` ⇒ **值**抹掉、**键**保留。

    保留键名是为了不丢「哪里出的问题」这一信息；抹掉值是为了不泄密。
    """

    commandId = await _command(dbSession, definition, key="fail-kv")
    executor = _executor(
        _Raising(
            CommandExecutionError("鉴权失败 password=hunter2 token: abc123 结束")
        )
    )

    await executor.execute(dbSession, commandId, workerId="worker-1")
    await dbSession.commit()

    message = await _errorMessage(dbSession, commandId)
    assert "hunter2" not in message
    assert "abc123" not in message
    assert "password=***" in message
    assert "token: ***" in message


async def test_控制字符落库前被压平(dbSession, definition):
    """多行消息 ⇒ 能**伪造审计行**（破坏审计的可解析性）⇒ 落库前压平。

    回归点：``\\n`` 直接进 ``error_message`` ⇒ 巡检按行解析审计时会把一条
    失败看成两条（甚至看成一条成功）。
    """

    commandId = await _command(dbSession, definition, key="fail-ctrl")
    executor = _executor(
        _Raising(CommandExecutionError("第一行\n伪造的审计行\r换行结束"))
    )

    await executor.execute(dbSession, commandId, workerId="worker-1")
    await dbSession.commit()

    message = await _errorMessage(dbSession, commandId)
    assert "\n" not in message
    assert "\r" not in message
    assert "第一行" in message
    assert "伪造的审计行" in message


async def test_超长错误消息落库前被截断(dbSession, definition):
    """错误消息**必须有界**（``error_message`` 是 ``Text`` ⇒ 不设上限 = 一条
    异常报文就能把审计表撑大）。"""

    commandId = await _command(dbSession, definition, key="fail-long")
    executor = _executor(_Raising(CommandExecutionError("x" * 5000)))

    await executor.execute(dbSession, commandId, workerId="worker-1")
    await dbSession.commit()

    message = await _errorMessage(dbSession, commandId)
    assert len(message) <= MAX_ERROR_MESSAGE_LEN + 32
    assert message.endswith("…（已截断）")


# =============================================================================
# L4：错误归类（基础设施 vs 适配器）
# =============================================================================


def test_数据库异常归类为基础设施错误():
    """DB 抖动 / 连接断了 ⇒ 基础设施错误 ⇒ 重试有意义。"""

    from sqlalchemy.exc import OperationalError

    assert classifyError(OperationalError("stmt", {}, Exception("boom"))) == ERROR_KIND_INFRA


def test_超时归类为基础设施错误():
    assert classifyError(TimeoutError()) == ERROR_KIND_INFRA


def test_业务异常归类为适配器错误():
    assert classifyError(ValueError("参数非法")) == ERROR_KIND_ADAPTER


async def test_适配器超时被归类为基础设施错误且可重试(dbSession, definition):
    """适配器卡死 ⇒ 超时 ⇒ **可重试**的失败（不是终态失败）。

    回归点：超时若被当成「业务拒绝」⇒ 终态失败 ⇒ 触发补偿 ⇒ 下游其实只是
    慢 ⇒ 白白把已成功的外部写回滚掉。
    """

    class _Hang:
        async def execute(self, session, *, command):
            await asyncio.sleep(30)
            return {"ok": True}

    commandId = await _command(dbSession, definition, key="fail-timeout")
    executor = _executor(_Hang(), timeoutSeconds=0.05)

    outcome = await executor.execute(dbSession, commandId, workerId="worker-1")
    await dbSession.commit()

    assert outcome.status == "FAILED"
    assert outcome.isTerminal is False
    assert outcome.errorKind == ERROR_KIND_INFRA
    assert "基础设施错误" in (outcome.errorMessage or "")

    # 命令**不留在执行中**（否则 Worker 永远领不走它 ⇒ 僵尸命令）。
    row = (
        await dbSession.execute(
            select(BusinessCommand)
            .where(BusinessCommand.id == commandId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status == "FAILED"
    assert row.attempts == 1


async def test_普通适配器异常被归类为适配器错误(dbSession, definition):
    """业务拒绝（参数非法 / 契约不符）⇒ 适配器错误 ⇒ 与基础设施错误可区分。"""

    commandId = await _command(dbSession, definition, key="fail-adapter")
    executor = _executor(_Raising(ValueError("下游返回了非预期的结构")))

    outcome = await executor.execute(dbSession, commandId, workerId="worker-1")
    await dbSession.commit()

    assert outcome.errorKind == ERROR_KIND_ADAPTER
    assert "适配器错误" in (outcome.errorMessage or "")


# =============================================================================
# L1：尝试次数只增不减
# =============================================================================


async def test_失败写回不把尝试次数改小(dbSession, definition):
    """并发失败时两次写回可能带着**不同的**次数 ⇒ 直接覆盖会让次数**往回退**。

    回归点：次数回退 ⇒ 毒丸命令永远达不到上限 ⇒ 无限重试。
    """

    commandId = await _command(dbSession, definition, key="fail-attempts")

    claimed = await _store.claimCommand(
        dbSession, commandId, workerId="worker-1", maxAttempts=3
    )
    assert claimed is not None
    # 模拟「另一次并发失败已把次数推到 3」。
    await dbSession.execute(
        update(BusinessCommand)
        .where(BusinessCommand.id == commandId)
        .values(attempts=3)
    )
    await dbSession.flush()

    await _store.failCommand(
        dbSession,
        commandId,
        attempts=1,
        errorMessage="失败写回（携带较旧的次数）",
    )
    await dbSession.commit()

    row = (
        await dbSession.execute(
            select(BusinessCommand)
            .where(BusinessCommand.id == commandId)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.attempts == 3


# =============================================================================
# R12-L3：SAVEPOINT 回滚**不得**抹掉 claim 事件（已取证 ⇒ 钉成契约）
# =============================================================================


async def test_适配器失败后领取事件仍在(dbSession, definition):
    """``command_claimed`` 事件必须**活下来**（SAVEPOINT 回滚不得抹掉它）。

    为什么要取证：失败收敛用 SAVEPOINT 回滚「适配器写的那一段」⇒ 若
    ``command_claimed`` 事件恰好写在 SAVEPOINT **之内** ⇒ 回滚会把它抹掉 ⇒
    事件流里只有「入队」没有「被领走」⇒ 事后**无法**回答「谁领走了这条命令」
    ⇒ 追责链断在事件流上。

    取证结论：claim 事件写在 claim 阶段（SAVEPOINT **之外**）⇒ **幸存**
    ⇒ 本用例把该结论钉成契约（防止后续重构把它挪进 SAVEPOINT）。
    """

    commandId = await _command(dbSession, definition, key="fail-outbox")
    executor = _executor(_Raising(CommandExecutionError("下游拒了")))

    outcome = await executor.execute(dbSession, commandId, workerId="worker-1")
    await dbSession.commit()

    assert outcome.status == "FAILED"

    eventTypes = (
        (
            await dbSession.execute(
                select(WorkflowOutbox.event_type)
                .where(WorkflowOutbox.payload["commandId"].astext == str(commandId))
                .order_by(WorkflowOutbox.id)
            )
        )
        .scalars()
        .all()
    )
    assert "command_claimed" in eventTypes
    assert "command_failed" in eventTypes
