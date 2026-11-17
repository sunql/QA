"""P5 Command 执行器 —— **端口契约**（Protocol + 模拟适配器）单测（W3 / D-8）。

D-8 的形状：外部写（ERP / SRM 等真实系统）先落成 **Protocol + 默认实现 + 契约测试**
三件套 ⇒ 真实适配器落地时**不改执行器核心**（只注册一个新 handler）。

本文件钉住的是**契约**，不是实现细节：

1. **未知 command_type 必须失败关闭**：注册表解析不到 ⇒ 抛 ``UnknownCommandTypeError``
   （绝不放行、绝不静默跳过）。
2. **重复注册拒绝**：同一 command_type 注册两个适配器 ⇒ 冲突（否则行为取决于注册顺序）。
3. **失败可重试性显式表达**：``CommandExecutionError(retryable=...)``——
   「业务上不可重试」与「暂时性故障」必须能区分（否则毒丸命令会一直重试）。
4. **模拟适配器可注入失败**：按 payload 指令失败 N 次 ⇒ 用于演练补偿链路。

⚠️ 本文件在 ``unit/`` 下 ⇒ **允许新建**（unit 分片按路径稳定哈希，文件集合变化不影响
分片归属）；``integration/`` **禁止新建文件**（分片按「排序下标 mod 4」轮转）。
"""

from __future__ import annotations

import pytest

from app.domain.exceptions import ConflictError
from app.services.workflow_command_ports import (
    CommandExecutionError,
    CommandHandlerRegistry,
    SimulatedCommandHandler,
    UnknownCommandTypeError,
)


class _StubCommand:
    """最小命令替身：适配器只用到 ``command_type`` / ``payload`` / ``attempts`` / ``id``。"""

    def __init__(self, *, commandType: str = "po.create", payload: dict | None = None, attempts: int = 1, commandId: int = 1):
        self.id = commandId
        self.command_type = commandType
        self.payload = payload or {}
        self.attempts = attempts


# =============================================================================
# 注册表
# =============================================================================


def test_注册后可解析出适配器():
    registry = CommandHandlerRegistry()
    handler = SimulatedCommandHandler()

    registry.register("po.create", handler)

    assert registry.resolve("po.create") is handler


def test_未知命令类型失败关闭():
    registry = CommandHandlerRegistry()

    with pytest.raises(UnknownCommandTypeError) as excinfo:
        registry.resolve("never.registered")

    assert "never.registered" in str(excinfo.value)


def test_可选解析对未知类型返回空():
    registry = CommandHandlerRegistry()

    assert registry.resolveOptional("never.registered") is None


def test_重复注册被拒():
    """行为不得取决于注册顺序 ⇒ 同一类型只能注册一个适配器。"""

    registry = CommandHandlerRegistry()
    registry.register("po.create", SimulatedCommandHandler())

    with pytest.raises(ConflictError):
        registry.register("po.create", SimulatedCommandHandler())


# =============================================================================
# 模拟适配器
# =============================================================================


async def test_模拟适配器成功返回回显():
    handler = SimulatedCommandHandler()
    command = _StubCommand(payload={"amount": 100})

    result = await handler.execute(None, command=command)

    assert result["simulated"] is True
    assert result["commandType"] == "po.create"
    assert result["echo"]["amount"] == 100


async def test_模拟适配器按指令失败():
    handler = SimulatedCommandHandler()
    command = _StubCommand(payload={"simulate": {"outcome": "fail", "message": "下游不可达"}})

    with pytest.raises(CommandExecutionError) as excinfo:
        await handler.execute(None, command=command)

    assert "下游不可达" in str(excinfo.value)


async def test_模拟适配器失败次数用尽后成功():
    """``failTimes=2`` ⇒ 前两次失败、第三次成功（演练重试语义）。"""

    handler = SimulatedCommandHandler()
    payload = {"simulate": {"outcome": "fail", "failTimes": 2}}

    for attempts in (1, 2):
        with pytest.raises(CommandExecutionError):
            await handler.execute(None, command=_StubCommand(payload=payload, attempts=attempts))

    result = await handler.execute(None, command=_StubCommand(payload=payload, attempts=3))
    assert result["simulated"] is True


async def test_模拟适配器认得命名空间后的演练指令():
    """编排器把**步骤**载荷放进 ``payload["step"]`` ⇒ 演练指令仍要认得。

    为什么：控制键（``simulate``）只允许来自**工作流定义**（作者）⇒ 它留在
    ``step`` 命名空间里；用户输入在 ``request`` 命名空间 ⇒ 两者不再混淆。
    """

    handler = SimulatedCommandHandler()
    command = _StubCommand(
        payload={"request": {"amount": 100}, "step": {"simulate": {"outcome": "fail", "message": "下游不可达"}}}
    )

    with pytest.raises(CommandExecutionError) as excinfo:
        await handler.execute(None, command=command)

    assert "下游不可达" in str(excinfo.value)


async def test_模拟适配器忽略用户命名空间里的演练指令():
    """``request.simulate`` **不生效**（用户不能左右适配器行为）。"""

    handler = SimulatedCommandHandler()
    command = _StubCommand(payload={"request": {"simulate": {"outcome": "fail"}}})

    result = await handler.execute(None, command=command)

    assert result["simulated"] is True


async def test_模拟适配器补偿可调用():
    handler = SimulatedCommandHandler()

    result = await handler.compensate(None, command=_StubCommand())

    assert result["compensated"] is True
    assert result["commandType"] == "po.create"


async def test_不可重试异常显式标记():
    err = CommandExecutionError("业务上不允许重试", retryable=False)

    assert err.retryable is False
    assert CommandExecutionError("暂时故障").retryable is True
