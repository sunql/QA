"""数据质量规则自动生成 API（dq-rule-auto-generation Task 4）。

POST /api/v1/data-quality/rules/generate/preview   预览规则建议
POST /api/v1/data-quality/rules/generate/confirm   确认并写入（Task 5）
POST /api/v1/data-quality/rules/generate/apply-suggestion  直接采纳单条（Task 6）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import (
    GenerateConfirmRequest,
    GenerateConfirmResponse,
    GeneratePreviewRequest,
    GeneratePreviewResponse,
)
from app.services.data_quality_rule_generate_service import (
    DataQualityRuleGenerateService,
)

router = APIRouter(prefix="", tags=["data-quality-generate"])


@router.post("/preview", response_model=GeneratePreviewResponse)
async def previewRules(
    payload: GeneratePreviewRequest,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> GeneratePreviewResponse:
    """预览指定本体类的数据质量规则建议。

    返回该类可推导的全部规则建议（NEW/EXISTS 状态），以及因 schema 映射问题被阻断的属性列表。
    """
    return await DataQualityRuleGenerateService().preview(
        session, classId=payload.class_id, datasourceId=payload.datasource_id,
    )


@router.post("/confirm", response_model=GenerateConfirmResponse)
async def confirmRules(
    payload: GenerateConfirmRequest,
    user: CurrentUser = Depends(getCurrentUser),
    session: AsyncSession = Depends(getDb),
) -> GenerateConfirmResponse:
    """批量确认并写入自动生成的数据质量规则。

    幂等写入：rule_code 已存在 → 记入 skippedCodes；并发撞唯一约束同理。
    每条创建成功的规则会写入一条 audit_outbox 审计事件。
    """
    return await DataQualityRuleGenerateService().confirm(
        session, payload=payload, actor=user,
    )
