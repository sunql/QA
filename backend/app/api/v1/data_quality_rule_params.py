"""DQ 规则结构化参数 API（feat-dq-rule-params v1 隔离 router）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schemas_dq_rule_params import (
    DataQualityRuleParamsCreate, DataQualityRuleParamsRead,
    DataQualityRuleParamsUpdate, DqRuleNextCodeRead,
)
from app.infrastructure.database import getDb
from app.services.data_quality_rule_params_service import (
    DataQualityRuleParamsService,
)

router = APIRouter(prefix="/dq-rule-params/rules", tags=["dq-rule-params"])


def _service(session: AsyncSession = Depends(getDb)) -> DataQualityRuleParamsService:
    return DataQualityRuleParamsService(session)


@router.get("", response_model=list[DataQualityRuleParamsRead])
async def listRules(
    datasource_id: int | None = Query(default=None),
    svc: DataQualityRuleParamsService = Depends(_service),
) -> list[DataQualityRuleParamsRead]:
    return await svc.list(datasource_id=datasource_id)


# 注意：必须注册在 /{rule_id} 之前。新建规则弹窗用：自动生成 DQ-Rule-{date}-{10位流水} 编码。
@router.get("/next-code", response_model=DqRuleNextCodeRead)
async def nextCode(
    date: str | None = Query(default=None, max_length=8),
    svc: DataQualityRuleParamsService = Depends(_service),
) -> DqRuleNextCodeRead:
    """返回规则编码建议：`DQ-Rule-{YYYYMMDD}-{10位流水}`（只读预览，不锁定）。"""
    result = await svc.nextCode(date_yyyymmdd=date)
    return DqRuleNextCodeRead(code=result["code"], seq=result["seq"])


@router.get("/{rule_id}", response_model=DataQualityRuleParamsRead)
async def getRule(
    rule_id: int,
    svc: DataQualityRuleParamsService = Depends(_service),
) -> DataQualityRuleParamsRead:
    return await svc.get(rule_id)


@router.post("", response_model=DataQualityRuleParamsRead, status_code=status.HTTP_201_CREATED)
async def createRule(
    dto: DataQualityRuleParamsCreate,
    svc: DataQualityRuleParamsService = Depends(_service),
) -> DataQualityRuleParamsRead:
    return await svc.create(dto)


@router.put("/{rule_id}", response_model=DataQualityRuleParamsRead)
async def updateRule(
    rule_id: int,
    dto: DataQualityRuleParamsUpdate,
    svc: DataQualityRuleParamsService = Depends(_service),
) -> DataQualityRuleParamsRead:
    return await svc.update(rule_id, dto)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteRule(
    rule_id: int,
    svc: DataQualityRuleParamsService = Depends(_service),
) -> None:
    await svc.delete(rule_id)
