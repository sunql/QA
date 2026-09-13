"""P3 批量知识编译服务（WikiCompileService）。"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.wiki_compile_models import (
    TASK_STATUS_PENDING, TASK_STATUS_RUNNING, TASK_STATUS_SUCCEEDED,
    TASK_STATUS_PARTIAL, TASK_STATUS_FAILED, ITEM_STATUS_PENDING,
    ITEM_STATUS_RUNNING, ITEM_STATUS_DONE, ITEM_STATUS_FAILED,
    ITEM_STATUS_SKIPPED, COMPILE_SCOPES,
    WikiCompileTask, WikiCompileItem,
)
from app.domain.wiki_models import WikiPage
from app.services.learning.relation_discovery import RelationDiscovery
from app.services.learning.structure_suggester import StructureSuggester
from app.services.learning.conflict_detector import ConflictDetector
from app.services.learning.claim_extractor import ClaimExtractor
from app.services.wiki_page_service import WikiPageService
from app.services.learning.llm_invoker import LearningLLMInvoker as LLMInvoker

logger = logging.getLogger(__name__)

def _utcnow():
    return datetime.now(timezone.utc)

class WikiCompileService:
    async def createTask(
        self, session: AsyncSession, *, scope: str, pageIds: list[str] | None = None,
        dimension: str | None = None, modelId: int | None = None,
        fallbackModelId: int | None = None, userId: int | None = None,
    ) -> WikiCompileTask:
        if scope not in COMPILE_SCOPES:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=f"scope must be one of {list(COMPILE_SCOPES)}")
        task = WikiCompileTask(
            status=TASK_STATUS_PENDING, scope=scope,
            selected_model_id=modelId, fallback_model_id=fallbackModelId,
            created_by_user_id=userId,
        )
        session.add(task)
        await session.flush()
        page_ids: list[str] = []
        if scope == "PAGE_IDS":
            page_ids = list(pageIds) if pageIds else []
        elif scope == "DIMENSION":
            page_ids = [p async for p in WikiPageService().iterPageIds(session, dimension=dimension)]
        elif scope == "ALL":
            page_ids = [p async for p in WikiPageService().iterPageIds(session)]
        task.total_items = len(page_ids)
        await self._seedItems(session, task.id, page_ids)
        await session.commit()
        await session.refresh(task)
        return task

    async def _seedItems(self, session: AsyncSession, taskId: int, pageIds: list[str]) -> None:
        for pageId in pageIds:
            session.add(WikiCompileItem(task_id=taskId, page_id=pageId, status=ITEM_STATUS_PENDING))
        await session.flush()

    async def runTask(self, taskId: int) -> None:
        """执行编译任务。

        注意：从 ORM 对象提取所有原始值（id, selected_model_id 等）后
        再做 await，避免 commit 后属性 lazy-load 触发 greenlet 错误。
        """
        from app.infrastructure.database import getEngine
        engine = getEngine()
        async with AsyncSession(engine) as session:
            svc = WikiCompileService()
            task = await svc.getTask(session, taskId)
            if task is None:
                return

            # 提取所有需要的原始值，在 commit 之前完成，避免后续 lazy-load 问题
            tid = task.id
            selected_model_id = task.selected_model_id
            fallback_model_id = task.fallback_model_id

            task.status = TASK_STATUS_RUNNING
            task.started_time = _utcnow()
            task.heartbeat_time = _utcnow()
            await session.commit()

            pending = await svc._pendingItems(session, tid)
            if not pending:
                await svc._finish(session, tid, 0, 0, error=None)
                return
            invoker: LLMInvoker | None = None
            if selected_model_id is not None:
                invoker = svc._buildInvoker(session, selected_model_id, fallback_model_id)
                if invoker is not None:
                    invoker.bindCompileTask(tid)
            successItems = 0
            failedItems = 0
            try:
                for item in pending:
                    await svc._compileOne(session, tid, item.id, item.page_id, invoker)
                    # _compileOne 直接用 SQL 更新了计数器，要重新查询获取最新值
                    result = await session.execute(
                        select(WikiCompileTask.success_items, WikiCompileTask.failed_items)
                            .where(WikiCompileTask.id == tid)
                    )
                    row = result.one()
                    successItems = row[0]
                    failedItems = row[1]
                await svc._finish(session, tid, successItems, failedItems, error=None)
            except Exception as e:
                logger.exception("Compile task %d failed", tid)
                await svc._markFailedBestEffort(session, tid)
                # 重新查最新计数
                result = await session.execute(
                    select(WikiCompileTask.success_items, WikiCompileTask.failed_items)
                        .where(WikiCompileTask.id == tid)
                )
                row = result.one()
                await svc._finish(session, tid, row[0], row[1], error=str(e))

    async def _compileOne(
        self, session: AsyncSession, taskId: int, itemId: int, pageId: str,
        invoker: LLMInvoker | None,
    ) -> None:
        """执行单条编译。

        所有 ORM 对象在第一次 await 之前完成属性提取，避免 lazy-load
        触发 greenlet 错误。
        """
        now = _utcnow()
        # 用 SQL 直接更新 item 状态，避免 ORM 跨 await 边界
        await session.execute(
            update(WikiCompileItem)
                .where(WikiCompileItem.id == itemId)
                .values(status=ITEM_STATUS_RUNNING, attempt_count=WikiCompileItem.attempt_count + 1, started_time=now)
        )
        await session.flush()

        counts: dict[str, int] = {}
        try:
            rel_discovery = RelationDiscovery()
            rel_result = await rel_discovery.discoverForPage(session, pageId, invoker=invoker)
            counts["RELATION"] = len(rel_result.candidates)
            struct_sugg = StructureSuggester()
            struct_result = await struct_sugg.suggestForPage(session, pageId, invoker=invoker)
            counts["STRUCTURE"] = len(struct_result.suggestions)
            conflict_det = ConflictDetector()
            conflict_result = await conflict_det.detectForPage(session, pageId, invoker=invoker)
            counts["CONFLICT"] = len(conflict_result.conflicts)
            claim_ext = ClaimExtractor()
            claim_result = await claim_ext.extractForPage(session, pageId, invoker=invoker)
            counts["CLAIM"] = claim_result.claimCount

            await session.execute(
                update(WikiCompileItem)
                    .where(WikiCompileItem.id == itemId)
                    .values(status=ITEM_STATUS_DONE, mechanism_counts=counts, finished_time=_utcnow())
            )
            await session.execute(
                update(WikiCompileTask)
                    .where(WikiCompileTask.id == taskId)
                    .values(success_items=WikiCompileTask.success_items + 1, heartbeat_time=_utcnow())
            )
        except Exception as e:
            logger.warning("Item %s (id=%s) failed: %s", pageId, itemId, e)
            await session.execute(
                update(WikiCompileItem)
                    .where(WikiCompileItem.id == itemId)
                    .values(status=ITEM_STATUS_FAILED, error_message=str(e), finished_time=_utcnow())
            )
            await session.execute(
                update(WikiCompileTask)
                    .where(WikiCompileTask.id == taskId)
                    .values(failed_items=WikiCompileTask.failed_items + 1, heartbeat_time=_utcnow())
            )
        await session.commit()

    async def _pendingItems(self, session: AsyncSession, taskId: int) -> list[WikiCompileItem]:
        result = await session.execute(
            select(WikiCompileItem).where(
                WikiCompileItem.task_id == taskId,
                WikiCompileItem.status.in_((ITEM_STATUS_PENDING, ITEM_STATUS_RUNNING)),
            ).order_by(WikiCompileItem.id).limit(50)
        )
        return list(result.scalars().all())

    async def _reclaimStaleItems(self, session: AsyncSession, taskId: int) -> None:
        stale_time = _utcnow()
        await session.execute(
            update(WikiCompileItem).where(
                WikiCompileItem.task_id == taskId,
                WikiCompileItem.status == ITEM_STATUS_RUNNING,
            ).where(
                WikiCompileItem.started_time < stale_time
            ).values(status=ITEM_STATUS_PENDING, started_time=None)
        )
        await session.flush()

    async def _finish(self, session: AsyncSession, taskId: int, successItems: int, failedItems: int, error: str | None) -> None:
        finished_time = _utcnow()
        heartbeat_time = _utcnow()
        if failedItems > 0 and successItems > 0:
            status = TASK_STATUS_PARTIAL
        elif failedItems > 0:
            status = TASK_STATUS_FAILED
        else:
            status = TASK_STATUS_SUCCEEDED
        await session.execute(
            update(WikiCompileTask).where(WikiCompileTask.id == taskId).values(
                error_message=error,
                finished_time=finished_time,
                heartbeat_time=heartbeat_time,
                status=status,
            )
        )
        await session.commit()

    async def _markFailedBestEffort(self, session: AsyncSession, taskId: int) -> None:
        await session.execute(
            update(WikiCompileItem).where(
                WikiCompileItem.task_id == taskId,
                WikiCompileItem.status == ITEM_STATUS_RUNNING,
            ).values(status=ITEM_STATUS_FAILED, error_message="Task abandoned")
        )
        await session.flush()

    async def getTask(self, session: AsyncSession, taskId: int) -> WikiCompileTask | None:
        result = await session.execute(
            select(WikiCompileTask).where(WikiCompileTask.id == taskId)
        )
        return result.scalar_one_or_none()

    async def listTasks(self, session: AsyncSession, limit: int = 50, offset: int = 0) -> tuple[list[WikiCompileTask], int]:
        result = await session.execute(
            select(WikiCompileTask).order_by(WikiCompileTask.id.desc()).limit(limit).offset(offset)
        )
        total_result = await session.execute(select(func.count(WikiCompileTask.id)))
        return list(result.scalars().all()), total_result.scalar() or 0

    def _buildInvoker(self, session: AsyncSession, modelId: int, fallbackId: int | None) -> LLMInvoker | None:
        try:
            return LLMInvoker(session, primaryModelId=modelId, fallbackModelId=fallbackId)
        except Exception:
            return None

__all__ = ["WikiCompileService"]
