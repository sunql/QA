"""评估报告分享 service（feat-dq-evaluation-report，Phase 8b）。

分享链路：
  1. owner/admin 调 ``create_share(report_id, expires_in_days)`` → 新建一行
     ``EvaluationReportShare``（UUID 4 token）；返回 ``{share_token, share_url,
     expires_at, ...}``。
  2. 任何（匿名）用户调 ``resolve_share(token)`` → 校验未过期、未被软删，
     access_count++，返回对应 ``EvaluationReportRead``。
  3. owner/admin 调 ``revoke_share(share_id)`` → 设 ``deleted_at = now()``
     （注：本表当前 schema 没有 deleted_at 列，按 share_token 仍存在但
     ``resolve_share`` 在 record.deleted_at IS NULL 时返 410 即可。
     为兼容现状，最小实现：``revoke_share`` 物理删行，``resolve_share``
     找不到行即 410 Gone。）

设计权衡：物理删更简单；如果将来要审计「谁撤销过哪个 share」，再加
deleted_at 列。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError
from app.domain.models import EvaluationReport, EvaluationReportShare
from app.domain.schemas import EvaluationReportShareRead
from app.dependencies import CurrentUser
from app.services.evaluation_report_service import (
    EvaluationReportService,
    _reportToRead,
)
from app.services.acl_service import AclService


def _build_share_url(share_token: str) -> str:
    """前端公共分享页路径（与 frontend DataQualityReportPublicSharePage 对齐）。"""
    return f"/data-quality/reports/share/{share_token}"


class EvaluationReportShareService:
    """评估报告分享 CRUD + 解析（仅凭 token 鉴权）。"""

    async def create_share(
        self,
        session: AsyncSession,
        *,
        report_id: int,
        expires_in_days: int,
        actor: CurrentUser,
    ) -> EvaluationReportShareRead:
        """创建分享。ACL：report.owner ∈ actor.departments 或 actor 含 admin。"""
        # 1. 取 report 校验存在 + ACL
        report = await session.get(EvaluationReport, report_id)
        if report is None or report.deleted_at is not None:
            raise NotFoundError(message=f"report {report_id} not found")
        AclService().assertCanModify(
            user=actor,
            entity_owner=report.owner,
            entity_label="EvaluationReport",
            entity_code=str(report.id),
        )

        # 2. 生成 token + expires_at
        token = uuid.uuid4()
        expires_at = datetime.now(UTC) + timedelta(days=expires_in_days)
        share = EvaluationReportShare(
            report_id=report_id,
            share_token=token,
            expires_at=expires_at,
            access_count=0,
            created_by=actor.userId,
        )
        session.add(share)
        await session.flush()
        await session.commit()

        return EvaluationReportShareRead(
            id=share.id,
            report_id=report_id,
            share_token=str(token),
            share_url=_build_share_url(str(token)),
            expires_at=expires_at,
            access_count=0,
            created_by=actor.userId,
            created_at=share.created_at or datetime.now(UTC),
        )

    async def list_shares(
        self,
        session: AsyncSession,
        *,
        report_id: int,
    ) -> list[EvaluationReportShareRead]:
        """某报告的所有现存 share（未过期）。"""
        now = datetime.now(UTC)
        stmt = (
            select(EvaluationReportShare)
            .where(
                EvaluationReportShare.report_id == report_id,
                EvaluationReportShare.expires_at > now,
            )
            .order_by(EvaluationReportShare.created_at.desc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            EvaluationReportShareRead(
                id=r.id,
                report_id=r.report_id,
                share_token=str(r.share_token),
                share_url=None,  # 列表视图不暴露 url，避免冗余
                expires_at=r.expires_at,
                access_count=r.access_count,
                created_by=r.created_by,
                created_at=r.created_at,
            )
            for r in rows
        ]

    async def revoke_share(
        self,
        session: AsyncSession,
        *,
        share_id: int,
        actor: CurrentUser,
    ) -> None:
        """撤销分享：物理删。ACL = 所属报告的 owner/admin。"""
        share = await session.get(EvaluationReportShare, share_id)
        if share is None:
            raise NotFoundError(message=f"share {share_id} not found")
        report = await session.get(EvaluationReport, share.report_id)
        if report is None or report.deleted_at is not None:
            raise NotFoundError(message=f"report {share.report_id} not found")
        AclService().assertCanModify(
            user=actor,
            entity_owner=report.owner,
            entity_label="EvaluationReport",
            entity_code=str(report.id),
        )
        await session.execute(
            delete(EvaluationReportShare).where(
                EvaluationReportShare.id == share_id,
            )
        )
        await session.commit()

    async def resolve_share(
        self,
        session: AsyncSession,
        *,
        token: str,
    ) -> EvaluationReport:
        """按 token 解析报告。过期 / 不存在返 410 Gone。

        同时自增 access_count（audit 用途）。
        """
        # UUID 列在 PG 用 postgresql.UUID 类型，非 UUID 字符串直接抛
        # asyncpg.exceptions.DataError，浏览器拿到 500。这里 catch 后转
        # NotFoundError（与「不存在」一致），避免暴露 schema 类型。
        try:
            parsed_uuid = uuid.UUID(token)
        except (ValueError, AttributeError):
            raise NotFoundError(message=f"share token invalid or expired") from None

        now = datetime.now(UTC)
        stmt = (
            select(EvaluationReportShare)
            .where(
                EvaluationReportShare.share_token == parsed_uuid,
                EvaluationReportShare.expires_at > now,
            )
        )
        stmt = (
            select(EvaluationReportShare)
            .where(
                EvaluationReportShare.share_token == token,
                EvaluationReportShare.expires_at > now,
            )
        )
        share = (await session.execute(stmt)).scalars().first()
        if share is None:
            raise NotFoundError(message=f"share token invalid or expired")

        report = await session.get(EvaluationReport, share.report_id)
        if report is None or report.deleted_at is not None:
            raise NotFoundError(message=f"underlying report gone")

        # access_count++ (UPDATE 不需要 commit 就能立刻可见于同 session)
        await session.execute(
            update(EvaluationReportShare)
            .where(EvaluationReportShare.id == share.id)
            .values(access_count=EvaluationReportShare.access_count + 1)
        )
        await session.commit()
        return report

    async def resolve_share_to_read(
        self,
        session: AsyncSession,
        *,
        token: str,
    ):
        """``resolve_share`` + 转 EvaluationReportRead（公开分享页用）。"""
        report = await self.resolve_share(session, token=token)
        return _reportToRead(report)


def get_evaluation_report_share_service() -> EvaluationReportShareService:
    return EvaluationReportShareService()