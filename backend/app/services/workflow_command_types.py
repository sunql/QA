"""P5 命令执行的**结果类型与状态字面量**（共享单一出口）。

为什么单独成文件：``CommandOutcome`` 既是**执行器**的返回值，也是**补偿器**的
返回值 ⇒ 两个模块都要用它 ⇒ 若它住在执行器里，补偿模块就得 import 执行器，
而执行器又要 import 补偿模块 ⇒ **循环依赖**。⇒ 结果类型下沉到本文件。

⚠️ **状态字面量与库层 CHECK 表达式必须一致**（由
``app/tests/unit/test_workflow_state_machine.py`` 末尾的钉合用例保证）⇒
本文件只声明「**命令这条线**」的状态；实例 / 步骤 / 审批的状态矩阵见
``workflow_state_machine.py``（那份是唯一权威）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 命令状态（与 ``business_command.status`` 的 CHECK 表达式一致）。
STATUS_PENDING = "PENDING"
STATUS_EXECUTING = "EXECUTING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_ABORTED = "ABORTED"
STATUS_CANCELLED = "CANCELLED"
STATUS_COMPENSATED = "COMPENSATED"
STATUS_SKIPPED = "SKIPPED"

# 步骤 / 实例状态（补偿要推进它们 ⇒ 与命令状态放在一起共用一份字面量）。
STATUS_RUNNING = "RUNNING"
STATUS_COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class CommandOutcome:
    """一次命令执行（或一次补偿）的结果（**不可变**）。

    ``errorKind`` 区分**基础设施错误**与**适配器错误**（见
    ``workflow_errors.py``）⇒ 运维能分辨「等一等就好」与「多半要改」。
    """

    commandId: int
    status: str
    attempts: int
    result: dict[str, Any] | None = None
    errorMessage: str | None = None
    isTerminal: bool = False
    errorKind: str = ""
