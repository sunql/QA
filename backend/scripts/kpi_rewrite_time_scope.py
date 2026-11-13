"""P4a 存量改写 dry-run 报告（只读；无 --apply 模式）。

建议规则（D5 裁决）在 :mod:`app.services.kpi_time_scope_rewrite`（纯函数）；
本脚本只负责读库、打印报告。写入一律走 admin 指标编辑表单（走既有审计）。

用法::

    python3 scripts/kpi_rewrite_time_scope.py \
        --database-url 'postgresql+asyncpg://.../qa_metadata'   # dry-run 报告
    python3 scripts/kpi_rewrite_time_scope.py --database-url ... --output report.json

退出码：0 正常；1 参数/连接失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# 直接 `python3 scripts/xxx.py` 运行时 sys.path[0] 是 scripts/ ⇒ `app` 不可导入
# （见 kpi_backfill_time_grain.py 同款注释：editable 安装的旧副本遮蔽陷阱）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.services.kpi_time_scope_rewrite import buildTimeScopeRewriteSuggestions  # noqa: E402
from scripts.kpi_metric_db_guard import assertSafeTarget, maskDsn  # noqa: E402

_SELECT_METRICS = (
    "SELECT id, metric_name, execution_source, formula, time_column, status "
    "FROM ontology_metric ORDER BY id"
)


async def _run(databaseUrl: str, outputPath: str | None) -> int:
    print(f"目标库: {maskDsn(databaseUrl)}")
    # 只读 dry-run（本脚本无 --apply，全程零写入）：按 kpi_backfill_time_grain.py
    # 同款先例（allowProdWrite = ... or not apply），read-only 目标允许 qa_metadata
    # —— 此处 allowProdWrite=True 仅用于通过白名单/正式库闸门，不代表有任何写入。
    assertSafeTarget(databaseUrl, allowProdWrite=True)
    engine = create_async_engine(databaseUrl)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(_SELECT_METRICS))).mappings().all()
    finally:
        await engine.dispose()
    suggestions = buildTimeScopeRewriteSuggestions(list(rows))
    counts: dict[str, int] = {}
    for s in suggestions:
        counts[s.action] = counts.get(s.action, 0) + 1
        print(f"[{s.action.upper():7}] metric={s.metricId} {s.metricName} col={s.timeColumn}")
        if s.suggestedFormula:
            print(f"    建议: {s.suggestedFormula}")
        if s.reason:
            print(f"    说明: {s.reason}")
    print(f"\n汇总: {counts}（共 {len(suggestions)} 条 ESCAPE_HATCH 指标；dry-run 零写入）")
    if outputPath:
        with open(outputPath, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "metricId": s.metricId, "metricName": s.metricName,
                        "executionSource": s.executionSource, "formula": s.formula,
                        "timeColumn": s.timeColumn, "action": s.action,
                        "reason": s.reason, "suggestedFormula": s.suggestedFormula,
                    }
                    for s in suggestions
                ],
                fh, ensure_ascii=False, indent=2,
            )
        print(f"报告已写入 {outputPath}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True, help="目标库 DSN（只读连接）")
    parser.add_argument("--output", default=None, help="JSON 报告输出路径")
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args.database_url, args.output))
    except Exception as exc:  # noqa: BLE001 — CLI 边界，显式报错退出
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
