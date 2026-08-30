"""SSE 流式事件契约（5.6）。

定义服务层产出、API 层序列化的统一事件格式：
`event: <type>\ndata: <json>\n\n`（text/event-stream）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

# 事件类型常量
EVENT_META = "meta"      # 意图识别结果
EVENT_PLAN = "plan"      # ReAct 查询计划（QueryPlan 字典，Phase E 流式展示）
EVENT_SQL = "sql"        # 生成的只读 SQL
EVENT_CHART = "chart"    # 图表类型 + ECharts option + 数据
EVENT_TOKEN = "token"    # 回答增量文本（逐 token/块）
EVENT_DONE = "done"      # 完成：携带累计 token/成本
EVENT_ERROR = "error"    # 失败：携带用户友好错误 + errorType（4-1）
EVENT_MULTI_STEP_PLAN = "multi_step_plan"  # 多步：拆解出的完整计划概览（steps 数组，循环前一次下发）
EVENT_STEP_PLAN = "step_plan"    # 多步：每个子步骤的计划（含 description / sub_question）
EVENT_STEP_RESULT = "step_result"  # 多步：每个子步骤的执行结果（sql + data + summary）
EVENT_DATA_QUALITY = "data_quality"  # Phase 1.4：目标表的可信度 badge 列表（每张 selectedClass 一条）


class ErrorType(str, Enum):
    """error 事件的可枚举错误类型（4-1），供前端差异化降级。

    - DOMAIN：领域错误（业务规则/校验失败），error 消息可直接展示。
    - LLM：LLM 调用失败，可提示服务降级或稍后重试。
    - INTERNAL：未预期异常，展示通用错误提示（不泄露内部细节）。
    """

    DOMAIN = "domain"
    LLM = "llm"
    INTERNAL = "internal"


@dataclass(frozen=True)
class StreamEvent:
    """一条 SSE 事件（不可变）。"""

    event: str
    data: dict[str, Any]

    def toSse(self) -> str:
        """序列化为 SSE 帧文本（event + data，data 为 JSON）。"""
        payload = json.dumps(self.data, ensure_ascii=False, default=str)
        return f"event: {self.event}\ndata: {payload}\n\n"
