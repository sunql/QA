"""可执行规则 / 结构化流程的读取与 dry-run（feat-wiki-knowledge，Phase 8 M6）。

与 ``services/learning/progressive_upgrader.py`` 分开，理由同前几个里程碑：
**抽取是机器的动作**（写产物、推阶段），**读取与试跑是人的动作**（看这条规则
到底长什么样、拿样例验一验）。前者跑在审核事务里，后者是纯读 + 纯函数。

本模块是 ``wiki_rule_engine``（纯求值）与外界的唯一接缝：引擎不认识 DB，这里
负责把规则从库里取出来、把报告组装成对外的形状。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError
from app.domain.wiki_learning_models import ProcessWorkflow, WikiRuleExecutable
from app.services.learning.progressive_upgrader import (
    StructureStageChange,
    applyStructureStage,
)
from app.services.learning.wiki_rule_engine import DryRunReport, dryRun
from app.services.messages_zh import (
    MSG_WIKI_RULE_NOT_FOUND,
    MSG_WIKI_WORKFLOW_NOT_FOUND,
)

logger = logging.getLogger(__name__)


class WikiStructureService:
    """规则 / 流程的读取与 dry-run。"""

    async def findRule(
        self, session: AsyncSession, pageId: str
    ) -> WikiRuleExecutable | None:
        """取条目的可执行规则；没有返回 ``None``（不 404）。

        与 ``getRule`` 的差别只在该不该 404：「读一条知识，顺带看它有没有
        结构化规则」里，没有规则是**正常状态**而不是错误。为它抛异常会逼
        调用方用 try/except 表达一个非例外的情况。
        """
        await _requirePage(session, pageId)
        result = await session.execute(
            select(WikiRuleExecutable).where(WikiRuleExecutable.page_id == pageId)
        )
        return result.scalar_one_or_none()

    async def getRule(
        self, session: AsyncSession, pageId: str
    ) -> WikiRuleExecutable:
        """取条目的可执行规则，没有则 404。"""
        entity = await self.findRule(session, pageId)
        if entity is None:
            raise NotFoundError(MSG_WIKI_RULE_NOT_FOUND.format(pageId=pageId))
        return entity

    async def findWorkflow(
        self, session: AsyncSession, pageId: str
    ) -> ProcessWorkflow | None:
        """取条目的结构化流程；没有返回 ``None``（不 404，理由同 ``findRule``）。"""
        await _requirePage(session, pageId)
        result = await session.execute(
            select(ProcessWorkflow).where(ProcessWorkflow.page_id == pageId)
        )
        return result.scalar_one_or_none()

    async def getWorkflow(self, session: AsyncSession, pageId: str) -> ProcessWorkflow:
        """取条目的结构化流程，没有则 404。"""
        entity = await self.findWorkflow(session, pageId)
        if entity is None:
            raise NotFoundError(MSG_WIKI_WORKFLOW_NOT_FOUND.format(pageId=pageId))
        return entity

    async def dryRun(
        self,
        session: AsyncSession,
        pageId: str,
        *,
        examples: Sequence[dict[str, Any]] | None = None,
    ) -> tuple[WikiRuleExecutable, DryRunReport]:
        """在样例上试跑规则。

        ``examples`` 为 ``None`` 时用规则里存的样例；传空列表则是明确的「这次不
        带样例」—— 两者的区别是「用存的」与「用空的」，不能混（传 ``[]`` 得到
        ``total=0`` 的报告，传 ``None`` 可能跑出若干条）。
        """
        rule = await self.getRule(session, pageId)
        stored = rule.dry_run_examples if isinstance(rule.dry_run_examples, list) else []
        report = dryRun(rule.rule_expression, examples if examples is not None else stored)
        return rule, report

    async def recomputeStage(
        self, session: AsyncSession, pageId: str
    ) -> StructureStageChange:
        """重算并落库阶段（幂等，供前端「刷新结构状态」用）。"""
        change = await applyStructureStage(session, pageId)
        await session.commit()
        return change


async def _requirePage(session: AsyncSession, pageId: str) -> None:
    """条目存在性前置检查（404 早失败）。

    先查条目而不是直接查规则，是为了让「条目不存在」与「条目存在但没有规则」
    返回不同的消息 —— 两者都是 404，但调用方该采取的行动完全不同。
    """
    from app.services.wiki_page_service import WikiPageService

    await WikiPageService().getPage(session, pageId)


__all__ = ["WikiStructureService"]
