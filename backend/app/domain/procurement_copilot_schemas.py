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

from typing import Literal

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

    message: str = Field(min_length=1, max_length=COPILOT_MESSAGE_MAX_LENGTH)
    agent_code: str = Field(min_length=1, max_length=64)
    model_config_id: int = Field(ge=1)
    limits: AgentRunLimits | None = None
    #: stream = SSE 过程流；task = 202 + 任务引用（调用方自己轮询轨迹）。
    mode: Literal["stream", "task"] = "stream"


class CopilotTaskRead(CamelModel):
    """`mode=task` 的响应：任务引用（**不含**运行结果）。

    结果与轨迹走既有 `GET /agent-runs/{runId}` / `GET /agent-runs/{runId}/steps`
    —— Copilot 不另造一套读接口。
    """

    run_id: str
    status: str
    steps_url: str
