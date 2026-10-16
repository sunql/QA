"""数据质量评估报告 — 公开 share 解析路由（独立 router，无 mustChangePasswordGuard）。

WHY 独立 router（2026-10-16 fix/data-quality-share-auth-policy）：
- ``/api/v1/data-quality/reports/share/{token}`` 设计意图是「匿名 token 访问」
  （分享链接场景，不要求登录）
- 之前误挂在 ``evaluation_report.router`` 下，而该 router 在 main.py 是带
  ``Depends(mustChangePasswordGuard)`` 的 ⇒ mustChangePasswordGuard 内部
  又 ``Depends(getCurrentUser)`` ⇒ 整 router 实际全鉴权
- 修法：把这条单独拎出来，放进一个无 mustChangePasswordGuard / rateLimitDep
  的 router（与 main.py:539 authRouter 同样模式 — 公开端点不带 guard）
- ``PUBLIC_ROUTES`` 白名单（test_route_auth_guard.py）里这条已声明
  「应公开」，跟代码现状不一致是 test 浮出的真 bug

不影响：
- 同一前缀下的 createShare / listShares / revokeShare（仍鉴权）
- 业务逻辑（service.resolve_share_to_read 不变）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import getDb
from app.domain.schemas import EvaluationReportRead
from app.services.evaluation_report_share_service import (
    EvaluationReportShareService,
    get_evaluation_report_share_service,
)

router = APIRouter()


@router.get(
    "/share/{token}",
    response_model=EvaluationReportRead,
)
async def resolveShare(
    token: str,
    session: AsyncSession = Depends(getDb),
    share_service: EvaluationReportShareService = Depends(
        get_evaluation_report_share_service,
    ),
) -> EvaluationReportRead:
    """按 token 公开访问（不需登录）。access_count 自增。

    见 test_public_resolve_no_auth_required / test_public_resolve_invalid_token_404
    （test_evaluation_report_share.py:241/255）。
    """
    return await share_service.resolve_share_to_read(session, token=token)
