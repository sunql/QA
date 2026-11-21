"""发布校验结论的落表与查询（M1-1.2）。

`semantic_release_check` 是「发布校验报告」的落库形态：
每次发布校验（草稿编译 / validate / activate）都把本次的**全部**校验项
（`PASS` 与 `FAIL` 都落）写进该表，**覆盖**该 release 的旧结论。

## 为什么「失败也要落表」

M1-a DoD：「构造一条缺 JOIN 的绑定 → validate 报 ERROR → check 表可查到该条
→ 不得激活」。⇒ 校验**失败**时的结论必须是可查的证据，否则「不得激活」不可证。
⇒ 失败路径必须**先提交 check 行再抛错**（与状态迁移解耦）——
   这一点由 `semantic_release_service` 编排，本模块只负责写入。

## 覆盖写（replace-all）语义

一个 release 的「当前校验结论」只有一份：每次评估都代表最新一次判断。
历史判断由 `audit_log` 的状态迁移留痕承担（谁在什么时候把 DRAFT 推到 VALIDATED），
不靠 check 表本身堆多轮历史 —— 否则「这张表有几行」就不再等于「这次校验查了几项」。

## 幂等 / 并发

`replaceChecks` = 「删该 release 的旧行 + 写新行」，两者在同一事务里；
调用方决定提交时机（成功路径随状态迁移一起提交，失败路径先提交证据再抛错）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.semantic_release_check_models import SemanticReleaseCheck
from app.services.semantic_release_checks import (
    MAX_CHECKS_PER_RELEASE,
    RESULT_FAIL,
    RESULT_PASS,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    PublishCheck,
    capChecks,
)

logger = logging.getLogger(__name__)

# 单个发布的校验项条数上限 —— **定义在 checks 模块**（从 Manifest 契约推导），
# 这里只做重导出（调用方历史上从本模块取）。
# ⚠️ 上限不是「随便取个够大的数」：契约允许的合法 manifest 必须**永不触发截断**
# （截断丢的是尾部校验项，往往正是最后一条指标的 ERROR 结论行）。
# ⚠️ 截断逻辑本身在 `capChecks`（checks 模块）里 ⇒ 闸门与落表用**同一个**上限。
__all__ = [
    "MAX_CHECKS_PER_RELEASE",
    "semanticReleaseCheckService",
    "summarizeChecksInDb",
    "toCheckDict",
]


class SemanticReleaseCheckService:
    """发布校验结论的写入与读取。"""

    async def replaceChecks(
        self,
        session: AsyncSession,
        releaseId: int,
        checks: tuple[PublishCheck, ...],
        *,
        actor: str | None = None,
    ) -> int:
        """覆盖写该 release 的校验结论（**不提交**，由调用方决定事务边界）。

        返回实际写入的行数。

        ⚠️ 截断**不是静默的**：`capChecks` 会把超限部分换成一条
        `CHECKS_TRUNCATED`（ERROR/FAIL）合成行 ⇒ 表里能查到「报告被截断」，
        且闸门（用同一集合算）会把它当阻断项。正常路径（契约内的 manifest）
        永远不触发截断 —— 上限就是从契约推导的。
        """
        await session.execute(
            delete(SemanticReleaseCheck).where(SemanticReleaseCheck.release_id == releaseId)
        )

        kept = capChecks(checks)
        if len(kept) < len(checks):
            logger.warning(
                "发布校验项超过上限，已截断并标记：release=%s 原始=%s 落库=%s",
                releaseId,
                len(checks),
                len(kept),
            )

        checkedTime = datetime.now(UTC)
        rows = [
            SemanticReleaseCheck(
                release_id=releaseId,
                check_code=check.code,
                check_item=check.item,
                severity=check.severity,
                result=check.result,
                target_type=check.targetType,
                target_key=check.targetKey,
                detail=check.detail,
                checked_time=checkedTime,
                checked_by=actor,
            )
            for check in kept
        ]
        session.add_all(rows)
        await session.flush()
        return len(rows)

    async def listChecks(
        self,
        session: AsyncSession,
        releaseId: int,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SemanticReleaseCheck]:
        """该 release 的校验结论（按写入顺序 = 校验项评估顺序）。

        `limit` 为 None = 全取（服务层内部用）；HTTP 层传分页参数。
        """
        statement = (
            select(SemanticReleaseCheck)
            .where(SemanticReleaseCheck.release_id == releaseId)
            .order_by(SemanticReleaseCheck.id.asc())
        )
        if limit is not None:
            statement = statement.limit(limit).offset(offset)
        return list((await session.execute(statement)).scalars().all())

    async def countChecks(self, session: AsyncSession, releaseId: int) -> int:
        """该 release 的校验结论总条数（分页的 `total`，不受 limit 影响）。"""
        statement = (
            select(func.count())
            .select_from(SemanticReleaseCheck)
            .where(SemanticReleaseCheck.release_id == releaseId)
        )
        return int((await session.scalar(statement)) or 0)

    async def summarizeChecksInDb(
        self, session: AsyncSession, releaseId: int
    ) -> dict[str, int]:
        """全量汇总（在库里聚合，不受分页影响）。

        ⇒ 分页页面上看到的 summary 仍然是「整份报告」的结论，而不是「这一页」的结论。

        ⚠️ **只有这一份实现**（不再保留「按行内存聚合」的孪生函数）：两套聚合口径
        一旦漂移，就会出现「页面 summary 说 0 个 ERROR、表里却有 ERROR 行」
        —— 与本特性的核心承诺（闸门与落表零漂移）自相矛盾。
        """
        failedError = (SemanticReleaseCheck.result == RESULT_FAIL) & (
            SemanticReleaseCheck.severity == SEVERITY_ERROR
        )
        failedWarning = (SemanticReleaseCheck.result == RESULT_FAIL) & (
            SemanticReleaseCheck.severity == SEVERITY_WARNING
        )
        statement = select(
            func.count(),
            func.count().filter(SemanticReleaseCheck.result == RESULT_PASS),
            func.count().filter(SemanticReleaseCheck.result == RESULT_FAIL),
            func.count().filter(failedError),
            func.count().filter(failedWarning),
        ).where(SemanticReleaseCheck.release_id == releaseId)
        row = (await session.execute(statement)).one()
        total, passed, failed, errorCount, warningCount = (int(v or 0) for v in row)
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errorCount": errorCount,
            "warningCount": warningCount,
        }


semanticReleaseCheckService = SemanticReleaseCheckService()


async def summarizeChecksInDb(session: AsyncSession, releaseId: int) -> dict[str, int]:
    """全量汇总（库里聚合）—— 分页页面上 summary 仍是「整份报告」的结论。"""
    return await semanticReleaseCheckService.summarizeChecksInDb(session, releaseId)


def toCheckDict(row: SemanticReleaseCheck) -> dict[str, Any]:
    """ORM 行 ⇒ 纯字典（DTO 装配用；避免把 ORM 实例直接丢给序列化层）。"""
    return {
        "id": row.id,
        "release_id": row.release_id,
        "check_code": row.check_code,
        "check_item": row.check_item,
        "severity": row.severity,
        "result": row.result,
        "target_type": row.target_type,
        "target_key": row.target_key,
        "detail": row.detail,
        "checked_time": row.checked_time,
        "checked_by": row.checked_by,
    }
