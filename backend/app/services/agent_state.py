"""LangGraph State 定义 — L4 Agent Loop 用。

Schema reference：Task 4.3 brief
"""

from typing import TypedDict

from langchain_core.messages import BaseMessage


class AgentState(TypedDict, total=False):
    """L4 Agent Loop 的 LangGraph 状态。

    - messages:   完整对话历史（含 HumanMessage / AIMessage / ToolMessage）
    - iterations: 已循环次数
    - final_sql:  若 LLM 给出最终 SQL，记录于此
    - error:      异常路径
    """

    messages: list[BaseMessage]
    iterations: int
    final_sql: str | None
    error: str | None
