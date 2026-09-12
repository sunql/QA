"""Agent 工具的类型契约（feat-wiki-knowledge M8 从 ``agent_tools`` 抽出）。

**为什么单独一个模块**：``agent_tools`` 是「工具注册表 + 内置 handler」，
``agent_tools_wiki`` 是「Wiki 领域 handler」；后者要构造 ``ToolResult``，
前者要 import 后者的 handler 去填 ``BUILTIN_HANDLERS``。若类型仍定义在
``agent_tools`` 里，两个模块就互为导入方 —— 循环。把**被双方共享的类型**
下沉到本模块即可解开，且不改变任何运行时身份（``agent_tools`` 仍 re-export
这些名字，既有 ``from app.services.agent_tools import ToolResult`` 全部照旧）。

本模块只有声明，没有逻辑。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.llm.base_client import BaseLlmClient

logger = logging.getLogger(__name__)

LlmFactory = Callable[[Any], BaseLlmClient]


@dataclass(frozen=True)
class AgentToolContext:
    """工具执行上下文（每次 run 注入；不可变）。"""

    llm_factory: LlmFactory | None = None
    actor: str = "runtime"


@dataclass(frozen=True)
class ToolResult:
    """工具执行结果。data 必须 JSON-serializable（camelCase alias 对齐 API 契约）。"""

    data: dict
    answer: str
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    llm_model_name: str | None = None


AgentHandler = Callable[[AsyncSession, dict, AgentToolContext], Awaitable[ToolResult]]
ArgExtractor = Callable[[str], dict | None]


@dataclass(frozen=True)
class AgentTool:
    """Agent 可调用的工具（function-calling 风格注册元数据）。"""

    name: str
    description: str
    data_object: str  # ACL 主题，与 AgentAccessPolicy.data_object 对齐
    input_schema: dict  # JSON Schema（供未来 LLM function calling 复用）
    arg_extractor: ArgExtractor  # 原始输入 → args dict；解析失败返回 None
    handler: AgentHandler  # 用解析后的 args 执行并返回 ToolResult
    data_layers: tuple[str, ...] = ()  # 工具读取的数据层（DIM/DWD/FEATURE…）；() = 层无关（回退到对象粒度）
