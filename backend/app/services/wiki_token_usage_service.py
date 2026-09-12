"""Wiki 学习机制的 LLM 计量服务（feat-wiki-knowledge，Phase 8 M2）。

与 ``token_usage_service`` 的区别（**刻意不复用**）：

- ``token_usage_service.recordUsage`` 写 ``session_token_usage`` 且
  ``sessionId`` 必填，且**自己 commit**。知识导入/分类没有 chat session，
  硬编一个假 sessionId 会污染会话维度成本报表；而「自己 commit」会让导入
  任务的部分进度提前落库、破坏 execute 的原子边界。
- 本服务写 ``wiki_token_usage``，``import_task_id`` 可空（手动分类无任务），
  且 **只 add + flush，不 commit**——提交时机交给调用方（导入任务要按页
  提交以保留部分成功进度）。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_learning_models import LEARNING_MECHANISMS, WikiTokenUsage


def _assertMechanism(mechanism: str) -> None:
    """机制名白名单校验，挡住拼错导致的计量散落。"""
    if mechanism not in LEARNING_MECHANISMS:
        raise ValueError(
            f"未知的学习机制 {mechanism!r}，合法值：{list(LEARNING_MECHANISMS)}"
        )


class WikiTokenUsageService:
    """记录学习机制的 LLM 调用计量。"""

    async def record(
        self,
        session: AsyncSession,
        *,
        mechanism: str,
        modelConfigId: int | None,
        modelName: str | None,
        promptTokens: int,
        completionTokens: int,
        cost: Decimal,
        purpose: str | None = None,
        importTaskId: int | None = None,
    ) -> WikiTokenUsage:
        """写入一条计量记录（add + flush，**不 commit**）。

        调用方负责 commit——导入任务按页提交以保留部分成功进度。
        """
        _assertMechanism(mechanism)
        row = WikiTokenUsage(
            import_task_id=importTaskId,
            mechanism=mechanism,
            model_config_id=modelConfigId,
            model_name=modelName,
            prompt_tokens=promptTokens,
            completion_tokens=completionTokens,
            cost=cost,
            purpose=purpose,
        )
        session.add(row)
        await session.flush()
        return row


__all__ = ["WikiTokenUsageService"]
