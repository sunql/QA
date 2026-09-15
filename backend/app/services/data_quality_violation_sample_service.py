"""评估违规样本 service（feat-dq-evaluation-report，Phase 4）。

- `sample_for_report`：对单条规则跑 evaluator 的 collect_violation_samples 收集 top-N
  违规样本，落库到 data_quality_violation_sample。
- `list_for_report`：给 `/{report_id}/samples?ruleId=X&limit=N` 接口用，按报告 + 规则过滤。
- `cleanup_orphans`：报告硬删时级联清样本（DB FK ON DELETE CASCADE 已经处理；本函数
  留给软删过期的清理 worker）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataQualityViolationSample
from app.services.data_quality_evaluator import DataQualityEvaluatorDispatcher

logger = logging.getLogger(__name__)


DEFAULT_SAMPLE_LIMIT = 20
"""per-rule 违规样本上限；与 plan 文件一致，避免大表全量落库撑爆。"""


class DataQualityViolationSampleService:
    """评估违规样本 service。"""

    def __init__(self, dispatcher: DataQualityEvaluatorDispatcher | None = None) -> None:
        self._dispatcher = dispatcher or DataQualityEvaluatorDispatcher()

    async def sampleForRule(
        self,
        session: AsyncSession,
        *,
        report_id: int,
        rule,
        violation_count: int,
        limit: int = DEFAULT_SAMPLE_LIMIT,
        time_window: tuple[Any, Any] | None = None,
    ) -> DataQualityViolationSample | None:
        """对单条规则采集 top-N 违规样本并写入 DB。

        feat-sampling-error-visible (2026-09-15)：失败路径不再静默。
        - 采样成功且有样本：写正常行（sampling_error=None）
        - 采样成功但 0 命中：不写行（与旧行为一致）
        - sampler 抛错：写 [sample_size=0, sample_pk_values=[], sampling_error=<msg>] 行
          让前端在「违规样本」表里显式提示「采样失败: ORA-00933: ...」

        配置错误（rule=None / 不支持的 rule_type / 缺 datasource）依然不写行——
        这些是规则定义问题，不是采样运行期错误，应在规则编辑/报告向导里暴露。

        violation_count：evaluator 算出的实际违规条数（total_count - passed_count）。
        必传参数——之前用 -1 占位会让前端「违规总数」列始终显示 -1，用户看不见真实
        规模。snapshot 阶段已经跑过 evaluator，调用方一定有这个数。
        """
        samples, error = await self._dispatcher.collectSamples(
            session, rule, limit=limit, time_window=time_window,
        )
        if not samples and not error:
            # 没有违规样本 + 没有错误：与旧行为一致，不写行
            return None
        row = DataQualityViolationSample(
            report_id=report_id,
            rule_id=rule.id,
            datasource_id=rule.datasource_id,
            target_table=rule.target_table or "",
            target_column=rule.target_column,
            total_violations=max(int(violation_count), 0),
            sample_size=len(samples),
            # feat-sample-pk-shape (2026-09-15)：只存 row_id（sampler 实际取出的值）。
            # 历史 bug：老版本存了 sampler 整个 dict（含 target_table/target_column，与
            # 行字段重复），前端 String(dict) = "[object Object]" 显示成乱码。
            # 新 shape: [{"pk": "P001"}, {"pk": "P002"}, ...]
            # 老 shape (兼容读取): [{"pk": {"row_id": "P001", ...}}, ...]——前端兼容路径
            # 见 ViolationSampleTable.extractPkValues。
            sample_pk_values=[{"pk": s.get("row_id")} for s in samples],
            sampling_error=error,  # None = 成功；str = 异常文本（已被 dispatcher 截到 500）
        )
        session.add(row)
        await session.flush()
        return row

    async def listForReport(
        self,
        session: AsyncSession,
        *,
        report_id: int,
        rule_id: int | None = None,
        limit: int = DEFAULT_SAMPLE_LIMIT,
    ) -> list[DataQualityViolationSample]:
        """按 report_id (+ 可选 rule_id) 列出违规样本。

        limit 作用在 sample_pk_values 行级（per-rule），不是行级（per-sample row）。
        """
        stmt = select(DataQualityViolationSample).where(
            DataQualityViolationSample.report_id == report_id,
        )
        if rule_id is not None:
            stmt = stmt.where(DataQualityViolationSample.rule_id == rule_id)
        stmt = stmt.order_by(DataQualityViolationSample.captured_at.desc())
        rows = list((await session.execute(stmt)).scalars().all())
        return rows[: max(1, limit)]

    async def cleanupOrphans(
        self,
        session: AsyncSession,
        *,
        older_than_days: int = 30,
    ) -> int:
        """清掉指向已软删报告的样本行（FK CASCADE 只管硬删；软删需要 worker）。

        当前实现是占位：直接 delete where report_id not in (active report ids)。
        返回删除行数。
        """
        from datetime import UTC, datetime, timedelta

        from app.domain.models import EvaluationReport

        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        active_ids_stmt = select(EvaluationReport.id).where(
            EvaluationReport.deleted_at.is_(None),
        )
        stmt = delete(DataQualityViolationSample).where(
            DataQualityViolationSample.report_id.notin_(active_ids_stmt),
            DataQualityViolationSample.captured_at < cutoff,
        )
        result = await session.execute(stmt)
        await session.commit()
        return int(result.rowcount or 0)


def get_data_quality_violation_sample_service() -> DataQualityViolationSampleService:
    """FastAPI Depends 工厂。"""
    return DataQualityViolationSampleService()
