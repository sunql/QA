"""研究型入口 REST DTO（feat-research-entry Task 7）。

独立小文件：1700 行的 ``schemas.py`` 不再追加（Task 7 brief 明示）。
契约约定：
- 字段 snake_case、JSON camelCase（``CamelModel`` 的 alias_generator 统一负责）；
- 请求体 ``extra="forbid"``（与 agent-tool-binding DTO 同款边界拒绝）；
- 枚举一律 Literal，**在 Pydantic 边界**拦截非法值（Task 3 Minor#3 / Task 6 F5）：
  role / phase / mode / status / action 非法即 422，不再拖到状态机内部炸。

关于 ``phase``：设计 §4.4 的固定点是 intent / planning / hypothesis，
``runtime_dynamic`` 是设计 §4.8 的动态点；Task 5 实现额外落了第二个动态相位
``low_confidence_step``（步失败 / 空数据）。故此处列**全部 5 个真实取值** ——
只列 4 个会让该相位的 checkpoint 在序列化时 422/500（真实数据打脸枚举）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field

from app.domain.schemas import CamelModel

# --- 枚举（与 DB 白名单 / 状态机词汇逐字一致）-------------------------------
ResearchMode = Literal["research", "attribution", "compare"]
"""会话研究模式（设计 §4.7 三 mode；ReportPlanner 的 ``_MODE_SECTIONS`` 同集合）。"""

TurnRole = Literal["user", "agent", "checkpoint_awaiting"]
"""轮次角色（Task 3 裁定；agent 为后续「研究结论」轮预留）。"""

CheckpointPhase = Literal[
    "intent", "planning", "hypothesis", "runtime_dynamic", "low_confidence_step"
]
"""检查点相位（3 固定 + 2 动态，见模块 docstring）。"""

SessionStatus = Literal["running", "awaiting_user", "done", "failed", "aborted"]
"""会话状态（Task 3 白名单，注意是 done 不是 completed）。"""

CheckpointStatus = Literal["pending", "confirmed", "modified", "rejected"]
"""检查点状态（pending = 待决策；其余为已决策终态）。"""

ReportStatus = Literal["published", "superseded"]
"""报告版本状态（每会话至多 1 个 published）。"""

CheckpointAction = Literal["confirm", "modify", "reject"]
"""用户决策动作（→ confirmed / modified / rejected，见 ports.ACTION_STATUS）。"""


# --- 请求体 -----------------------------------------------------------------


class ResearchSessionCreate(CamelModel):
    """POST /research/sessions 请求体。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    mode: ResearchMode = "research"
    datasourceId: int | None = None
    """业务数据源（可选，Task 13e）。不传 → 落默认数据源（无可用源则显式报错）。

    可选是**刻意**的：前端无需改动；缺省回落规则与 chat 同口径（默认源优先）。
    """
    modelId: int | None = None
    """LLM 模型配置（可选，W5）。不传 → None = 自动路由（行为与今天完全一致）。

    显式指定时**必须存在且启用**，否则 404（不静默回落自动路由 —— 否则用户以为
    用了 A 实际用了 B）。会话级落库：研究多轮可恢复，追问 / resume 沿用同一模型。
    """


class ResearchTurnCreate(CamelModel):
    """POST /research/sessions/{sessionId}/turns 请求体。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


class CheckpointAnswerRequest(CamelModel):
    """POST /research/checkpoints/{checkpointId}/answer 请求体。

    ``choice`` 是自由 JSONB（不同相位载荷不同：三臂选择 / 计划反馈 /
    假设下标 / 改写问题），故不做结构约束，只要求是对象。
    """

    model_config = ConfigDict(extra="forbid")

    action: CheckpointAction
    choice: dict[str, Any] = Field(default_factory=dict)


# --- 响应体 -----------------------------------------------------------------


class ResearchSessionRead(CamelModel):
    """会话摘要（列表与创建响应用）。"""

    id: uuid.UUID
    title: str
    mode: ResearchMode
    status: SessionStatus
    question: str
    """原始问题（ORM ``input_seed``；重启恢复意图的唯一来源）。"""
    datasourceId: int | None = None
    """本研究跑在哪个业务数据源上（ORM ``datasource_id``，Task 13e 落库、W4 回显）。"""
    modelId: int | None = None
    """本研究用哪个 LLM 模型配置（ORM ``model_id``，W5 落库/回显）；None = 自动路由。"""
    createdAt: datetime
    updatedAt: datetime


class ResearchTurnRead(CamelModel):
    """单轮交互。"""

    id: uuid.UUID
    turnIndex: int
    role: TurnRole
    content: dict[str, Any]
    createdAt: datetime


class ResearchCheckpointRead(CamelModel):
    """待决策检查点。

    ``prompt`` **无独立列**：由调用方从 ``options["prompt"]`` 派生（Task 3 裁定 #1），
    顶层下发是为了对齐设计 §4.5 的 SSE payload 形状与前端 CheckpointCard。
    """

    id: uuid.UUID
    phase: CheckpointPhase
    status: CheckpointStatus
    options: dict[str, Any]
    prompt: str = ""
    userChoice: dict[str, Any] | None = None
    decidedAt: datetime | None = None


class ResearchSessionDetail(CamelModel):
    """GET /research/sessions/{sessionId} 响应：会话 + 全部轮次 + 待决策点。"""

    session: ResearchSessionRead
    turns: list[ResearchTurnRead]
    pendingCheckpoint: ResearchCheckpointRead | None = None


class ResearchTurnAccepted(CamelModel):
    """POST .../turns 的 202 响应（状态机异步跑，进度走 SSE）。"""

    sessionId: uuid.UUID
    turnId: uuid.UUID
    status: SessionStatus


class CheckpointAnswerRead(CamelModel):
    """决策提交后的会话走向（状态机在后台续跑）。"""

    sessionStatus: SessionStatus
    nextPhase: str


class ResearchReportRead(CamelModel):
    """报告详情（含 JSONB payload 与渲染后的 Markdown）。"""

    id: uuid.UUID
    version: int
    status: ReportStatus
    payload: dict[str, Any]
    renderedMd: str
    createdAt: datetime


class ResearchReportSummaryRead(CamelModel):
    """版本列表条目（不携带 payload / Markdown，避免列表响应膨胀）。"""

    id: uuid.UUID
    version: int
    status: ReportStatus
    createdAt: datetime
