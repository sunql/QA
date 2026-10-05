"""研究入口假设适配层（Task 4）：LLM 输出 → 合法 ``Hypothesis`` 列表。

复用 ``hypothesis_service`` 的公开面（prompt 构造 / fence+JSON 解析 / 静态只读校验），
不重写解析逻辑：M7 Hypothesis Hook 与本层消费同一份 LLM 契约，任何一方漂移都会
让「假设」语义分叉。

best-effort：解析失败返回空列表（``parseHypotheses`` 已保证不抛），由调用方
（Task 5 的 ``_stageHypothesis``）决定降级表现。

Step 0 核对结果（2026-10-04，本任务强制）：

$ grep -nE "class |def " app/infrastructure/business_db_pool.py | head -8
167:def _assert_read_only(sql: str) -> None:
213:def _unquoteIdentifier(raw: str) -> str:
225:def _callShape(flat: list[sqlparse.sql.Token], idx: int) -> str | None:
250:def _assertNoHiddenWrites(stmt: sqlparse.sql.Statement, sql: str) -> None:
314:def _quote_digit_leading_identifiers(sql: str) -> str:
331:    def _shelter(match: re.Match[str]) -> str:
346:def _inject_nulls_last(sql: str) -> str:
365:    def _shelter(match: re.Match[str]) -> str:
（同文件其余公开面：420:class BusinessDbAdapter(Protocol) / 436:async def
execute_read_only(self, sql) / 696:def get_adapter(datasourceId: int, ds: DataSource)
-> BusinessDbAdapter）

$ grep -rnE "def (validate|check|guard)" app/services/sql_guard*.py app/infrastructure/sql_guard*.py
（无匹配：不存在 sql_guard*.py 模块。真实 SQL Guard 入口是
app/infrastructure/business_db_pool.py 的模块级 ``_assert_read_only(sql) -> None``，
抛 SqlSafetyError；已被 nl2sql_service / agent_tools_nl2sql / feature_compute_service /
feature_definition_service / agent_runtime_service / nl2sql_prompts / chat_service
等 8 处公开复用 —— 即本任务「公开面复用」的对象。）
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.infrastructure.llm.base_client import LlmMessage
from app.services.hypothesis_service import (
    Hypothesis,
    buildHypothesisMessages,
    isValidVerificationSql,
    parseHypotheses,
)
from app.services.llm_json_fence import stripJsonFence

logger = logging.getLogger(__name__)


class HypothesisLlmClient(Protocol):
    """假设生成所需的最小 LLM 客户端契约（``BaseLlmClient`` 结构性满足）。"""

    async def complete(self, messages: list[LlmMessage], **kwargs: Any) -> Any: ...


async def generateHypotheses(
    llmClient: HypothesisLlmClient,
    question: str,
    dataSummary: str,
    drivers: list[str],
) -> list[Hypothesis]:
    """生成假设列表：prompt → LLM → 剥 fence → 解析 → 过滤非法 verification_sql。

    过滤是第二层防御：``parseHypotheses`` 已丢弃非法项，此处再按
    ``isValidVerificationSql`` 复核（两者若漂移，以静态校验为准），丢弃计数入
    warning 日志（显式处理，不静默）。
    """
    messages = buildHypothesisMessages(question, dataSummary, drivers)
    raw = await llmClient.complete(messages)
    text = stripJsonFence(getattr(raw, "content", None) or str(raw))
    parsed = parseHypotheses(text)
    hypotheses = [h for h in parsed if isValidVerificationSql(h.verificationSql)]
    dropped = len(parsed) - len(hypotheses)
    if dropped:
        logger.warning("假设适配层过滤 %d 条非法 verification_sql", dropped)
    return hypotheses
