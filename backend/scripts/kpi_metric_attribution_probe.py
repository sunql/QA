#!/usr/bin/env python3
"""A6：KPI 12 条归因探针（**只读**，不进 CI）。

用途
----
把「治理层现状 / 技术层现状 / 差异归因 / 缺什么」落成可复跑的事实：

1. **对账**：与运行时**同源** —— ``governanceSpecOf`` → ``resolutionForMetric`` →
   ``reconcileMetric``（含别名解析层）。⚠️ 不能只看 ``reconcileMetric`` 的恒等回退：
   关掉 resolution 会忽略治理 token 解析，结论与 chat 路径不一致。
2. **本体覆盖**：技术侧引用到的列（measure.expr / measure.filter / formula /
   filter_expression / grain_keys / time_column）是否都能在源类 ``ontology_property``
   里找到；SQL 引用的表是否都在 ``ontology_metric_source`` 的源类里。
3. **四分类校验**（可选 ``--attribution``）：验收口径要求每条 KPI 落到
   「治理层需改 / 技术层需改 / 仅列名不同 / 真口径分歧」之一 —— 本脚本按给定的
   JSON 校验并渲染成表，缺项/错字直接拒绝（防止文档与实测脱节）。

输出是**事实**；分类是人类判断，写在 ``--attribution`` 给定的 JSON 里。

用法::

    python -m scripts.kpi_metric_attribution_probe --database-url "$QA_METADATA_URL"
    python -m scripts.kpi_metric_attribution_probe --database-url "$QA_METADATA_URL" \\
        --attribution ../docs/update/1118/M0/kpi-attribution.json --json

退出码：0 正常；2 用法错误 / 目标库不在白名单 / ``--attribution`` 校验失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 直接 `python3 scripts/xxx.py` 运行时 sys.path[0] 是 scripts/ ⇒ `app` 不可导入。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlglot  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.domain.models import (  # noqa: E402
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyMetricSource,
    OntologyProperty,
)
from app.services.kpi_metric_reconcile import (  # noqa: E402
    ReconcileResult,
    reconcileMetric,
)
from app.services.kpi_metric_spec import (  # noqa: E402
    governanceSpecOf,
    resolutionForMetric,
    techniqueOf,
)
from scripts.kpi_metric_db_guard import (  # noqa: E402
    ALLOWED_DB_PATTERN,
    databaseNameOf,
    maskDsn,
)

CATEGORIES: tuple[str, ...] = (
    "治理层需改",
    "技术层需改",
    "仅列名不同",
    "真口径分歧",
)

_SQL_DIALECT = "oracle"


# =============================================================================
# 纯函数（单测覆盖）
# =============================================================================
@dataclass(frozen=True)
class MetricRefs:
    """技术形态里**会引用物理列**的全部字段（探针输入，脱离 ORM 可测）。"""

    numerator_measure: object = None
    denominator_measure: object = None
    formula: str | None = None
    filter_expression: str | None = None
    grain_keys: tuple[str, ...] | None = None
    time_column: str | None = None


def _columnNames(text: str) -> set[str] | None:
    """SQL 片段里的列名（大写）；解析失败 ⇒ ``None``（不可判 ≠ 没有列）。"""
    try:
        tree = sqlglot.parse_one(text, read=_SQL_DIALECT)
    except Exception:
        return None
    if tree is None:
        return None
    return {column.name.strip().upper() for column in tree.find_all(sqlglot.exp.Column)}


def _tokensOf(text: object) -> set[str] | None:
    if not isinstance(text, str) or not text.strip():
        return set()
    return _columnNames(text)


def _statementTokens(text: object) -> set[str] | None:
    """``formula`` 是**可执行语句**；中文说明文字虽能被 sqlglot 解析成表达式
    （``口径声明：...`` → ``Div``），但它不是查询 ⇒ ``None``（不可判）。

    这条区分很关键：NONE 类指标的 ``formula`` 存的是中文口径说明，把它当
    SQL 会抽出假列名、并让「不在源类」的空集检查假绿。
    """
    if not isinstance(text, str) or not text.strip():
        return set()
    try:
        tree = sqlglot.parse_one(text, read=_SQL_DIALECT)
    except Exception:
        return None
    if not isinstance(tree, sqlglot.exp.Query):
        return None
    return {column.name.strip().upper() for column in tree.find_all(sqlglot.exp.Column)}


def referenceTokens(metric: MetricRefs) -> set[str] | None:
    """技术侧引用的全部物理列；任一处 SQL 不可解析 ⇒ ``None``。"""
    tokens: set[str] = set()
    for raw in (metric.numerator_measure, metric.denominator_measure):
        if not isinstance(raw, dict):
            continue
        for key in ("expr", "filter"):
            parsed = _tokensOf(raw.get(key))
            if parsed is None:
                return None
            tokens |= parsed
    formulaTokens = _statementTokens(metric.formula)
    if formulaTokens is None:
        return None
    tokens |= formulaTokens
    filterTokens = _tokensOf(metric.filter_expression)
    if filterTokens is None:
        return None
    tokens |= filterTokens
    tokens |= {
        str(value).strip().upper()
        for value in (metric.grain_keys or ())
        if str(value).strip()
    }
    if isinstance(metric.time_column, str) and metric.time_column.strip():
        tokens.add(metric.time_column.strip().upper())
    return tokens


def missingOntologyColumns(
    tokens: set[str] | None, allowed: set[str]
) -> tuple[str, ...] | None:
    """引用列 − 本体物理列；``tokens is None`` ⇒ ``None``（不给空集结论）。"""
    if tokens is None:
        return None
    allowedUpper = {column.strip().upper() for column in allowed}
    return tuple(sorted(token for token in tokens if token.strip().upper() not in allowedUpper))


def assertCategory(value: str) -> str:
    """四分类词表校验（验收口径：每条 KPI 必须落到四类之一）。"""
    if value not in CATEGORIES:
        raise ValueError(f"非法分类 {value!r}，只允许 {CATEGORIES}")
    return value


# =============================================================================
# 读库
# =============================================================================
async def _loadFacts(session: AsyncSession) -> dict[str, Any]:
    kpis = (await session.execute(select(KpiCatalog))).scalars().all()
    metrics = {
        row.id: row for row in (await session.execute(select(OntologyMetric))).scalars().all()
    }
    classes = {
        row.id: row for row in (await session.execute(select(OntologyClass))).scalars().all()
    }
    sourceClassIds: dict[int, list[int]] = {}
    for row in (await session.execute(select(OntologyMetricSource))).scalars().all():
        sourceClassIds.setdefault(row.metric_id, []).append(row.class_id)
    columnsByClass: dict[int, set[str]] = {}
    for row in (await session.execute(select(OntologyProperty))).scalars().all():
        columnsByClass.setdefault(row.class_id, set()).add(
            (row.source_column or "").upper()
        )
    return {
        "kpis": kpis,
        "metrics": metrics,
        "classes": classes,
        "sourceClassIds": sourceClassIds,
        "columnsByClass": columnsByClass,
    }


def _tableNames(metric: OntologyMetric) -> set[str] | None:
    """可执行 SQL 里的表名；无 formula / 解析失败 ⇒ ``None``。"""
    if not isinstance(metric.formula, str) or not metric.formula.strip():
        return None
    try:
        tree = sqlglot.parse_one(metric.formula, read=_SQL_DIALECT)
    except Exception:
        return None
    if not isinstance(tree, sqlglot.exp.Query):
        return None  # 中文口径说明 / 表达式片段不是可执行语句
    return {table.name.strip().upper() for table in tree.find_all(sqlglot.exp.Table)}


def _refsOf(metric: OntologyMetric) -> MetricRefs:
    return MetricRefs(
        numerator_measure=metric.numerator_measure,
        denominator_measure=metric.denominator_measure,
        formula=metric.formula,
        filter_expression=metric.filter_expression,
        grain_keys=tuple(metric.grain_keys or ()),
        time_column=metric.time_column,
    )


def _technicalFacts(
    metric: OntologyMetric | None, sourceIds: list[int], facts: dict[str, Any]
) -> dict[str, Any]:
    """技术层现状 + 本体覆盖（引用列 − 源类物理列）。"""
    referenced = referenceTokens(_refsOf(metric)) if metric is not None else None
    allowed: set[str] = set()
    for cid in sourceIds:
        allowed |= facts["columnsByClass"].get(cid, set())
    return {
        "numerator": metric.numerator_measure if metric is not None else None,
        "denominator": metric.denominator_measure if metric is not None else None,
        "grainKeys": list(metric.grain_keys or []) if metric is not None else [],
        "timeGrain": metric.time_grain if metric is not None else None,
        "timeColumn": metric.time_column if metric is not None else None,
        "referenceColumns": len(referenced) if referenced is not None else None,
        "missingColumns": (
            None if referenced is None else list(missingOntologyColumns(referenced, allowed) or ())
        ),
    }


def _reconcileFacts(reconcile: ReconcileResult | None) -> dict[str, Any] | None:
    if reconcile is None:
        return None
    return {
        "verdict": reconcile.verdict,
        "degraded": reconcile.degraded,
        "tierA": reconcile.isTierAEligible,
        "findings": [
            {
                "layer": finding.layer,
                "reason": finding.reason,
                "verdict": finding.verdict,
                "detail": finding.detail,
            }
            for finding in reconcile.findings
        ],
    }


async def _factsOfMetric(
    session: AsyncSession, kpi: KpiCatalog, metric: OntologyMetric | None, facts: dict[str, Any]
) -> dict[str, Any]:
    governance = governanceSpecOf(kpi)
    resolution = (
        await resolutionForMetric(session, metric.id, governance) if metric is not None else None
    )
    reconcile = (
        reconcileMetric(governance, techniqueOf(metric), resolution=resolution)
        if metric is not None
        else None
    )
    sourceIds: list[int] = (
        list(facts["sourceClassIds"].get(metric.id, [])) if metric is not None else []
    )
    return {
        "kpiCode": str(kpi.kpi_code),
        "metricId": kpi.metric_id,
        "executionSource": (metric.execution_source if metric is not None else None),
        "governance": {
            "numeratorColumns": list(governance.numerator_columns),
            "denominatorColumns": list(governance.denominator_columns),
            "grainKeys": list(governance.grain_keys),
            "timeGrain": governance.time_grain,
        },
        "technical": _technicalFacts(metric, sourceIds, facts),
        "resolution": None if resolution is None else {
            "resolved": [list(pair) for pair in resolution.resolved],
            "unknownTokens": list(resolution.unknownTokens),
            "missingTokens": list(resolution.missingTokens),
        },
        "reconcile": _reconcileFacts(reconcile),
        "sourceClasses": [facts["classes"][cid].class_name for cid in sourceIds],
        "sqlTablesOutsideSources": _tablesOutsideSources(metric, sourceIds, facts),
    }


def _tablesOutsideSources(
    metric: OntologyMetric | None, sourceIds: list[int], facts: dict[str, Any]
) -> list[str] | None:
    """可执行 SQL 引用了、但不在 ``ontology_metric_source`` 源类里的表。

    ⚠️ 无 SQL / 解析失败 ⇒ ``None``（不可判）。**不能**把 ``None`` 折成空集 ——
    NONE 类指标的 ``formula`` 是中文说明文字，折成空集会渲染成「无越界表」，
    即「没检查 = 通过」（本仓反复踩过的假绿）。
    """
    tables = _tableNames(metric) if metric is not None else None
    if tables is None:
        return None
    known = {
        name
        for cid in sourceIds
        for name in (
            facts["classes"][cid].class_name.upper(),
            (facts["classes"][cid].source_table or "").upper(),
        )
    }
    return sorted(tables - known)


# =============================================================================
# 渲染 / CLI
# =============================================================================
def _render(rows: list[dict[str, Any]], attribution: dict[str, dict[str, str]] | None) -> str:
    lines: list[str] = []
    for row in rows:
        reconcile = row["reconcile"] or {}
        governance, technical = row["governance"], row["technical"]
        lines.append(
            f"== {row['kpiCode']} (metric={row['metricId']}, source={row['executionSource']})"
        )
        lines.append(
            f"   治理: num={governance['numeratorColumns']} den={governance['denominatorColumns']} "
            f"grain={governance['grainKeys']} timeGrain={governance['timeGrain']}"
        )
        lines.append(
            f"   技术: grain={technical['grainKeys']} timeGrain={technical['timeGrain']} "
            f"timeColumn={technical['timeColumn']} 引用列={technical['referenceColumns']}"
        )
        lines.append(f"   源类: {row['sourceClasses']}")
        resolution = row["resolution"]
        if resolution is not None:
            lines.append(
                f"   解析: resolved={len(resolution['resolved'])} "
                f"unknown={resolution['unknownTokens']} missing={resolution['missingTokens']}"
            )
        if reconcile:
            lines.append(
                f"   对账: verdict={reconcile['verdict']} degraded={reconcile['degraded']} "
                f"tierA={reconcile['tierA']}"
            )
            for finding in reconcile["findings"]:
                lines.append(
                    f"     - [{finding['layer']}/{finding['verdict']}] "
                    f"{finding['reason']}: {finding['detail']}"
                )
        missing = technical["missingColumns"]
        coverage = (
            "不可判（SQL 解析失败）" if missing is None else f"缺列={missing or '无'}"
        )
        outside = row["sqlTablesOutsideSources"]
        outsideText = (
            "不可判（无 SQL 或解析失败）" if outside is None else f"{outside or '无'}"
        )
        lines.append(f"   本体覆盖: {coverage} | SQL 表不在源类={outsideText}")
        if attribution is not None:
            entry = attribution[row["kpiCode"]]
            lines.append(f"   分类: {entry['category']} —— {entry.get('note', '')}")
        lines.append("")
    return "\n".join(lines)


def _loadAttribution(path: Path, kpiCodes: set[str]) -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"归因文件读取失败：{path}（{exc}）") from None
    if not isinstance(raw, dict) or not raw:
        raise SystemExit("归因文件必须是 {kpi_code: {category, note}} 的非空对象")
    missing = sorted(kpiCodes - set(raw))
    unknown = sorted(set(raw) - kpiCodes)
    if missing or unknown:
        raise SystemExit(f"归因文件与库内 KPI 不一致：缺 {missing} / 多 {unknown}")
    result: dict[str, dict[str, str]] = {}
    for code, entry in raw.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("category"), str):
            raise SystemExit(f"{code}: 缺 category")
        try:
            category = assertCategory(entry["category"])
        except ValueError as exc:
            raise SystemExit(f"{code}: {exc}") from None
        result[code] = {"category": category, "note": str(entry.get("note") or "")}
    return result


async def _run(args: argparse.Namespace) -> int:
    databaseName = databaseNameOf(args.database_url)
    if not ALLOWED_DB_PATTERN.match(databaseName):
        raise SystemExit(
            f"拒绝执行：目标库 {databaseName!r} 不在白名单内"
            "（只允许 qa_metadata / qa_metadata_test*）。"
        )
    print(f"目标库 DSN = {maskDsn(args.database_url)}（只读）")
    engine = create_async_engine(args.database_url)
    sessionFactory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionFactory() as session:
            facts = await _loadFacts(session)
            rows = [
                await _factsOfMetric(
                    session, kpi, facts["metrics"].get(kpi.metric_id), facts
                )
                for kpi in sorted(facts["kpis"], key=lambda row: str(row.kpi_code))
            ]
    finally:
        await engine.dispose()

    attribution = (
        _loadAttribution(Path(args.attribution), {row["kpiCode"] for row in rows})
        if args.attribution
        else None
    )
    if args.json:
        print(json.dumps({"kpis": rows, "attribution": attribution}, ensure_ascii=False, indent=2))
    else:
        print(_render(rows, attribution))
    if attribution is not None:
        counts: dict[str, int] = {}
        for entry in attribution.values():
            counts[entry["category"]] = counts.get(entry["category"], 0) + 1
        print("分类计数：" + " / ".join(f"{key}={counts.get(key, 0)}" for key in CATEGORIES))
    return 0


def _parseArgs(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="KPI 归因探针（只读）")
    parser.add_argument("--database-url", required=True, help="元数据库 DSN（勿写进证据文件）")
    parser.add_argument("--attribution", help="四分类 JSON 路径（验收口径校验）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本表")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(_run(_parseArgs(sys.argv[1:] if argv is None else argv)))
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return 2
        return int(exc.code)


if __name__ == "__main__":
    raise SystemExit(main())
