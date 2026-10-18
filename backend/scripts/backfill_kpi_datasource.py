"""KPI → 数据源回填工具（P0）。

**背景**：F2 起新建 KPI 必须绑定数据源，但存量 ``kpi_catalog.datasource_id``
大多为 NULL（列是 F2 才接入写入流的）。本工具负责把历史行补绑：

- **预览（默认，绝不写库）**：列出全部未绑定 KPI + 建议数据源
  （建议来自 ``KpiDatasourceSuggestionService``，与编辑页提示同一实现）。
- **按映射落库**：``--mapping m.csv --apply``，CSV 表头 ``kpi_code,datasource``；
  ``datasource`` 可填数据源 ID 或名称（名称先解析成 ID，找不到 → 该行 failed）。
- 落库复用 ``KpiDatasourceBindingService.bulkBind``：逐行 commit、bound /
  skipped / failed 逐行交代，与 admin API 完全同一写路径（校验 + 审计 + history）。

幂等：二次执行已绑定的行全部 ``skipped``，不产生 revision/审计噪声。

用法（容器内 ``DATABASE_URL`` 已注入）::

    uv run python scripts/backfill_kpi_datasource.py                  # 预览
    uv run python scripts/backfill_kpi_datasource.py --mapping m.csv  # 仍只预览
    uv run python scripts/backfill_kpi_datasource.py --mapping m.csv --apply

退出码：存在 failed 行（含 CSV 里无法解析的数据源）→ 1；否则 0。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.models import DataSource
from app.domain.schemas import KpiDatasourceBulkBindItem
from app.infrastructure.database import getSessionFactory
from app.services.kpi_catalog_service import KpiCatalogService
from app.services.kpi_datasource_binding_service import KpiDatasourceBindingService
from app.services.kpi_datasource_suggestion_service import (
    KpiDatasourceSuggestionService,
)

_CSV_HEADER = ("kpi_code", "datasource")
_ACTOR = CurrentUser(
    userId="backfill-kpi-datasource", roles=("admin",), departments=("IT",)
)


@dataclass(frozen=True)
class MappingRow:
    row: int
    kpi_code: str
    datasource: str


@dataclass(frozen=True)
class PreviewRow:
    kpi_code: str
    kpi_name: str
    legacy_source: str | None
    suggestion_name: str | None
    suggestion_reason: str | None


def parseMappingCsv(path: Path) -> list[MappingRow]:
    """解析映射 CSV；表头非法或行为空时抛 ValueError。行号从 2 起（表头是 1）。"""
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None or tuple(h.strip() for h in header) != _CSV_HEADER:
            raise ValueError(
                f"CSV 表头必须是 {','.join(_CSV_HEADER)}，实际 {header!r}"
            )
        rows: list[MappingRow] = []
        for idx, raw in enumerate(reader, start=2):
            if not any(cell.strip() for cell in raw):
                continue
            if len(raw) < 2:
                raise ValueError(f"第 {idx} 行缺少 datasource 列：{raw!r}")
            rows.append(MappingRow(idx, raw[0].strip(), raw[1].strip()))
    return rows


async def buildPreview(session: AsyncSession) -> list[PreviewRow]:
    """未绑定 KPI + 首条建议（只读）。"""
    catalog = KpiCatalogService()
    suggestions = KpiDatasourceSuggestionService()
    rows: list[PreviewRow] = []
    for kpi in await catalog.listKpis(session, unbound=True):
        top = (await suggestions.suggest(session, kpi.id) or [None])[0]
        rows.append(
            PreviewRow(
                kpi_code=kpi.kpi_code,
                kpi_name=kpi.kpi_name,
                legacy_source=kpi.data_source,
                suggestion_name=top.name if top else None,
                suggestion_reason=top.reasons[0] if top else None,
            )
        )
    return rows


def _uniqueIndex(sources: list[DataSource], normalize: Any) -> dict[str, int]:
    """名称 → id，但**只保留唯一命中**；重名一律剔除（歧义不猜）。"""
    hits: dict[str, list[int]] = {}
    for ds in sources:
        hits.setdefault(normalize(ds.name), []).append(ds.id)
    return {key: ids[0] for key, ids in hits.items() if len(ids) == 1}


async def _resolveDatasources(
    session: AsyncSession, rows: list[MappingRow]
) -> dict[str, int]:
    """datasource 列（ID 或名称）→ data_source.id；仅收录可无歧义解析的键。

    解析优先级：ID → 名称精确（去首尾空白）→ 名称大小写不敏感。
    同名启用数据源多于一个时视为歧义、不解析（该行 failed，提示改用 ID），
    避免把 KPI 绑到猜测的某个同名源上。
    """
    wanted = {r.datasource for r in rows}
    sources = list(
        (
            await session.execute(
                select(DataSource).where(DataSource.is_active.is_(True))
            )
        ).scalars().all()
    )
    byId = {str(ds.id): ds.id for ds in sources}
    byExactName = _uniqueIndex(sources, lambda name: name.strip())
    byLowerName = _uniqueIndex(sources, lambda name: name.strip().lower())
    resolved: dict[str, int] = {}
    for key in wanted:
        if key in byId:
            resolved[key] = byId[key]
        elif key.strip() in byExactName:
            resolved[key] = byExactName[key.strip()]
        elif key.strip().lower() in byLowerName:
            resolved[key] = byLowerName[key.strip().lower()]
    return resolved


async def runBackfill(
    session: AsyncSession, rows: list[MappingRow], *, force: bool
) -> tuple[list[Any], int, int, int]:
    """按 CSV 落库；返回 (逐行结果, bound, skipped, failed)。"""
    resolved = await _resolveDatasources(session, rows)
    items: list[tuple[int, KpiDatasourceBulkBindItem]] = []
    results: list[Any] = []
    unresolved = 0
    for row in rows:
        dsId = resolved.get(row.datasource)
        if dsId is None:
            unresolved += 1
            results.append(
                {
                    "row": row.row,
                    "kpiCode": row.kpi_code,
                    "status": "failed",
                    "error": (
                        f"数据源「{row.datasource}」不存在、已停用"
                        "或同名有歧义（歧义时请改填 ID）"
                    ),
                }
            )
            continue
        items.append(
            (row.row, KpiDatasourceBulkBindItem(kpi_code=row.kpi_code, datasource_id=dsId))
        )

    bulk = await KpiDatasourceBindingService().bulkBind(
        session, [item for _, item in items], _ACTOR, force=force
    )
    # 把 bulk 的 1..N 行号还原成 CSV 行号，便于对着文件排查
    for (rowNo, _), resultRow in zip(items, bulk.results, strict=True):
        results.append(
            {
                "row": rowNo,
                "kpiCode": resultRow.kpi_code,
                "status": resultRow.status,
                "error": resultRow.error,
            }
        )
    results.sort(key=lambda r: r["row"])
    failed = bulk.failed + unresolved
    return results, bulk.bound, bulk.skipped, failed


def _printPreview(rows: list[PreviewRow]) -> None:
    if not rows:
        print("没有未绑定数据源的 KPI。")
        return
    print(f"未绑定 KPI {len(rows)} 个（预览，未写库）：")
    for r in rows:
        suggestion = r.suggestion_name or "（无建议）"
        reason = f" [{r.suggestion_reason}]" if r.suggestion_reason else ""
        print(
            f"  - {r.kpi_code} | {r.kpi_name} | legacy={r.legacy_source or '-'} "
            f"| 建议={suggestion}{reason}"
        )


def _printResults(results: list[Any], bound: int, skipped: int, failed: int) -> None:
    for r in results:
        suffix = f" ({r['error']})" if r.get("error") else ""
        print(f"  [row {r['row']}] {r['status']}: {r['kpiCode']}{suffix}")
    print(f"完成：bound={bound} skipped={skipped} failed={failed}")


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KPI → 数据源回填")
    parser.add_argument("--mapping", type=Path, help="CSV：kpi_code,datasource")
    parser.add_argument("--apply", action="store_true", help="按 CSV 落库（默认只预览）")
    parser.add_argument("--force", action="store_true", help="覆盖已绑定其他数据源的行")
    args = parser.parse_args(argv)

    if args.force and not args.apply:
        parser.error("--force 只能与 --apply 一起使用")
    if args.apply and args.mapping is None:
        parser.error("--apply 需要 --mapping")

    factory = getSessionFactory()
    async with factory() as session:
        if not args.apply:
            _printPreview(await buildPreview(session))
            return 0
        rows = parseMappingCsv(args.mapping)
        results, bound, skipped, failed = await runBackfill(
            session, rows, force=args.force
        )
        _printResults(results, bound, skipped, failed)
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
