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

与 ``_SqlaAdapter.execute_read_only`` 的**能力对齐（fix round 1）**：池路径自带
行数上限 + ``asyncio.wait_for`` 超时，注入 session 路径此前只有解析层防线。本模块
补齐同样两项（``VERIFICATION_ROW_CAP`` / ``VERIFICATION_TIMEOUT_SECONDS``，语义
与池一致：超限**截断**并 warning、超时抛 TimeoutError）。池路径的**库侧只读兜底**
（``SET TRANSACTION READ ONLY`` 事务级只读）不在此实现——它属于 Task 5 的执行
上下文（需 begin() 后再 SET，见 business_db_pool 注释），已列入 carry-over。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import SqlSafetyError
from app.infrastructure.business_db_pool import _assert_read_only
from app.services.hypothesis_service import Hypothesis

logger = logging.getLogger(__name__)

# 单次验证查询的行数上限。刻意**不**复用 getSettings().queryRowLimit：该配置默认
# 0 = 不限制，而本 runner 要修的正是「无上限」这条风险；1000 与 business_db_pool 的
# _UNLIMITED_BATCH_SIZE 同量级，作为「验证查询」这种小结果集的合理上界。
VERIFICATION_ROW_CAP = 1000

# 单次验证查询的超时（秒）。取值与 getSettings().queryTimeoutSeconds 默认值一致，
# 但此处是常量而非配置：runner 不持有 settings，且 Task 4 要求本层自带闸门。
VERIFICATION_TIMEOUT_SECONDS = 30.0


def assertReadonlySql(sql: str) -> None:
    """只读校验：SQL Guard 拒绝（``SqlSafetyError``）一律转为 ``ValueError``。

    只转换 ``SqlSafetyError`` 一种：``_assert_read_only`` 及其内部
    ``_assertNoHiddenWrites`` 的**全部 12 处 raise 都是 ``SqlSafetyError``**
    （空 / 多语句 / 解析失败 / 非只读动词 / 黑名单动词 / 隐藏写），即 Guard 的
    完整拒绝面；其余异常（驱动层等）不属于「非法 SQL」，原样上抛不做伪装。

    转换而非新增异常类型：本 runner 对外只暴露 ``ValueError``（Task 4 brief 的
    ``test_rejects_dml`` 契约），原始拒绝原因经 ``__cause__`` 与消息文本保留。
    """
    try:
        _assert_read_only(sql)
    except SqlSafetyError as exc:
        raise ValueError(str(exc)) from exc


class ResearchSqlRunner:
    """只读 SQL 执行与假设验证（无状态，可复用）。"""

    async def executeReadonlySql(self, session: AsyncSession, sql: str) -> list[dict[str, Any]]:
        """SQL Guard 校验后执行只读查询，返回行字典列表。

        失败面（显式，三条）：
        - ``ValueError``：SQL Guard 拒绝（非只读 / 多语句 / 隐藏写 / 空）；
        - ``TimeoutError``：执行超过 ``VERIFICATION_TIMEOUT_SECONDS``（asyncio 取消）；
        - 驱动层异常（表不存在、权限、语法）原样上抛。

        行数上限 ``VERIFICATION_ROW_CAP``：超限**截断**（与
        ``_SqlaAdapter.execute_read_only`` 的 ``fetchmany(limit)`` 同语义）并打
        warning —— 截断是显式的，不静默。多取一行用于判定是否真的发生过截断。

        失败路径会让注入的 session 进入失败事务态，调用方需 ``rollback``
        （``runVerification`` 已内建）。
        """
        assertReadonlySql(sql)

        async def _run() -> list[dict[str, Any]]:
            result = await session.execute(text(sql))
            rows = result.mappings().fetchmany(VERIFICATION_ROW_CAP + 1)
            if len(rows) > VERIFICATION_ROW_CAP:
                logger.warning(
                    "验证查询结果超过行上限 %d，已截断: %s",
                    VERIFICATION_ROW_CAP,
                    sql[:120],
                )
                rows = rows[:VERIFICATION_ROW_CAP]
            return [dict(row) for row in rows]

        try:
            return await asyncio.wait_for(_run(), VERIFICATION_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise TimeoutError(
                f"只读验证查询超时（>{VERIFICATION_TIMEOUT_SECONDS}s）"
            ) from exc

    async def runVerification(
        self, session: AsyncSession, hypothesis: Hypothesis
    ) -> dict[str, Any]:
        """执行假设的 verificationSql，返回 ``{"rows": [...], "error": None | str}``。

        验证失败不抛：设计允许「验证失败」本身作为 finding 的结论（Task 5 据此
        以低 confidence 落库），故全部异常在此收敛为 error 字符串 + warning 日志。

        失败后**必须回滚**：语句失败会让注入的 session 进入失败事务态，不回滚则
        同一 session 上的后续语句一律抛 PendingRollbackError（PG 报
        ``current transaction is aborted``），把整条研究链路带崩。与
        ``hypothesis_service._maybeGenerateHypotheses`` 的失败路径同一处置。

        契约：调用方不应在同一 session 上留有「必须保留的未提交写入」——回滚会
        一并丢弃。Task 5 应先 commit 再验证（或验证后再写）。
        """
        try:
            rows = await self.executeReadonlySql(session, hypothesis.verificationSql)
            return {"rows": rows, "error": None}
        except Exception as exc:  # noqa: BLE001 —— 失败是合法结论，见 docstring
            await self._rollbackQuietly(session)
            logger.warning("假设验证失败，降级为 error 结论: %s", exc)
            return {"rows": [], "error": str(exc)}

    @staticmethod
    async def _rollbackQuietly(session: AsyncSession) -> None:
        """回滚失败事务；rollback 自身再失败（连接已死）只 warning，不上抛。"""
        try:
            await session.rollback()
        except Exception as rollbackExc:  # noqa: BLE001 - 连接已死时兜底
            logger.warning("验证失败路径 rollback 异常: %s", rollbackExc)
