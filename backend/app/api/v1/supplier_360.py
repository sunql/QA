"""供应商 360° ADS 视图 REST API（Phase 5.3）。

挂在 /api/v1/supplier-360：按 enterprise_key 聚合 supplier 主数据 +
跨系统编码 + 4 个 SUPPLIER KPI 最新值。

鉴权策略：仅需 `getCurrentUser` 鉴权（不强制 owner-based ACL）。
理由：supplier-360 是**只读聚合视图**，源表（entity_mapping / feature_value）
各自已有 ACL；supplier-360 视图自身不存可变更状态，强行加 ACL 反而
会暴露「该 supplier_key 是否存在」的侧信道（与 Phase 4.5 ACL 治理
「403 通用消息、无 owner/code 泄漏」原则一致）。

后续若需做 supplier 级 ACL（按部门限制可见 supplier 列表），应：
1. 在 entity_mapping 加 `data_classification` 字段
2. service 层加 `actor.canSeeSupplier(supplier_key)` 校验
3. 仍保持 NotFoundError 通用消息（避免泄漏）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import Supplier360Read
from app.services.supplier_360_service import Supplier360Service

router = APIRouter()
_service = Supplier360Service()


def getSupplier360Service() -> Supplier360Service:
    """service 无状态依赖；保留工厂风格便于测试 monkeypatch。"""
    return _service


@router.get(
    "/{supplier_key}",
    response_model=Supplier360Read,
    status_code=status.HTTP_200_OK,
    summary="供应商 360° 视图（按 enterprise_key 聚合）",
)
async def getSupplier360(
    supplier_key: int,
    _user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
    service: Supplier360Service = Depends(getSupplier360Service),
) -> Supplier360Read:
    """按 enterprise_key 取供应商 360° 数据。

    不存在 → 404（防 typo 静默）；子模块失败 → 整体仍 200 + 该字段空值（plan §5.3）。
    """
    return await service.get360(db, supplier_key)
