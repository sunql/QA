"""研究入口薄执行 runner（Task 4）：只读 SQL 执行 + 假设验证（失败不抛）。

设计约束（plan §Task 4 对设计 §4.3 的修正）：不复用 ``ChatService`` 的
``_executeDataStep`` / ``_runQueryWithRetry`` —— 那是 mixin 私有路径。本 runner
只依赖**调用方按次送达的业务库 adapter**与公开 SQL Guard。

**Task 13e（根因修正）**：业务 SQL 一律经 ``BusinessDbAdapter.execute_read_only``
执行，**不再**打在调用方注入的元数据库 session 上。历史缺陷：研究侧执行 SQL 时压根
没有业务库连接 —— 注入的是应用元数据库（Postgres）会话，而业务表在 Oracle（``THBI
Oracle``）⇒ 一律 ``UndefinedTableError``；13b「6/6 检查通过」与 13d「``.sql`` 事件
出现」两次验收**都从未触达业务数据**，因此全绿。

接线方式（控制器裁定）：adapter **按次送达** —— ``ResearchSqlRunner`` 在
``buildResearchAgentService()`` 里一次性构造（无自定义 ``__init__``），而数据源逐请求
才知道 ⇒ 走**方法入参**（``adapter=``），**不给 runner 加构造函数参数**。

保留语义，**不重复实现**（能复用适配器路径的一律复用）：
- 只读守卫：``assertReadonlySql`` 把 SQL Guard 的 ``SqlSafetyError`` 转成 ``ValueError``
  （Task 4 对外契约），适配器内部原样复用同一个 ``_assert_read_only`` 原语，不新增第二套解析；
- 方言后处理（``_quote_digit_leading_identifiers`` / ``_inject_nulls_last``）、库侧只读
  兜底（``SET TRANSACTION READ ONLY``）与执行超时：全由 ``execute_read_only`` 提供；
- 行数上限 ``VERIFICATION_ROW_CAP``：适配器的限行来自 ``QUERY_ROW_LIMIT`` 配置，而该配置
  **默认 0 = 不限行**，在未配置限行的部署里等于没有上限 ⇒ 本层对结果做显式截断 + warning，
  让「验证查询结果形状」与「配置是否限行」解耦（截断是显式的，不静默）。

``session`` 入参**刻意保留**：它是调用方的元数据库事务句柄，本 runner 不再用它执行任何
业务 SQL —— 它是「业务 SQL 不得打在元数据库会话上」这条回归守卫的断言点
（见 ``test_research_sql_runner.py::test_execute_readonly_never_uses_metadata_session``）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import SqlSafetyError
from app.infrastructure.business_db_pool import _assert_read_only
from app.services.hypothesis_service import Hypothesis

logger = logging.getLogger(__name__)

# 单次验证查询的结果行数上限。刻意**不**复用 getSettings().queryRowLimit：该配置默认
# 0 = 不限制，而本 runner 要修的正是「无上限」这条风险；1000 与 business_db_pool 的
# _UNLIMITED_BATCH_SIZE 同量级，作为「验证查询」这种小结果集的合理上界。
VERIFICATION_ROW_CAP = 1000

# 验证查询超行截断日志里 SQL 字符串的字符上限。仅影响运维日志可读性，避免日志里堆
# 整段长 SQL；下游若有日志解析依赖此长度需同步调整。
LOG_SQL_TRUNCATE_LEN = 120


def assertReadonlySql(sql: str) -> None:
    """只读校验：SQL Guard 拒绝（``SqlSafetyError``）一律转为 ``ValueError``。

    只转换 ``SqlSafetyError`` 一种：``_assert_read_only`` 及其内部
    ``_assertNoHiddenWrites`` 的**全部 12 处 raise 都是 ``SqlSafetyError``**
    （空 / 多语句 / 解析失败 / 非只读动词 / 黑名单动词 / 隐藏写），即 Guard 的
    完整拒绝面；其余异常（驱动层等）不属于「非法 SQL」，原样上抛不做伪装。

    转换而非新增异常类型：本 runner 对外只暴露 ``ValueError``（Task 4 brief 的
    ``test_rejects_dml`` 契约），原始拒绝原因经 ``__cause__`` 与消息文本保留。
    适配器路径会再校验一次（同一原语，幂等）；此处先校验是为了**在离开进程前**
    就给出研究链路的错误类型，且不产生一次无谓的连接。
    """
    try:
        _assert_read_only(sql)
    except SqlSafetyError as exc:
        raise ValueError(str(exc)) from exc


class ResearchSqlRunner:
    """只读 SQL 执行与假设验证（无状态，可复用）。"""

    async def executeReadonlySql(
        self, session: AsyncSession, sql: str, *, adapter: Any
    ) -> list[dict[str, Any]]:
        """SQL Guard 校验后经**业务库 adapter** 执行只读查询，返回行字典列表。

        失败面（显式，四条）：
        - ``ValueError``：SQL Guard 拒绝（非只读 / 多语句 / 隐藏写 / 空）；
        - ``RuntimeError``：adapter 未送达（接线缺失）—— 刻意用非 ``ValueError``，否则会被
          调用方当成「SQL 被 Guard 拒绝」降级成「该步无数据」，把配置缺失伪装成业务失败；
        - ``TimeoutError``：适配器侧执行超时（``QUERY_TIMEOUT_SECONDS``）；
        - 驱动层异常（表不存在、权限、语法）原样上抛。

        行数上限 ``VERIFICATION_ROW_CAP``：超限**截断**并打 warning（见模块 docstring）。
        """
        assertReadonlySql(sql)
        if adapter is None:
            raise RuntimeError(
                "业务库 adapter 未送达：拒绝在应用元数据库会话上执行业务 SQL"
            )
        rows = await adapter.execute_read_only(sql)
        if len(rows) > VERIFICATION_ROW_CAP:
            logger.warning(
                "验证查询结果超过行上限 %d，已截断: %s", VERIFICATION_ROW_CAP, sql[:LOG_SQL_TRUNCATE_LEN]
            )
            return rows[:VERIFICATION_ROW_CAP]
        return rows

    async def runVerification(
        self, session: AsyncSession, hypothesis: Hypothesis, *, adapter: Any
    ) -> dict[str, Any]:
        """执行假设的 verificationSql，返回 ``{"rows": [...], "error": None | str}``。

        验证失败不抛：设计允许「验证失败」本身作为 finding 的结论（Task 5 据此
        以低 confidence 落库），故**业务性**异常在此收敛为 error 字符串 + warning 日志。

        **唯一例外**（Task 14 / E1）：``RuntimeError`` 是 ``executeReadonlySql`` 的
        「adapter 未送达（接线缺失）」信号，**原样上抛**——把它收敛成 ``error`` 会把
        配置缺失伪装成「验证失败」的低置信结论（与 ``executeReadonlySql`` docstring
        刻意用非 ``ValueError`` 的意图相合）。

        失败后仍**回滚**调用方事务：Task 13e 后业务 SQL 已不走 ``session``（不再把
        元数据库会话打成失败态），此处保留是**兜底** —— 调用方在同一会话上可能有
        其它未决写/失败事务，而失败结论需要会话可用才能落 finding。契约不变：
        调用方不应在同一 session 上留有「必须保留的未提交写入」（Task 5 先 commit 再验证）。
        """
        try:
            rows = await self.executeReadonlySql(session, hypothesis.verificationSql, adapter=adapter)
            return {"rows": rows, "error": None}
        except RuntimeError:
            raise  # 接线缺失：冒泡，不得降级成「验证失败」
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
