"""P5 工作流定义的**发布契约**（生效版本唯一 + 客户端不得自选版本）。

为什么需要这个文件
------------------

审批层级写在**定义体**的审批矩阵里 ⇒「哪一版定义生效」是**安全边界**，不是
版本管理细节：

- 若同一域能力下存在**两条** ``PUBLISHED`` 定义，调用方就能挑一条「审批矩阵
  更宽松」的来跑 ⇒ **绕过高层审批**；
- 若生效版本可由**客户端自选**，上面那条路就必然可达。

⇒ 两条硬约束：

1. **库层**：同一 ``(domain_code, capability_code)`` 至多一条 ``PUBLISHED``
   （部分唯一索引 ``uq_workflow_definition_published``）；
2. **服务层**：生效版本由服务端按域能力唯一确定；客户端指定的版本与之不符 ⇒ 409。

⚠️ 本文件跑在**真实 PostgreSQL** 上（unit 目录 + 真实 PG，见
``app/tests/unit/test_workflow_approval_api.py`` 的说明：unit 分片按路径哈希，
新建 unit 文件不影响任何既有文件的分片归属）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.workflow_models import WorkflowDefinition
from app.services.workflow_service import (
    DETAIL_WORKFLOW_DEFINITION_NOT_PUBLISHED,
    DETAIL_WORKFLOW_DEFINITION_VERSION_MISMATCH,
    WorkflowService,
)

_DOMAIN = "procurement"
_CAPABILITY = "po_publish_contract"


def _definitionJson() -> dict:
    return {
        "riskLevel": "MEDIUM",
        "steps": [{"code": "write", "order": 0, "commandType": "po.write"}],
    }


async def _seedDefinition(dbSession, *, version: str, status: str) -> WorkflowDefinition:
    row = WorkflowDefinition(
        domain_code=_DOMAIN,
        capability_code=_CAPABILITY,
        version=version,
        definition_json=_definitionJson(),
        status=status,
        created_by="tester",
    )
    dbSession.add(row)
    await dbSession.commit()
    return row


async def test_同一域能力下两条生效定义被库层拒绝(dbSession):
    """两条 ``PUBLISHED`` ⇒ IntegrityError（部分唯一索引生效）。"""

    await _seedDefinition(dbSession, version="v1", status="PUBLISHED")

    dbSession.add(
        WorkflowDefinition(
            domain_code=_DOMAIN,
            capability_code=_CAPABILITY,
            version="v2",
            definition_json=_definitionJson(),
            status="PUBLISHED",
            created_by="tester",
        )
    )
    with pytest.raises(IntegrityError):
        await dbSession.commit()
    await dbSession.rollback()


async def test_草稿与生效定义可共存(dbSession):
    """``DRAFT`` 不受「生效版本唯一」约束（可以同时存在一份草稿）。"""

    await _seedDefinition(dbSession, version="v1", status="PUBLISHED")
    draft = await _seedDefinition(dbSession, version="v2", status="DRAFT")

    assert draft.id is not None


async def test_提交时版本不是生效版本返回409(dbSession):
    """客户端指定了**非生效版本** ⇒ 409（不得自选版本 ⇒ 不得挑宽松审批矩阵）。"""

    await _seedDefinition(dbSession, version="v1", status="PUBLISHED")

    with pytest.raises(ConflictError) as excInfo:
        await WorkflowService().submitInstance(
            dbSession,
            domainCode=_DOMAIN,
            capabilityCode=_CAPABILITY,
            version="v0",
            idempotencyKey="k-1",
            requestJson={},
            actor="submitter",
        )

    assert excInfo.value.detail == DETAIL_WORKFLOW_DEFINITION_VERSION_MISMATCH


async def test_提交时定义不存在返回404(dbSession):
    """定义压根不存在 ⇒ 404（**不是** 500 —— 模板占位符对不上会变成 500）。"""

    with pytest.raises(NotFoundError):
        await WorkflowService().submitInstance(
            dbSession,
            domainCode=_DOMAIN,
            capabilityCode=_CAPABILITY,
            version="v404",
            idempotencyKey="k-2",
            requestJson={},
            actor="submitter",
        )


async def test_提交时定义存在但未发布返回409(dbSession):
    """版本存在但**未生效**（且该域能力无生效版本）⇒ 409。"""

    await _seedDefinition(dbSession, version="v1", status="DRAFT")

    with pytest.raises(ConflictError) as excInfo:
        await WorkflowService().submitInstance(
            dbSession,
            domainCode=_DOMAIN,
            capabilityCode=_CAPABILITY,
            version="v1",
            idempotencyKey="k-3",
            requestJson={},
            actor="submitter",
        )

    assert excInfo.value.detail == DETAIL_WORKFLOW_DEFINITION_NOT_PUBLISHED


async def test_提交时版本为生效版本则成功(dbSession):
    """生效版本照常提交（正常路径不被上面的约束误伤）。"""

    await _seedDefinition(dbSession, version="v1", status="PUBLISHED")

    instance, created = await WorkflowService().submitInstance(
        dbSession,
        domainCode=_DOMAIN,
        capabilityCode=_CAPABILITY,
        version="v1",
        idempotencyKey="k-4",
        requestJson={},
        actor="submitter",
    )

    assert created is True
    assert instance.status in {"PENDING", "RUNNING"}
