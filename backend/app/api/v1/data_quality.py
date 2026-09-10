"""数据质量规则 API 路由（Phase 1.1 + Phase 1.2 + Phase 1.3）。

Phase 1.1 基础 CRUD（router 挂在 /api/v1/data-quality/rules）：
  GET    /api/v1/data-quality/rules          列表（按 type/table/enabled 过滤）
  GET    /api/v1/data-quality/rules/{id}     详情
  POST   /api/v1/data-quality/rules          创建（201）
  PUT    /api/v1/data-quality/rules/{id}     更新
  DELETE /api/v1/data-quality/rules/{id}     软删除（204；is_enabled=false）

Phase 1.2 评估执行：
  POST   /api/v1/data-quality/rules/{id}/evaluate      单规则评估
  POST   /api/v1/data-quality/rules/evaluate-batch     批量评估（body: rule_ids）

Phase 1.3 评分（scores_router 挂在 /api/v1/data-quality/scores）：
  POST   /api/v1/data-quality/scores/compute           触发全量评估 + 聚合落库
  GET    /api/v1/data-quality/scores                   列表（含 latest / table / scoreType 过滤）
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.enums import ScoreType
from app.domain.schemas import (
    ComputeScoresResponse,
    DataQualityRuleCreate,
    DataQualityRuleRead,
    DataQualityRuleUpdate,
    DataQualityScoreRead,
    EvaluateBatchRequest,
    EvaluateBatchResponse,
    EvaluationResult,
    RuleOptionsRead,
)
from app.services.data_quality_evaluator import DataQualityEvaluatorDispatcher
from app.services.data_quality_score_service import DataQualityScoreService
from app.services.data_quality_service import DataQualityRuleService, ruleToRead

router = APIRouter(dependencies=[])
scores_router = APIRouter(dependencies=[])


def getDataQualityRuleService() -> DataQualityRuleService:
    """service 无状态依赖，直接返回新实例；保留工厂风格便于后续注入。"""
    return DataQualityRuleService()


def getDataQualityEvaluatorDispatcher() -> DataQualityEvaluatorDispatcher:
    """dispatcher 无状态依赖。"""
    return DataQualityEvaluatorDispatcher()


def getDataQualityScoreService() -> DataQualityScoreService:
    """score service 无状态依赖。"""
    return DataQualityScoreService()


@router.get("", response_model=list[DataQualityRuleRead])
async def listDataQualityRules(
    ruleType: str | None = Query(default=None, alias="ruleType"),
    targetTable: str | None = Query(default=None, alias="targetTable"),
    enabledOnly: bool | None = Query(default=None, alias="enabledOnly"),
    ruleName: str | None = Query(default=None, alias="ruleName"),
    datasourceId: int | None = Query(default=None, alias="datasourceId"),
    severity: str | None = Query(default=None, alias="severity"),
    enabled: Literal["all", "enabled", "disabled"] | None = Query(
        default=None, alias="enabled"
    ),
    session: AsyncSession = Depends(getDb),
    service: DataQualityRuleService = Depends(getDataQualityRuleService),
) -> list[DataQualityRuleRead]:
    rules = await service.listRules(
        session,
        ruleType=ruleType,
        targetTable=targetTable,
        enabledOnly=enabledOnly,
        ruleName=ruleName,
        datasourceId=datasourceId,
        severity=severity,
        enabled=enabled,
    )
    return [ruleToRead(r) for r in rules]


# 注意：必须注册在 `/{ruleId}` 之前，否则会被路径参数吃掉返回 422。
@router.get("/options", response_model=RuleOptionsRead)
async def listRuleFilterOptions(
    session: AsyncSession = Depends(getDb),
    service: DataQualityRuleService = Depends(getDataQualityRuleService),
) -> RuleOptionsRead:
    """规则列表筛选下拉的可选值（feat-dq-rule-list-filters）。"""
    return await service.listOptions(session)


@router.get("/{ruleId}", response_model=DataQualityRuleRead)
async def getDataQualityRule(
    ruleId: int,
    session: AsyncSession = Depends(getDb),
    service: DataQualityRuleService = Depends(getDataQualityRuleService),
) -> DataQualityRuleRead:
    rule = await service.getRule(session, ruleId)
    return ruleToRead(rule)


@router.post("", response_model=DataQualityRuleRead, status_code=status.HTTP_201_CREATED)
async def createDataQualityRule(
    payload: DataQualityRuleCreate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DataQualityRuleService = Depends(getDataQualityRuleService),
) -> DataQualityRuleRead:
    rule = await service.createRule(session, payload, user)
    return ruleToRead(rule)


@router.put("/{ruleId}", response_model=DataQualityRuleRead)
async def updateDataQualityRule(
    ruleId: int,
    payload: DataQualityRuleUpdate,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DataQualityRuleService = Depends(getDataQualityRuleService),
) -> DataQualityRuleRead:
    rule = await service.updateRule(session, ruleId, payload, user)
    return ruleToRead(rule)


@router.delete("/{ruleId}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteDataQualityRule(
    ruleId: int,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DataQualityRuleService = Depends(getDataQualityRuleService),
) -> None:
    await service.disableRule(session, ruleId, user)


# ===== Phase 1.2 评估执行 =====


@router.post("/{ruleId}/evaluate", response_model=EvaluationResult)
async def evaluateDataQualityRule(
    ruleId: int,
    session: AsyncSession = Depends(getDb),
    dispatcher: DataQualityEvaluatorDispatcher = Depends(
        getDataQualityEvaluatorDispatcher
    ),
) -> EvaluationResult:
    return await dispatcher.evaluate(session, ruleId)


@router.post("/evaluate-batch", response_model=EvaluateBatchResponse)
async def evaluateBatchDataQualityRules(
    payload: EvaluateBatchRequest,
    session: AsyncSession = Depends(getDb),
    dispatcher: DataQualityEvaluatorDispatcher = Depends(
        getDataQualityEvaluatorDispatcher
    ),
) -> EvaluateBatchResponse:
    return await dispatcher.evaluateBatch(session, payload.rule_ids)


# ===== Phase 1.3 评分（独立 router 挂在 /api/v1/data-quality/scores）=====


@scores_router.post("/compute", response_model=ComputeScoresResponse)
async def computeDataQualityScores(
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
    service: DataQualityScoreService = Depends(getDataQualityScoreService),
) -> ComputeScoresResponse:
    return await service.computeScores(
        session,
        actor=user.userId,
        actor_departments=user.departments,
    )


@scores_router.get("", response_model=list[DataQualityScoreRead])
async def listDataQualityScores(
    table: str | None = Query(default=None, alias="table"),
    scoreType: ScoreType | None = Query(default=None, alias="scoreType"),
    latest: bool = Query(default=False, alias="latest"),
    limit: int = Query(default=100, ge=1, le=1000, alias="limit"),
    session: AsyncSession = Depends(getDb),
    service: DataQualityScoreService = Depends(getDataQualityScoreService),
) -> list[DataQualityScoreRead]:
    rows = await service.listScores(
        session,
        target_table=table,
        score_type=scoreType,
        latest=latest,
        limit=limit,
    )
    return [
        DataQualityScoreRead.model_validate(r, from_attributes=True) for r in rows
    ]