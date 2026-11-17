"""P5 工作流 / 审批 REST DTO。

约定与仓内其余 DTO 一致：
- Python 字段 ``snake_case``，JSON 走 ``camelCase``（``CamelModel`` 的 alias_generator）；
- **请求模型一律 ``extra="forbid"``** ⇒ 未知字段在边界被拒，不静默忽略；
- 数值下界在 DTO 层约束，上限由服务层按配置判定（不重复抄一遍）。

⚠️ 审批面是**写入口**（P5 唯一的写面之一）：DTO 的 ``extra="forbid"`` 与
长度上限是防止「多传一个字段就悄悄生效」的第一道闸。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator

from app.domain.schemas import CamelModel

# 长度上限对齐库层列宽。
DOMAIN_CODE_MAX_LENGTH = 64
CAPABILITY_CODE_MAX_LENGTH = 64
VERSION_MAX_LENGTH = 32
IDEMPOTENCY_KEY_MAX_LENGTH = 128
DECISION_NOTE_MAX_LENGTH = 2000

# 请求体体积上限（JSON 序列化字节数）：审批面是写入口 ⇒ 载荷必须有闸。
MAX_REQUEST_BYTES = 64 * 1024


class WorkflowInstanceCreate(CamelModel):
    """``POST /api/v1/workflow/instances`` 请求体（提交一个工作流实例）。

    **不含 riskLevel**：风险级别由定义（服务端）绑定 —— 客户端自选就等于
    自选审批层级（详见 `app/services/workflow_definition.py`）。
    """

    model_config = ConfigDict(extra="forbid")

    domain_code: str = Field(min_length=1, max_length=DOMAIN_CODE_MAX_LENGTH)
    capability_code: str = Field(min_length=1, max_length=CAPABILITY_CODE_MAX_LENGTH)
    version: str = Field(min_length=1, max_length=VERSION_MAX_LENGTH)
    idempotency_key: str = Field(min_length=1, max_length=IDEMPOTENCY_KEY_MAX_LENGTH)
    request: dict[str, Any] = Field(default_factory=dict)

    @field_validator("request")
    @classmethod
    def _capRequestSize(cls, value: dict[str, Any]) -> dict[str, Any]:
        """限制请求体体积：超限的载荷在**边界**被拒，不进库。"""

        try:
            encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("request 必须是可 JSON 序列化的对象") from exc
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError(f"request 体积超限（上限 {MAX_REQUEST_BYTES} 字节）")
        return value


class WorkflowInstanceRead(CamelModel):
    """工作流实例摘要。"""

    id: int
    definition_id: int
    status: str
    idempotency_key: str
    business_key: str | None = None
    created_by: str | None = None
    error_message: str | None = None
    created_time: datetime
    updated_time: datetime


class WorkflowStepRead(CamelModel):
    """工作流步骤。"""

    id: int
    step_code: str
    step_order: int
    status: str
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WorkflowApprovalRead(CamelModel):
    """审批任务。

    ``approver_user`` = **实际审批人（签署人）**；待审批时为空
    （「谁有资格批」由定义体的审批矩阵决定，不落任务行）。
    ``created_by`` = 本轮审批的发起人（四眼原则的第二个比对对象）。
    """

    id: int
    instance_id: int
    matrix_code: str
    subject_type: str
    subject_id: str
    status: str
    approver_role: str | None = None
    approver_user: str | None = None
    decision_note: str | None = None
    due_at: datetime | None = None
    decided_at: datetime | None = None
    created_by: str | None = None


class WorkflowInstanceDetail(CamelModel):
    """实例详情（实例 + 步骤 + 审批任务）。"""

    instance: WorkflowInstanceRead
    steps: list[WorkflowStepRead]
    approvals: list[WorkflowApprovalRead]


class WorkflowApprovalRequest(CamelModel):
    """``POST /instances/{instanceId}/approval-request`` 请求体。

    **刻意没有任何字段**：风险级别由定义（服务端）绑定，客户端不得自选。
    保留 body 形位是为了让「多传一个字段」被 ``extra="forbid"`` 拒成 422，
    而不是被静默忽略（静默忽略 = 客户端以为生效了）。
    请求体传空对象 ``{}`` 即可。
    """

    model_config = ConfigDict(extra="forbid")


class WorkflowApprovalDecision(CamelModel):
    """``POST /approvals/{taskId}/decision`` 请求体。"""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVE", "REJECT"]
    note: str | None = Field(default=None, max_length=DECISION_NOTE_MAX_LENGTH)


class WorkflowInstanceCancel(CamelModel):
    """``POST /instances/{instanceId}/cancel`` 请求体。

    ``reason`` 落 ``workflow_instance.error_message`` 与治理审计 ⇒ 取消
    也会留痕（「谁在什么时候为什么停了这件事」）。
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=DECISION_NOTE_MAX_LENGTH)
