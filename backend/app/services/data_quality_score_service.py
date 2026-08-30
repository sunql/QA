"""数据质量评分 service（Phase 1.3）。

把 Phase 1.2 dispatcher 返回的 EvaluationResult 按 target_table 聚合成 6 维评分
并落库；同时支持列表查询（含 latest / score_type 过滤）。

设计要点：
- 复用 dispatcher.evaluateBatch，不重起 SQL 拼接
- 通过 rule_id 反查 DataQualityRule.target_table 拿到聚合 key
- 5 维（COMPLETENESS / VALIDITY / UNIQUENESS / CONSISTENCY / REFERENTIAL）
  按 rule_type 填对应列；TIMELINESS 留 NULL（Phase 2 补）
- overall_score = 非 NULL 维度的算术平均；维度缺失不拉低整体分
- GLOBAL 行：跨所有 enabled rule 求 mean
- 每次 compute 写 N+1 条（每个 distinct target_table 一条 + 一条 GLOBAL）
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import bindparam, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import RuleType, ScoreType
from app.domain.exceptions import ValidationError
from app.domain.models import DataQualityRule, DataQualityScore
from app.domain.schemas import (
    ComputeScoresResponse,
    DataQualityBadge,
    DataQualityScoreRead,
    EvaluationResult,
)
from app.services.data_quality_evaluator import DataQualityEvaluatorDispatcher
from app.services.data_quality_evaluators._common import validate_identifier

logger = logging.getLogger(__name__)

# rule_type -> score column 名
_RULE_TYPE_TO_COLUMN: dict[RuleType, str] = {
    RuleType.COMPLETENESS: "completeness_score",
    RuleType.VALIDITY: "validity_score",
    RuleType.UNIQUENESS: "uniqueness_score",
    RuleType.CONSISTENCY: "consistency_score",
    RuleType.REFERENTIAL: "referential_score",
    # TIMELINESS 暂无 evaluator，列保持 NULL
}

# 6 维评分列（含 timeliness 留空 NULL）；与 DataQualityScore 表字段一一对应
_SCORE_FIELDS: tuple[str, ...] = (
    "completeness_score",
    "validity_score",
    "uniqueness_score",
    "consistency_score",
    "timeliness_score",
    "referential_score",
)

# 计算 overall 时纳入的维度（timeliness 暂无 evaluator，暂不计入）
_DIMENSION_FIELDS: tuple[str, ...] = (
    "completeness_score",
    "validity_score",
    "uniqueness_score",
    "consistency_score",
    "referential_score",
)

_COLUMN_TO_RULE_TYPE: dict[str, RuleType] = {v: k for k, v in _RULE_TYPE_TO_COLUMN.items()}


class DataQualityScoreService:
    """数据质量评分聚合 + 落库 + 列表查询。"""

    def __init__(self, dispatcher: DataQualityEvaluatorDispatcher | None = None) -> None:
        self._dispatcher = dispatcher or DataQualityEvaluatorDispatcher()

    async def computeScores(self, session: AsyncSession) -> ComputeScoresResponse:
        """拉所有 enabled rule → 批量评估 → 按表聚合 + GLOBAL → 落库 → 返回。

        空 enabled rule 集合：直接返回空 response，不写库。
        """
        started = time.perf_counter()
        enabled_rules = await self._listEnabledRules(session)
        if not enabled_rules:
            return ComputeScoresResponse(
                evaluated_rules=0, saved_scores=0, duration_ms=0, scores=[]
            )
        rule_ids = [r.id for r in enabled_rules]
        batch = await self._dispatcher.evaluateBatch(session, rule_ids)
        # 通过 rule_id 反查 target_table / rule_type
        rule_by_id = {r.id: r for r in enabled_rules}
        aggregated = self._aggregate(batch.results, rule_by_id)
        saved_entities = self._buildEntities(aggregated, started)
        session.add_all(saved_entities)
        await session.commit()
        for entity in saved_entities:
            await session.refresh(entity)
        duration_ms = int((time.perf_counter() - started) * 1000)
        return ComputeScoresResponse(
            evaluated_rules=len(rule_ids),
            saved_scores=len(saved_entities),
            duration_ms=duration_ms,
            scores=[_scoreToRead(s) for s in saved_entities],
        )

    async def listScores(
        self,
        session: AsyncSession,
        *,
        target_table: str | None = None,
        score_type: ScoreType | None = None,
        latest: bool = False,
        limit: int = 100,
    ) -> list[DataQualityScore]:
        """列表查询。

        - latest=true 时按 (target_table, score_type) 取最新一条
        - 默认按 evaluated_at DESC 倒序
        - limit 默认 100，上限 1000
        """
        if latest:
            return await self._listLatest(session, target_table, score_type)
        stmt = select(DataQualityScore).order_by(DataQualityScore.evaluated_at.desc())
        if target_table is not None:
            stmt = stmt.where(DataQualityScore.target_table == target_table)
        if score_type is not None:
            stmt = stmt.where(DataQualityScore.score_type == score_type)
        stmt = stmt.limit(max(1, min(limit, 1000)))
        return list((await session.execute(stmt)).scalars().all())

    async def getLatestTableScores(
        self,
        session: AsyncSession,
        target_tables: tuple[str, ...],
    ) -> dict[str, DataQualityBadge | None]:
        """批量获取每张目标表最新的 TABLE 分数（chat badge 用）。

        行为契约：
        - 输入去重 + 去空字符串后为空 → 返回空 dict（不查 DB）。
        - 任一元素不是合法 SQL identifier → 抛 ValidationError（caller 编程错误，
          不静默吞；与 DB 异常明确区分）。
        - DB 异常（连接/超时等）→ 返回空 dict + 记 WARN 日志（chat 主链路不挂）。
        - 评估过的表 → {table: DataQualityBadge(evaluated=True, ...)}。
        - 未评估过的表 → {table: DataQualityBadge(evaluated=False, 其余 None)}。
        - 返回 dict 的 key 集合 = 输入 tuple 去重 + 去空 的集合（保序）。

        实现要点：
        - 单条 SQL：target_table IN (:tables) + ROW_NUMBER 按表取最新 evaluated_at。
        - IN 子句用 sqlalchemy.bindparam(expanding=True) 参数化（防注入）。
        """
        # 1. 输入归一化：去空 + 去重保序
        seen: set[str] = set()
        cleaned: list[str] = []
        for t in target_tables:
            if not t or not t.strip():
                continue
            if t in seen:
                continue
            seen.add(t)
            cleaned.append(t)
        if not cleaned:
            return {}

        # 2. identifier 校验 —— 任一非法直接抛错（不静默）
        for t in cleaned:
            validate_identifier(t, role="table")

        # 3. 单条 SQL：IN + ROW_NUMBER 取每张表最新
        try:
            row_num = (
                func.row_number()
                .over(
                    partition_by=DataQualityScore.target_table,
                    order_by=DataQualityScore.evaluated_at.desc(),
                )
                .label("rn")
            )
            inner = (
                select(
                    DataQualityScore.id,
                    DataQualityScore.target_table,
                    DataQualityScore.overall_score,
                    DataQualityScore.evaluated_at,
                    DataQualityScore.rules_count,
                    row_num,
                )
                .where(DataQualityScore.score_type == ScoreType.TABLE)
                .where(DataQualityScore.target_table.in_(bindparam("tables", expanding=True)))
                .subquery()
            )
            stmt = select(
                inner.c.target_table,
                inner.c.overall_score,
                inner.c.evaluated_at,
                inner.c.rules_count,
            ).where(inner.c.rn == 1)
            result = await session.execute(stmt, {"tables": cleaned})
            rows = result.all()
        except Exception as exc:  # noqa: BLE001 —— 静默降级日志告警
            logger.warning(
                "DQ 评分查询失败（chat 注入路径）: tables=%s exc=%s",
                cleaned,
                exc,
            )
            return {}

        # 4. 行 → dict[table, badge]；未评估的表补 evaluated=False
        out: dict[str, DataQualityBadge | None] = {
            t: DataQualityBadge(
                target_table=t,
                overall_score=None,
                evaluated_at=None,
                rules_count=None,
                evaluated=False,
            )
            for t in cleaned
        }
        for row in rows:
            # row 可能是 Row 对象或 tuple；统一用 _mapping 风格访问
            row_map = row._mapping if hasattr(row, "_mapping") else row
            table = row_map["target_table"]
            out[table] = DataQualityBadge(
                target_table=table,
                overall_score=row_map["overall_score"],
                evaluated_at=row_map["evaluated_at"],
                rules_count=row_map["rules_count"],
                evaluated=True,
            )
        return out

    # ===== 内部 =====

    async def _listEnabledRules(self, session: AsyncSession) -> list[DataQualityRule]:
        stmt = (
            select(DataQualityRule)
            .where(DataQualityRule.is_enabled.is_(True))
            .order_by(DataQualityRule.id)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def _listLatest(
        self,
        session: AsyncSession,
        target_table: str | None,
        score_type: ScoreType | None,
    ) -> list[DataQualityScore]:
        """latest=true：用 ROW_NUMBER() 按 (target_table, score_type) 取最新一条。

        实现：用 correlated subquery 取每个分组 rn=1 的 id，再 IN 选出全行。
        这样既不引入 CROSS JOIN 又简洁。
        """
        # 分组列
        partition_cols: list = []
        if target_table is None and score_type is None:
            partition_cols = [DataQualityScore.target_table, DataQualityScore.score_type]
        elif target_table is not None and score_type is not None:
            partition_cols = []  # 双过滤已确定唯一 (target_table, score_type)，保留全表最新
        elif target_table is not None:
            partition_cols = [DataQualityScore.score_type]
        else:
            partition_cols = [DataQualityScore.target_table]

        # 构造 OVER 子句
        row_num = (
            func.row_number()
            .over(
                partition_by=partition_cols or [DataQualityScore.id],
                order_by=DataQualityScore.evaluated_at.desc(),
            )
            .label("rn")
        )
        inner = select(DataQualityScore.id, row_num)
        if target_table is not None:
            inner = inner.where(DataQualityScore.target_table == target_table)
        if score_type is not None:
            inner = inner.where(DataQualityScore.score_type == score_type)
        subq = inner.subquery()
        # 仅选 rn=1 的 id
        latest_ids = select(subq.c.id).where(subq.c.rn == 1)
        stmt = select(DataQualityScore).where(DataQualityScore.id.in_(latest_ids))
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    def _aggregate(
        results: list[EvaluationResult],
        rule_by_id: dict[int, DataQualityRule],
    ) -> list[dict]:
        """把 EvaluationResult 列表聚合为每表 + GLOBAL 的 6 维分数字典。

        错误状态（status=ERROR）跳过：异常结果不应污染聚合分母。
        """
        per_table: dict[str, dict[RuleType, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for r in results:
            if r.status == "ERROR":
                continue
            rule = rule_by_id.get(r.rule_id)
            if rule is None:
                continue
            try:
                rt = RuleType(r.rule_type)
            except (ValueError, KeyError):
                continue
            per_table[rule.target_table][rt].append(float(r.pass_rate))

        aggregated: list[dict] = []
        for table, by_type in per_table.items():
            aggregated.append(_buildScoreDict(table, ScoreType.TABLE, by_type))
        # GLOBAL：拍平所有 per_table 的 by_type
        flat: dict[RuleType, list[float]] = defaultdict(list)
        for by_type in per_table.values():
            for rt, rates in by_type.items():
                flat[rt].extend(rates)
        if flat:
            aggregated.append(
                _buildScoreDict(
                    "*", ScoreType.GLOBAL, flat, rules_count=len(results)
                )
            )
        return aggregated

    @staticmethod
    def _buildEntities(aggregated: list[dict], started: float) -> list[DataQualityScore]:
        """把聚合字典转 ORM 实体。"""
        duration_ms = int((time.perf_counter() - started) * 1000)
        evaluated_at = datetime.now(timezone.utc)
        entities: list[DataQualityScore] = []
        for s in aggregated:
            entities.append(
                DataQualityScore(
                    target_table=s["target_table"],
                    score_type=s["score_type"],
                    completeness_score=s.get("completeness_score"),
                    validity_score=s.get("validity_score"),
                    uniqueness_score=s.get("uniqueness_score"),
                    consistency_score=s.get("consistency_score"),
                    timeliness_score=None,  # Phase 2 补
                    referential_score=s.get("referential_score"),
                    overall_score=s["overall_score"],
                    evaluated_at=evaluated_at,
                    evaluation_duration_ms=duration_ms,
                    rules_count=s["rules_count"],
                )
            )
        return entities


# ===== 模块级辅助函数 =====


def _buildScoreDict(
    target_table: str,
    score_type: ScoreType,
    by_type: dict[RuleType, list[float]],
    rules_count: int | None = None,
) -> dict:
    """构造单条 score 字典。

    每维是 rule_type 下所有 pass_rate 的算术平均；overall = 非 NULL 维度的平均。
    """
    out: dict = {
        "target_table": target_table,
        "score_type": score_type,
    }
    # 先把 6 维都填上默认值 None，保证字典形状稳定
    for col in _SCORE_FIELDS:
        out[col] = None
    non_null: list[float] = []
    # 仅对 _DIMENSION_FIELDS 里的维度计算均值并参与 overall
    for col in _DIMENSION_FIELDS:
        rt = _COLUMN_TO_RULE_TYPE.get(col)
        if rt is None:
            continue
        rates = by_type.get(rt, [])
        if rates:
            avg = sum(rates) / len(rates)
            out[col] = Decimal(f"{avg:.2f}")
            non_null.append(avg)
    if non_null:
        out["overall_score"] = Decimal(f"{sum(non_null) / len(non_null):.2f}")
    else:
        # 没有任何维度可用 → 整体 0
        out["overall_score"] = Decimal("0.00")
    out["rules_count"] = (
        rules_count if rules_count is not None else sum(len(v) for v in by_type.values())
    )
    return out


def _scoreToRead(score: DataQualityScore) -> DataQualityScoreRead:
    """ORM → Read DTO。"""
    return DataQualityScoreRead.model_validate(score, from_attributes=True)