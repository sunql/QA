"""修复卡在 ``RUNNING`` 的导入任务：按实际入库结果回填台账。

**为什么会卡住**：``wiki_import_service.execute`` 先 commit 一条 ``RUNNING`` 的
task，再逐条导入。进程若被外力杀死（容器重建、OOM、kill -9），那个兜底的
``except`` 分支根本不会执行 —— task 就永远停在 ``RUNNING``，用户看到「进行中」，
实际早就没人跑了。``_markFailedBestEffort`` 只在「异常被抛到 Python 层」时生效，
覆盖不了「进程直接没了」。

**修复口径 = 复刻 ``_markFailedBestEffort`` 的语义**（不是自创）：

- ``status = FAILED``（代码对「异常中止」用的就是这个值，不是 PARTIAL）
- ``page_ids`` / ``success_pages`` ← 从 ``wiki_page.imported_via_task_id`` 反查
  真实落库的条目。**台账必须反映真实入库量**，否则界面显示「失败」看起来像
  一条都没进去，实际库里躺着一半。
- ``failed_pages`` ← 保持 0：该字段记的是「冲突/校验失败」的条数，中断场景
  下没有任何条目失败过。未处理的那部分体现在 ``total_pages - success_pages``。
- ``total_cost_usd`` ← 取 ``wiki_token_usage`` 的 SUM（与正常运行路径同源；
  中断路径没写这个字段，留着 0 会让成本台账凭空少一笔）。
- ``finished_time`` ← 已入库条目里最晚的一条（比 ``now()`` 更接近真实结束时刻）。

**幂等**：只处理 ``status = 'RUNNING'`` 的行，跑完即不再命中，可重复执行。

**注意**：``wiki_import_task.page_ids`` 目前只写不读 —— 没有任何代码拿它做
「重跑跳过已成功项」。（模型 docstring 里那句「失败重跑时用来跳过已成功项」
尚未实现；page_id 每次导入都重新生成，重跑同批次会产出**又一份完整副本**。
库里 587 条 wiki_page ≈ 同一份 75 条文档的 7.8 份副本，就是这么来的。）
回填 page_ids 是为了让台账如实，不代表续跑能力已经存在。

用法（容器内跑，DATABASE_URL 已由 compose 注入，避免在脚本里硬编码口令）::

    docker exec -w /app qa-backend uv run python scripts/repair_stuck_import_tasks.py            # 预览
    docker exec -w /app qa-backend uv run python scripts/repair_stuck_import_tasks.py --apply    # 落库
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update

from app.domain.wiki_learning_models import WikiImportTask, WikiTokenUsage
from app.domain.wiki_models import WikiPage
from app.infrastructure.database import getSessionFactory
from app.services.messages_zh import MSG_WIKI_IMPORT_ABORTED


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _collectRepair(session: Any, task: WikiImportTask) -> dict[str, Any]:
    """把一个卡住的 task 换算成「若当时没被杀，兜底分支会写成什么」。"""
    rows = (
        await session.execute(
            select(WikiPage.page_id, WikiPage.created_time)
            .where(WikiPage.imported_via_task_id == task.id)
            .order_by(WikiPage.created_time)
        )
    ).all()

    pageIds = [r[0] for r in rows]
    lastPageAt = max((r[1] for r in rows if r[1] is not None), default=None)

    totalCost = (
        await session.execute(
            select(func.coalesce(func.sum(WikiTokenUsage.cost), 0)).where(
                WikiTokenUsage.import_task_id == task.id
            )
        )
    ).scalar_one()

    remaining = max(task.total_pages - len(pageIds), 0)
    message = (
        f"{MSG_WIKI_IMPORT_ABORTED}"
        f"（进程在导入途中被终止，非数据错误；中断时已入库 {len(pageIds)}"
        f"/{task.total_pages} 条，剩余 {remaining} 条未处理）"
    )

    return {
        "status": "FAILED",
        "page_ids": pageIds,
        "success_pages": len(pageIds),
        "failed_pages": 0,
        "total_cost_usd": totalCost,
        "error_message": message,
        "finished_time": lastPageAt or _utcnow(),
    }


async def main(apply: bool) -> int:
    factory = getSessionFactory()
    async with factory() as session:
        stuck = (
            (
                await session.execute(
                    select(WikiImportTask)
                    .where(WikiImportTask.status == "RUNNING")
                    .order_by(WikiImportTask.id)
                )
            )
            .scalars()
            .all()
        )

        if not stuck:
            print("没有处于 RUNNING 的导入任务，无需修复。")
            return 0

        for task in stuck:
            patch = await _collectRepair(session, task)

            print(f"--- task #{task.id} ---")
            print(f"  现状: status={task.status} success={task.success_pages} "
                  f"failed={task.failed_pages} page_ids={len(task.page_ids or [])} "
                  f"cost={task.total_cost_usd} finished={task.finished_time}")
            print(f"  改为: status={patch['status']} success={patch['success_pages']} "
                  f"failed={patch['failed_pages']} page_ids={len(patch['page_ids'])} "
                  f"cost={patch['total_cost_usd']} finished={patch['finished_time']}")
            print(f"  未处理条目数: {task.total_pages - patch['success_pages']}")

            if not apply:
                continue

            # 用 Core UPDATE 而不是给 ORM 实例赋值：单条语句、无 identity map
            # 缓存歧义（本仓库踩过「ORM 读到旧值」的坑）。
            # 附带 status 守卫，避免修复期间任务被别的进程改走。
            result = await session.execute(
                update(WikiImportTask)
                .where(WikiImportTask.id == task.id, WikiImportTask.status == "RUNNING")
                .values(**patch)
            )
            print(f"  影响行数: {result.rowcount}")

        if apply:
            await session.commit()
            print("已提交。")
        else:
            await session.rollback()
            print("（预览模式，未写入。加 --apply 落库）")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(apply="--apply" in sys.argv)))
