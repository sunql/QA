"""数据质量评估 dispatcher（Phase 1.2）。

按 rule.rule_type 路由到对应 evaluator，对外只暴露 evaluate / evaluateBatch 两个
入口。所有 SQL 走 BusinessDbAdapter.execute_read_only，自动享受只读护栏 + 行数 + 超时。
datasource_id 来自规则定义本身，由 service 调用方传入或读取；本期选择「规则绑定数据源」
（datasource_id 字段已加入 data_quality_rule，迁移 0018）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import RuleType
from app.domain.exceptions import NotFoundError
from app.domain.models import DataQualityRule, DataSource
from app.domain.schemas import (
    EvaluateBatchResponse,
    EvaluationResult,
)
from app.infrastructure.business_db_pool import (
    BusinessDbAdapter,
    get_adapter,
)
from app.services.data_quality_evaluators.completeness import evaluate as completeness_evaluate
from app.services.data_quality_evaluators.consistency import evaluate as consistency_evaluate
from app.services.data_quality_evaluators.referential import evaluate as referential_evaluate
from app.services.data_quality_evaluators.uniqueness import evaluate as uniqueness_evaluate
from app.services.data_quality_evaluators.validity import evaluate as validity_evaluate
from app.services.messages_zh import (
    MSG_DQ_EVAL_DATASOURCE_NOT_FOUND,
    MSG_DQ_EVAL_RULE_TYPE_UNSUPPORTED,
)

# 评估器签名：async (rule, adapter) -> (total, passed)
EvaluatorFn = Callable[[DataQualityRule, BusinessDbAdapter], "Any"]

_EVALUATORS: dict[RuleType, EvaluatorFn] = {
    RuleType.COMPLETENESS: completeness_evaluate,
    RuleType.VALIDITY: validity_evaluate,
    RuleType.UNIQUENESS: uniqueness_evaluate,
    RuleType.CONSISTENCY: consistency_evaluate,
    RuleType.REFERENTIAL: referential_evaluate,
}


class DataQualityEvaluatorDispatcher:
    """数据质量评估调度器。

    用法：
        dispatcher = DataQualityEvaluatorDispatcher()
        result = await dispatcher.evaluate(session, rule_id)
        batch = await dispatcher.evaluateBatch(session, [1, 2, 3])
    """

    async def evaluate(
        self,
        session: AsyncSession,
        rule_id: int,
    ) -> EvaluationResult:
        """评估单条规则。返回 EvaluationResult（status=PASS/FAIL/ERROR）。"""
        rule = await session.get(DataQualityRule, rule_id)
        if rule is None:
            raise NotFoundError(f"数据质量规则 id={rule_id} 不存在")
        return await self._evaluateOne(session, rule)

    async def evaluateBatch(
        self,
        session: AsyncSession,
        rule_ids: list[int],
    ) -> EvaluateBatchResponse:
        """批量评估：返回 results + summary（passed 数）。"""
        if not rule_ids:
            return EvaluateBatchResponse(results=[], summary_total=0, summary_passed=0)
        stmt = select(DataQualityRule).where(DataQualityRule.id.in_(rule_ids))
        rows = (await session.execute(stmt)).scalars().all()
        rules_by_id = {r.id: r for r in rows}
        # 缺失的 rule_id 直接当作 ERROR，不抛异常，避免部分失败连累整批
        results: list[EvaluationResult] = []
        for rid in rule_ids:
            rule = rules_by_id.get(rid)
            if rule is None:
                results.append(self._errorResult(rid, f"规则 id={rid} 不存在"))
                continue
            results.append(await self._evaluateOne(session, rule))
        return EvaluateBatchResponse(
            results=results,
            summary_total=len(results),
            summary_passed=sum(1 for r in results if r.status == "PASS"),
        )

    async def _evaluateOne(
        self,
        session: AsyncSession,
        rule: DataQualityRule,
    ) -> EvaluationResult:
        started = time.perf_counter()
        try:
            try:
                rule_type_enum = RuleType(rule.rule_type)
            except (ValueError, KeyError):
                return self._errorResult(
                    rule.id,
                    MSG_DQ_EVAL_RULE_TYPE_UNSUPPORTED.format(ruleType=rule.rule_type),
                )
            evaluator = _EVALUATORS.get(rule_type_enum)
            if evaluator is None:
                return self._errorResult(
                    rule.id,
                    MSG_DQ_EVAL_RULE_TYPE_UNSUPPORTED.format(ruleType=rule.rule_type),
                )
            ds = await session.get(DataSource, rule.datasource_id)
            if ds is None:
                return self._errorResult(
                    rule.id,
                    MSG_DQ_EVAL_DATASOURCE_NOT_FOUND.format(ruleId=rule.id),
                )
            adapter = get_adapter(rule.datasource_id, ds)
            total, passed = await evaluator(rule, adapter)
            duration_ms = int((time.perf_counter() - started) * 1000)
            rate = (passed / total * 100.0) if total > 0 else 0.0
            status = "PASS" if rate >= float(rule.threshold) else "FAIL"
            return EvaluationResult(
                rule_id=rule.id,
                rule_code=rule.rule_code,
                rule_type=_safeRuleType(rule.rule_type),
                datasource_id=rule.datasource_id,
                total_count=total,
                passed_count=passed,
                pass_rate=round(rate, 4),
                status=status,
                evaluated_at=_resolveEvaluatedAt(rule),
                duration_ms=duration_ms,
                message=None,
            )
        except Exception as exc:  # noqa: BLE001 - 评估失败要带回 message 给前端
            duration_ms = int((time.perf_counter() - started) * 1000)
            return EvaluationResult(
                rule_id=rule.id,
                rule_code=getattr(rule, "rule_code", "") or "",
                rule_type=_safeRuleType(getattr(rule, "rule_type", RuleType.COMPLETENESS)),
                datasource_id=getattr(rule, "datasource_id", None),
                total_count=0,
                passed_count=0,
                pass_rate=0.0,
                status="ERROR",
                evaluated_at=_resolveEvaluatedAt(rule),
                duration_ms=duration_ms,
                message=str(exc)[:500],
            )

    @staticmethod
    def _errorResult(rule_id: int, message: str) -> EvaluationResult:
        """构造一条 ERROR 结果（用于规则缺失 / 类型不支持 / 数据源缺失等）。"""
        return EvaluationResult(
            rule_id=rule_id,
            rule_code="",
            rule_type=RuleType.COMPLETENESS,
            datasource_id=None,
            total_count=0,
            passed_count=0,
            pass_rate=0.0,
            status="ERROR",
            evaluated_at=datetime.now(UTC),
            duration_ms=0,
            message=message,
        )


def _utcnow():
    """评估时间戳占位，避免外部循环依赖 datetime；调用方拿到即可转 ISO。"""
    return datetime.now(UTC)


def _safeRuleType(value: Any) -> RuleType:
    """把任意字符串/枚举转成 RuleType，失败返回 COMPLETENESS（兜底不让 Pydantic 抛错）。"""
    if isinstance(value, RuleType):
        return value
    try:
        return RuleType(str(value))
    except (ValueError, KeyError):
        return RuleType.COMPLETENESS


def _resolveEvaluatedAt(rule: Any) -> datetime:
    """取规则 updated_time；缺失则取当前 UTC。"""
    val = getattr(rule, "updated_time", None)
    if isinstance(val, datetime):
        return val
    return _utcnow()