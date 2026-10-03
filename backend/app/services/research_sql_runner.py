"""研究入口薄执行 runner（Task 4）：只读 SQL 执行 + 假设验证（失败不抛）。

设计约束（plan §Task 4 对设计 §4.3 的修正）：不复用 ``ChatService`` 的
``_executeDataStep`` / ``_runQueryWithRetry`` —— 那是 mixin 私有路径。本 runner
只依赖调用方注入的 ``AsyncSession`` 与公开 SQL Guard。

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
（执行面公开入口：420:class BusinessDbAdapter(Protocol) / 436:async def
execute_read_only(self, sql) -> list[dict[str, Any]] / 696:def get_adapter(datasourceId:
int, ds: DataSource) -> BusinessDbAdapter）

$ grep -rnE "def (validate|check|guard)" app/services/sql_guard*.py app/infrastructure/sql_guard*.py
（无匹配：不存在 sql_guard*.py 模块。真实 SQL Guard = business_db_pool._assert_read_only，
抛 SqlSafetyError，被 8 处服务公开复用。）

接线裁定：本 runner 的入参只有 ``session``（无 datasourceId），因此执行走调用方注入的
会话；只读防线复用与 ``execute_read_only`` 同一个 ``_assert_read_only`` 原语 ——
同一 Guard、同一拒绝原因，不新增第二套 SQL 解析逻辑。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import SqlSafetyError
from app.infrastructure.business_db_pool import _assert_read_only
from app.services.hypothesis_service import Hypothesis

logger = logging.getLogger(__name__)


def assertReadonlySql(sql: str) -> None:
    """只读校验；非法 SQL 抛 ``ValueError``。

    SQL Guard 原语 ``_assert_read_only`` 抛 ``SqlSafetyError``（DomainError 子类，
    不是 ValueError）。本 runner 的对外契约（Task 4 brief 的 ``test_rejects_dml``）
    是 ``ValueError``，故在此转换并链上 cause：调用方只需捕获一种异常，原始拒绝
    原因仍可从 ``__cause__`` / 消息文本取回（不静默吞错）。
    """
    try:
        _assert_read_only(sql)
    except SqlSafetyError as exc:
        raise ValueError(str(exc)) from exc


class ResearchSqlRunner:
    """只读 SQL 执行与假设验证（无状态，可复用）。"""

    async def executeReadonlySql(self, session: AsyncSession, sql: str) -> list[dict[str, Any]]:
        """SQL Guard 校验后执行只读查询，返回行字典列表。

        非法 SQL（DML/DDL、多语句、CTE 内隐藏写等）抛 ``ValueError``。
        """
        assertReadonlySql(sql)
        result = await session.execute(text(sql))
        return [dict(row._mapping) for row in result.fetchall()]

    async def runVerification(
        self, session: AsyncSession, hypothesis: Hypothesis
    ) -> dict[str, Any]:
        """执行假设的 verificationSql，返回 ``{"rows": [...], "error": None | str}``。

        验证失败不抛：设计允许「验证失败」本身作为 finding 的结论（Task 5 据此
        以低 confidence 落库），故全部异常在此收敛为 error 字符串 + warning 日志。
        """
        try:
            rows = await self.executeReadonlySql(session, hypothesis.verificationSql)
            return {"rows": rows, "error": None}
        except Exception as exc:  # noqa: BLE001 —— 失败是合法结论，见 docstring
            logger.warning("假设验证失败，降级为 error 结论: %s", exc)
            return {"rows": [], "error": str(exc)}
