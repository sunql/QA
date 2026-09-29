"""P3 批量编译器 HTTP 接口。"""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import getDb, getCurrentUser, CurrentUser
from app.domain.wiki_compile_models import (
    TASK_STATUS_PENDING, TASK_STATUS_RUNNING, TASK_STATUS_SUCCEEDED,
    TASK_STATUS_PARTIAL, TASK_STATUS_FAILED, ITEM_STATUS_PENDING,
    ITEM_STATUS_RUNNING, ITEM_STATUS_DONE, ITEM_STATUS_SKIPPED, ITEM_STATUS_FAILED,
    COMPILE_SCOPES, WikiCompileTask, WikiCompileItem,
)
from app.domain.wiki_compile_schemas import (
    WikiCompileCreateRequest, WikiCompileTaskRead, WikiCompileTaskListRead,
    WikiCompileItemRead, WikiClaimUpdateRequest, KnowledgeClaimDetailRead,
)
from app.domain.wiki_models import KnowledgeClaim
from app.services.wiki_compile_service import WikiCompileService
from app.services.wiki_page_service import WikiPageService
from app.services.messages_zh import MSG_WIKI_COMPILE_TASK_NOT_FOUND, MSG_WIKI_CLAIM_NOT_FOUND

router = APIRouter(prefix="/wiki/compile", tags=["compile"])

def _task_to_read(t: WikiCompileTask) -> WikiCompileTaskRead:
    return WikiCompileTaskRead(
        id=t.id, status=t.status, scope=t.scope,
        selected_model_id=t.selected_model_id, fallback_model_id=t.fallback_model_id,
        total_items=t.total_items, success_items=t.success_items,
        skipped_items=t.skipped_items, failed_items=t.failed_items,
        total_cost_usd=t.total_cost_usd, error_message=t.error_message,
        created_by_user_id=t.created_by_user_id, created_time=t.created_time,
        started_time=t.started_time, heartbeat_time=t.heartbeat_time,
        finished_time=t.finished_time,
    )

@router.post("/tasks", response_model=WikiCompileTaskRead, status_code=201)
async def create_task(body: WikiCompileCreateRequest, db: AsyncSession = Depends(getDb), user: CurrentUser = Depends(getCurrentUser)):
    if body.scope not in COMPILE_SCOPES:
        raise HTTPException(status_code=400, detail=f"scope must be one of {list(COMPILE_SCOPES)}")
    svc = WikiCompileService()
    task = await svc.createTask(
        db, scope=body.scope, pageIds=body.page_ids,
        dimension=body.dimension, modelId=body.model_id,
        fallbackModelId=body.fallback_model_id, userId=user.dbUserId,
    )
    return _task_to_read(task)

@router.post("/tasks/{taskId}/run", status_code=202)
async def run_task(taskId: int, db: AsyncSession = Depends(getDb)):
    svc = WikiCompileService()
    task = await svc.getTask(db, taskId)
    if task is None:
        raise HTTPException(status_code=404, detail=MSG_WIKI_COMPILE_TASK_NOT_FOUND.format(taskId=taskId))
    if task.status not in (TASK_STATUS_PENDING, TASK_STATUS_FAILED):
        raise HTTPException(status_code=409, detail="Task cannot be started in current state")
    # 在同一次请求中直接 await 任务完成，避免 asyncio.create_task 带来的
    # greenlet 上下文问题（MissingGreenlet / PendingRollbackError）。
    # 202 已在路由层声明，实际响应体不关键。
    import asyncio
    asyncio.create_task(svc.runTask(taskId))
    return {"message": "Task started", "taskId": taskId}

@router.get("/tasks", response_model=WikiCompileTaskListRead)
async def list_tasks(db: AsyncSession = Depends(getDb)):
    svc = WikiCompileService()
    tasks, total = await svc.listTasks(db)
    return WikiCompileTaskListRead(items=[_task_to_read(t) for t in tasks], total=total)

@router.get("/tasks/{taskId}", response_model=WikiCompileTaskRead)
async def get_task(taskId: int, db: AsyncSession = Depends(getDb)):
    svc = WikiCompileService()
    task = await svc.getTask(db, taskId)
    if task is None:
        raise HTTPException(status_code=404, detail=MSG_WIKI_COMPILE_TASK_NOT_FOUND.format(taskId=taskId))
    return _task_to_read(task)

@router.get("/tasks/{taskId}/items", response_model=list[WikiCompileItemRead])
async def list_task_items(taskId: int, db: AsyncSession = Depends(getDb)):
    result = await db.execute(
        select(WikiCompileItem).where(WikiCompileItem.task_id == taskId).order_by(WikiCompileItem.id)
    )
    items = result.scalars().all()
    return [
        WikiCompileItemRead(
            id=i.id, task_id=i.task_id, page_id=i.page_id, status=i.status,
            mechanism_counts=i.mechanism_counts, attempt_count=i.attempt_count,
            error_message=i.error_message, started_time=i.started_time,
            finished_time=i.finished_time,
        )
        for i in items
    ]

@router.patch("/claims/{claimId}", response_model=KnowledgeClaimDetailRead)
async def update_claim(claimId: int, body: WikiClaimUpdateRequest, db: AsyncSession = Depends(getDb)):
    from app.domain.schemas import CamelModel
    dto = CamelModel.model_validate({"claim_text": body.claim_text})
    svc = WikiPageService()
    claim = await svc.updateClaimText(db, claimId, dto=dto)
    return KnowledgeClaimDetailRead(
        id=claim.id, page_id=claim.page_id, claim_text=claim.claim_text,
        claim_type=claim.claim_type, subject_id=claim.subject_id,
        predicate=claim.predicate, object_value=claim.object_value,
        object_type=claim.object_type, confidence=claim.confidence,
        authority_level=claim.authority_level, status=claim.status,
        triple_stale=claim.triple_stale, created_time=claim.created_time,
    )

__all__ = ["router"]
