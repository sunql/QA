"""P5 Command 执行器 —— **端口层**（Protocol + 模拟适配器）。

D-8：**外部写先接口 + 模拟器**。仓内没有任何真实写接口证据（`config.py` 只提到 ERP 名字）
⇒ 本批**只交付接口与模拟器**，真实 ERP/SRM 适配后置。

三条纪律：

1. **Command 是唯一写入口**：外部世界的写操作一律经 ``CommandHandler``；
   适配器之外的服务层不得直接写外部系统。
2. **未知命令类型失败关闭**：注册表解析不到 ⇒ 抛 ``UnknownCommandTypeError``
   （不放行、不静默跳过 —— 与状态机「未知状态失败关闭」同一姿态）。
3. **失败可重试性显式表达**：``CommandExecutionError(retryable=...)``。
   「业务上不可重试」（如参数非法）与「暂时性故障」（如下游超时）必须能区分，
   否则毒丸命令会被无限重试。

**Agent 仍只读**：本模块不提供任何 Agent 可调用的入口 ——
写能力只在 Command 执行器里（且执行器只在 Worker 进程内被调用）。
"""

from __future__ import annotations

from typing import Any, Protocol

from app.domain.exceptions import ConflictError
from app.domain.workflow_models import BusinessCommand

# ---------------------------------------------------------------- 异常


class CommandExecutionError(Exception):
    """适配器执行失败。

    ``retryable=False`` ⇒ 业务上不可重试（如参数非法）⇒ 执行器直接判终态失败，
    不再重试、不触发补偿（重试也没用）。
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class UnknownCommandTypeError(Exception):
    """命令类型没有注册适配器（失败关闭）。"""


# ---------------------------------------------------------------- 端口协议


class CommandHandler(Protocol):
    """业务命令适配器契约。

    实现方负责**外部写**；返回体落 ``business_command.result``（JSON 对象）。
    失败必须抛 ``CommandExecutionError``（携带可重试性）。
    """

    async def execute(self, session: Any, *, command: BusinessCommand) -> dict[str, Any]: ...


class CompensableCommandHandler(CommandHandler, Protocol):
    """支持补偿的适配器（可逆写操作）。

    ``compensate`` 返回体落 ``business_command.result``；返回 ``None`` 表示
    「补偿无需留痕」（此时执行器仍会把命令置为 COMPENSATED）。
    """

    async def compensate(self, session: Any, *, command: BusinessCommand) -> dict[str, Any] | None: ...


def isCompensable(handler: Any) -> bool:
    """判断适配器是否支持补偿（鸭子类型判定，不强制继承）。"""

    return callable(getattr(handler, "compensate", None))


# ---------------------------------------------------------------- 注册表


class CommandHandlerRegistry:
    """``command_type`` → 适配器 的注册表（不可变映射语义：注册后只读）。"""

    def __init__(self) -> None:
        self._handlers: dict[str, Any] = {}

    def register(self, commandType: str, handler: Any) -> None:
        """注册适配器；同一类型重复注册 ⇒ 冲突。"""

        if not commandType or not isinstance(commandType, str):
            raise ConflictError(f"命令类型非法：{commandType!r}")
        if commandType in self._handlers:
            raise ConflictError(f"命令类型「{commandType}」已注册适配器，拒绝重复注册")
        self._handlers[commandType] = handler

    def resolve(self, commandType: str) -> Any:
        """解析适配器；未注册 ⇒ 失败关闭。"""

        handler = self._handlers.get(commandType)
        if handler is None:
            raise UnknownCommandTypeError(f"命令类型「{commandType}」没有注册适配器（失败关闭）")
        return handler

    def resolveOptional(self, commandType: str) -> Any | None:
        """解析适配器；未注册 ⇒ 返回 ``None``（补偿路径用：不补偿 ≠ 放行）。"""

        return self._handlers.get(commandType)

    def registeredTypes(self) -> tuple[str, ...]:
        """已注册的命令类型（只读快照）。"""

        return tuple(sorted(self._handlers))


# ---------------------------------------------------------------- 模拟适配器


class SimulatedCommandHandler:
    """**默认模拟适配器**（D-8）：不写任何外部系统，只回显 + 可注入失败。

    payload 指令（放在 ``payload["simulate"]``）：

    - ``{"outcome": "success"}`` —— 成功（默认）；
    - ``{"outcome": "fail", "message": "..."}`` —— 每次都失败；
    - ``{"outcome": "fail", "failTimes": 2, "message": "..."}`` —— 前 N 次失败，
      之后成功（演练重试语义）。

    真实适配器落地后，本类只用于**本地演练与契约自检**，不再承载生产写。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def execute(self, session: Any, *, command: BusinessCommand) -> dict[str, Any]:
        self.calls.append(("execute", command.id))

        instruction = self._instruction(command)
        if instruction.get("outcome") == "fail":
            failTimes = instruction.get("failTimes")
            if failTimes is None or command.attempts <= int(failTimes):
                raise CommandExecutionError(str(instruction.get("message") or "模拟执行失败"))

        return {
            "simulated": True,
            "commandType": command.command_type,
            "echo": command.payload,
        }

    async def compensate(self, session: Any, *, command: BusinessCommand) -> dict[str, Any]:
        self.calls.append(("compensate", command.id))

        return {
            "compensated": True,
            "commandType": command.command_type,
            "echo": command.payload,
        }

    @staticmethod
    def _instruction(command: BusinessCommand) -> dict[str, Any]:
        payload = command.payload or {}
        instruction = payload.get("simulate")
        return instruction if isinstance(instruction, dict) else {}
