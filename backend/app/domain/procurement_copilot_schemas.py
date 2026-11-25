"""I3-a 采购 Copilot 统一对话入口的 DTO（设计稿 §8.4）。

设计稿对 `POST /procurement/copilot/messages` 只给了语义（「统一对话入口；
返回 SSE 或任务引用」），没有规定 DTO。这里按**已存在的编排契约**收口：

- 运行体 = `agent-runs` 的 `AgentRunCreate`（agentCode + input + modelConfigId
  + limits），Copilot 不新造执行语义，也不放宽只读白名单；
- `mode` 显式二选一：`stream`（SSE）/ `task`（202 + 任务引用）；
- **不设**「后端替你选模型」的默认值：`modelConfigId` 必填（见
  `qa-system-no-model-override`：后端自动改派一律禁止）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from app.domain.agent_orchestration_schemas import AgentRunLimits
from app.domain.schemas import CamelModel

#: Copilot 单条消息上限（与聊天入口同量级；超长问题应先拆解）。
COPILOT_MESSAGE_MAX_LENGTH = 2000
#: 采购域 Agent code 前缀（目录口径与 Copilot 入口同源）。
PROCUREMENT_AGENT_PREFIX = "PROCUREMENT_"


class CopilotMessageRequest(CamelModel):
    """`POST /api/v1/procurement/copilot/messages` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    #: 可为空：场景分支（scenarioCode + slots）下 chips 点击没有文字，
    #: 主句由模板提供；自由文本分支的空消息在服务层 fail loud。
    message: str = Field(default="", max_length=COPILOT_MESSAGE_MAX_LENGTH)
    agent_code: str = Field(min_length=1, max_length=64)
    model_config_id: int = Field(ge=1)
    #: 场景编码（P4）：与 ``slots`` 同时提供 ⇒ 后端按场景模板渲染最终提问
    #: （冲突让位在服务端）。**续跑回复（回答追问）不带 slots** ⇒ 走自由文本。
    scenario_code: str | None = Field(default=None, max_length=64)
    #: 结构化槽位值（键 = slot_schema[].key；值为标量或数组）。
    slots: dict[str, Any] | None = None
    limits: AgentRunLimits | None = None
    #: stream = SSE 过程流；task = 202 + 任务引用（调用方自己轮询轨迹）。
    mode: Literal["stream", "task"] = "stream"
    #: 多轮会话 id（缺省 = 新开一个会话；响应里会回传新 id）。
    conversation_id: str | None = Field(default=None, max_length=36)


class CopilotTaskRead(CamelModel):
    """`mode=task` 的响应：任务引用（**不含**运行结果）。

    结果与轨迹走既有 `GET /agent-runs/{runId}` / `GET /agent-runs/{runId}/steps`
    —— Copilot 不另造一套读接口。
    """

    run_id: str
    status: str
    steps_url: str
    conversation_id: str
    #: True = 这一轮是对上一轮追问的回答（resume 同一个 run）
    resumed: bool = False


class CopilotTurnRead(CamelModel):
    """会话中的一轮：用户原文 + 对应的运行状态/答案。"""

    seq: int
    question: str
    run_id: str
    status: str
    answer: str | None = None
    pause_code: str | None = None
    created_at: str


class CopilotConversationRead(CamelModel):
    """会话详情（只有创建者能读）。"""

    conversation_id: str
    agent_code: str
    model_config_id: int
    turns: tuple[CopilotTurnRead, ...] = ()


class CopilotConversationSummary(CamelModel):
    """会话列表项：侧栏「历史会话」用，不返回全部轮次正文。"""

    conversation_id: str
    agent_code: str
    model_config_id: int
    turn_count: int
    #: 最后一轮的用户问题（预览用；没有轮次时为 None）
    last_question: str | None = None
    updated_at: str
    #: 置顶时间（P4 阶段5：跨设备同步；None = 未置顶）
    pinned_at: str | None = None


class CopilotPinRequest(CamelModel):
    """`POST /procurement/copilot/conversations/{id}/pin` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    pinned: bool


class CopilotScenarioSlotRead(CamelModel):
    """场景槽位定义（P4）：前端槽位表单按此动态生成。"""

    key: str
    type: str
    label: str
    required: bool = False
    conflict_keywords: list[str] = Field(default_factory=list)
    #: 编码让位模式（lookaround 正则；None = 无编码启发式）
    code_pattern: str | None = None
    #: 尾注显示格式（可带 {value} 占位，如「近{value}个月」）
    display_template: str | None = None


class CopilotScenarioRead(CamelModel):
    """`GET /api/v1/procurement/copilot/scenarios` 响应项：卡片墙数据源。"""

    scenario_code: str
    name: str
    description: str
    icon_code: str
    agent_code: str
    slot_schema: list[CopilotScenarioSlotRead]
    question_template: str
    chips: list[dict[str, Any]]
    result_renderer: str
    required_role: str | None = None
