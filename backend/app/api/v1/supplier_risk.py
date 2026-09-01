"""供应商风险 Agent REST API（Phase 5.4 Supplier Risk Agent）。

挂在 /api/v1/supplier-risk：按 supplier_key 实时评估供应商风险等级
（主路径 RISK_SCORE + fallback 3-feature 违规计数），返回等级 +
主要风险点（LLM 生成 / 模板降级）+ 推荐动作 + 4 个特征贡献。

Phase 6.x：supplier_key 同时接受 VARCHAR 业务码与 BIGINT 代理键，
service 内部 _resolveSupplier 双路解析。

鉴权策略与 supplier-360 一致：仅 `getCurrentUser`（不强制 owner-based ACL），
理由：
- 底层 entity_mapping / feature_value 各自已有 ACL
- 该接口不存可变更状态；强行加 ACL 会暴露「该 supplier_key 是否存在」
  （与 Phase 4.5 ACL 治理「NotFoundError 通用消息、无 owner/code 泄漏」一致）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import SupplierRiskRead
from app.services.supplier_risk_service import SupplierRiskService

router = APIRouter()
_service = SupplierRiskService()


def getSupplierRiskService() -> SupplierRiskService:
    """service 无状态依赖；保留工厂风格便于测试 monkeypatch。"""
    return _service


@router.get(
    "/{supplier_key}",
    response_model=SupplierRiskRead,
    status_code=status.HTTP_200_OK,
    summary="供应商风险评估（按 supplier_key 实时聚合）",
)
async def getSupplierRisk(
    supplier_key: str,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    service: SupplierRiskService = Depends(getSupplierRiskService),
) -> SupplierRiskRead:
    """按 supplier_key 取供应商风险等级 + LLM 风险点 + 推荐动作。

    supplier_key 同时接受 VARCHAR 业务码与 BIGINT 代理键（service 双路解析）。

    不存在 → 404（防 typo 静默）；LLM 不可用 → 自动降级到 fallback_template
    （不影响主响应，log warn）。
    """
    return await service.assess(db, supplier_key)