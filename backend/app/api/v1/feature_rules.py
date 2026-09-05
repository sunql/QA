"""FeatureRule 管理 API（feat-feature-rule-config）。

挂在 /api/v1/feature-rules：
  GET    /api/v1/feature-rules                 列表（enabledOnly 过滤）
  GET    /api/v1/feature-rules/{code}          详情
  POST   /api/v1/feature-rules                 创建（admin only）
  PUT    /api/v1/feature-rules/{code}          更新（admin only, optimistic lock）
  DELETE /api/v1/feature-rules/{code}          删除（admin only, 409 if referenced）
  POST   /api/v1/feature-rules/{code}/toggle   翻转 enabled（admin only）
  POST   /api/v1/feature-rules/parse-description  LLM NL → suggestions（admin only）

ACL：读 - 所有登录用户；写 - 仅 admin。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getAdminOnlyActor, getCurrentUser, getDb
from app.domain.models import FeatureRule
from app.domain.schemas import (
    FeatureRuleCreate,
    FeatureRuleParseDescriptionRequest,
    FeatureRuleParseDescriptionResponse,
    FeatureRuleRead,
    FeatureRuleUpdate,
)
from app.infrastructure.llm.factory import createClient
from app.services.feature_rule_llm_service import parseFeatureRuleDescription
from app.services.feature_rule_registry import feature_rule_registry
from app.services.feature_rule_service import FeatureRuleService

router = APIRouter(prefix="/api/v1/feature-rules", tags=["feature-rules"])


def _svc() -> FeatureRuleService:
    return FeatureRuleService()


@router.get("", response_model=list[FeatureRuleRead])
async def listFeatureRules(
    enabledOnly: bool = False,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> list[FeatureRuleRead]:
    stmt = select(FeatureRule).options(selectinload(FeatureRule.thresholds))
    if enabledOnly:
        stmt = stmt.where(FeatureRule.enabled.is_(True))
    result = await session.execute(stmt)
    rows = result.scalars().all()
    return [FeatureRuleRead.model_validate(r) for r in rows]


@router.get("/{code}", response_model=FeatureRuleRead)
async def getFeatureRule(
    code: str,
    _user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> FeatureRuleRead:
    stmt = (
        select(FeatureRule)
        .options(selectinload(FeatureRule.thresholds))
        .where(FeatureRule.code == code)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        from app.domain.exceptions import FeatureRuleNotFoundError
        raise FeatureRuleNotFoundError(f"feature_rule not found: {code}")
    return FeatureRuleRead.model_validate(row)


@router.post("", response_model=FeatureRuleRead, status_code=status.HTTP_201_CREATED)
async def createFeatureRule(
    payload: FeatureRuleCreate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> FeatureRuleRead:
    row = await _svc().createRule(session, payload, _admin)
    await session.commit()
    # 写时失效
    if row.id:
        await feature_rule_registry.reloadOne(session, row.id)
    return FeatureRuleRead.model_validate(row)


@router.put("/{code}", response_model=FeatureRuleRead)
async def updateFeatureRule(
    code: str,
    payload: FeatureRuleUpdate,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> FeatureRuleRead:
    row = await _svc().updateRule(session, code, payload, _admin)
    await session.commit()
    await feature_rule_registry.reloadOne(session, row.id)
    return FeatureRuleRead.model_validate(row)


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteFeatureRule(
    code: str,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> None:
    row = await _svc().getRule(session, code)
    await _svc().deleteRule(session, code, _admin)
    await session.commit()
    feature_rule_registry.invalidate(row.id)


@router.post("/{code}/toggle", response_model=FeatureRuleRead)
async def toggleFeatureRule(
    code: str,
    enabled: bool = Body(..., embed=True),
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> FeatureRuleRead:
    row = await _svc().toggleEnabled(session, code, enabled, _admin)
    await session.commit()
    await feature_rule_registry.reloadOne(session, row.id)
    return FeatureRuleRead.model_validate(row)


@router.post("/parse-description", response_model=FeatureRuleParseDescriptionResponse)
async def parseDescription(
    payload: FeatureRuleParseDescriptionRequest,
    _admin: CurrentUser = Depends(getAdminOnlyActor),
    session: AsyncSession = Depends(getDb),
) -> FeatureRuleParseDescriptionResponse:
    """LLM 解析自然语言策略 → 建议阈值（advisory，不持久化）。"""
    llm_client = createClient(None)
    return await parseFeatureRuleDescription(
        session, payload, llm_client, actor=_admin.userId
    )
