"""数据质量规则自动生成 API（dq-rule-auto-generation Task 4-6）。

POST /api/v1/data-quality/rules/generate/preview   预览规则建议
POST /api/v1/data-quality/rules/generate/confirm   确认并写入（Task 5）
POST /api/v1/data-quality/rules/generate/parse-descriptions  LLM 解析属性描述（Task 6）
POST /api/v1/data-quality/rules/generate/apply-suggestion  采纳 LLM 建议写入元数据（Task 6）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import (
    ApplySuggestionRequest,
    ApplySuggestionResponse,
    GenerateConfirmRequest,
    GenerateConfirmResponse,
    GeneratePreviewRequest,
    GeneratePreviewResponse,
    ParseDescriptionsRequest,
    ParseDescriptionsResponse,
)
from app.infrastructure.llm.factory import createClient
from app.services.data_quality_rule_generate_service import (
    DataQualityRuleGenerateService,
)
from app.services.data_quality_rule_llm_service import parsePropertyDescriptions

router = APIRouter(prefix="", tags=["data-quality-generate"])


def _getDefaultLlmClient() -> Any:
    """获取默认 LLM 客户端（支持测试 monkeypatch）。

    优先用 OPENAI_API_KEY 环境变量；若未配置则返回 None
   （路由层捕获 None → 503 LLMUnavailableError）。
    """
    return createClient(None)


@router.post("/preview", response_model=GeneratePreviewResponse)
async def previewRules(
    payload: GeneratePreviewRequest,
    user: CurrentUser = Depends(getCurrentUser),  # noqa: B008
    session: AsyncSession = Depends(getDb),  # noqa: B008
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
    user: CurrentUser = Depends(getCurrentUser),  # noqa: B008
    session: AsyncSession = Depends(getDb),  # noqa: B008
) -> GenerateConfirmResponse:
    """批量确认并写入自动生成的数据质量规则。

    幂等写入：rule_code 已存在 → 记入 skippedCodes；并发撞唯一约束同理。
    每条创建成功的规则会写入一条 audit_outbox 审计事件。
    """
    return await DataQualityRuleGenerateService().confirm(
        session, payload=payload, actor=user,
    )


@router.post("/parse-descriptions", response_model=ParseDescriptionsResponse)
async def parseDescriptions(
    payload: ParseDescriptionsRequest,
    user: CurrentUser = Depends(getCurrentUser),  # noqa: B008
    session: AsyncSession = Depends(getDb),  # noqa: B008
) -> ParseDescriptionsResponse:
    """LLM 解析本体类所有属性的 description 字段，推导候选约束建议。

    advisory 只读，不写 outbox。LLM 失败 → 503。
    """
    # 先用 service 做 classId 存在性校验（404）
    from app.domain.models import OntologyClass
    cls = await session.get(OntologyClass, payload.class_id)
    if cls is None:
        from app.domain.exceptions import NotFoundError
        from app.services.messages_zh import MSG_DQ_GEN_CLASS_NOT_FOUND
        raise NotFoundError(MSG_DQ_GEN_CLASS_NOT_FOUND.format(id=payload.class_id))

    # model_id 有值 → 按 LlmConfig 加载指定 provider；否则走默认 env（向后兼容）。
    # createClient 在 key 缺失时返回 None（不再让 OpenAI SDK 构造期 raise → 500），
    # 统一走「未配置 LLM → 503 LLMUnavailableError」语义。
    if payload.model_id:
        from app.services.model_config_service import ModelConfigService
        config = await ModelConfigService().get(session, payload.model_id)
        llm_client = createClient(config)
    else:
        llm_client = _getDefaultLlmClient()
    if llm_client is None:
        from app.domain.exceptions import LLMUnavailableError
        raise LLMUnavailableError("未配置 LLM，无法进行 AI 辅助分析")
    return await parsePropertyDescriptions(
        session, payload=payload, llm_client=llm_client, actor=user,
    )


@router.post("/apply-suggestion", response_model=ApplySuggestionResponse)
async def applySuggestion(
    payload: ApplySuggestionRequest,
    user: CurrentUser = Depends(getCurrentUser),  # noqa: B008
    session: AsyncSession = Depends(getDb),  # noqa: B008
) -> ApplySuggestionResponse:
    """采纳 LLM 推荐的 allowed_values，写入 ontology_property 并记录 outbox 审计。

    仅值域型约束写入元数据；业务必填类建议不写回。
    值不允许含单引号（SQL 注入防护）。
    """
    return await DataQualityRuleGenerateService().applySuggestion(
        session, payload=payload, actor=user,
    )
