"""LangGraph State 定义 — L4 Agent Loop 用。

Schema reference：Task 4.3 brief

Architecture note: 本实现用纯 Python async while loop，未用 LangGraph StateGraph。
AgentState TypedDict 保留为后续 LangGraph 升级占位，不引用 langchain_core 以避免
测试环境额外依赖。
"""

from typing import TypedDict, Any


class AgentState(TypedDict, total=False):
    """L4 Agent Loop 的 LangGraph 状态。

    - messages:   完整对话历史（含 HumanMessage / AIMessage / ToolMessage）
    - iterations: 已循环次数
    - final_sql:  若 LLM 给出最终 SQL，记录于此
    - error:      异常路径
    """

    messages: list[Any]
    iterations: int
    final_sql: str | None
    error: str | None
