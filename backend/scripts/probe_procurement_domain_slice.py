"""采购域切片可达性探针（只读）：M1-1.4 的**业务后果**取证。

为什么需要它
------------
M1-1.4 的补齐动作（复活墓碑类 / 补 BO / 改指）在**本体表**上看得见，
但真正的业务后果是「**采购域切片里能不能看见采购 KPI**」：

- 指标归属靠类锚定（``targetClassName`` / ``sources[].className``）；
- ``ontology_metric_source`` 指向**墓碑类**的源会被编译器**整条丢弃**
  ⇒ 指标 ``sources`` 为空 ⇒ 归不了属 ⇒ 被 fail-closed 排除；
- 域归属只由 ``business_object.primary_domain_id`` 承担 ⇒ 缺 BO ⇒ 域切片空。

⇒ 「类复活了」不等于「KPI 能被查到」。本探针把这条链**端到端**走一遍：
治理表 → ``compileManifest`` → ``sliceManifestByDomain(PROCUREMENT)``。

纪律（与同款探针一致，不可放宽）

1. **只读**：全部是 SELECT / 内存编译，不写库、不改库。
2. **不落密**：不打印 DSN / 口令。
3. **只写证据文件**：证据文件不含真实 DSN / 口令。

用法::

    cd backend
    uv run python scripts/probe_procurement_domain_slice.py \
      --out ../docs/update/1118/M1/evidence/procurement-domain-slice.txt

退出码：0 通过 / 1 环境错。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

sys.path.insert(0, os.getcwd())

from app.config import getSettings  # noqa: E402
from app.domain.models import (  # noqa: E402
    OntologyClass,
    OntologyMetricSource,
)
from app.services.semantic_compiler import compileManifest  # noqa: E402
from app.services.semantic_domain_slice import sliceManifestByDomain  # noqa: E402

_DOMAIN_CODE = "PROCUREMENT"

# 本批要复活的 7 个类（与 app/domain/procurement_object_catalog.py 同款）。
_PROCUREMENT_CLASS_NAMES: tuple[str, ...] = (
    "DWD_GOODS_RECEIPT_LINE",
    "DWD_PURCHASE_INVOICE",
    "DWD_PURCHASE_INVOICE_LINE",
    "DWD_PURCHASE_ORDER_LINE",
    "DWD_PURCHASE_REQUISITION_LINE",
    "DWD_REQUISITION_ORDER_LINK",
    "DWD_SUPPLIER_PAYMENT",
)


async def _collect(session: AsyncSession) -> list[str]:
    """采集切片可达性事实，返回可打印的行。"""
    manifest = await compileManifest(session)
    slice_ = sliceManifestByDomain(manifest, _DOMAIN_CODE)

    classes = (await session.execute(select(OntologyClass))).scalars().all()
    sources = (
        await session.execute(select(OntologyMetricSource))
    ).scalars().all()

    activeClasses = [row for row in classes if row.valid_to is None]
    tombstoneClasses = [row for row in classes if row.valid_to is not None]
    compiledSources = sum(len(item.get("sources", ())) for item in manifest["metrics"])
    rawSources = len(sources)

    lines: list[str] = []
    lines.append(f"域切片 domain = {_DOMAIN_CODE}")
    lines.append("")
    lines.append("--- 本体总量 ---")
    lines.append(f"ontology_class 总数 = {len(classes)}")
    lines.append(f"活跃类（valid_to IS NULL）= {len(activeClasses)}")
    lines.append(f"墓碑类（valid_to 非空）= {len(tombstoneClasses)}")
    lines.append("")
    lines.append("--- 本批 7 个采购类的墓碑状态 ---")
    byName = {row.class_name: row for row in classes}
    for name in _PROCUREMENT_CLASS_NAMES:
        row = byName.get(name)
        if row is None:
            lines.append(f"{name}: 不存在")
        elif row.valid_to is None:
            lines.append(f"{name}: 活跃（已复活）")
        else:
            lines.append(f"{name}: 墓碑 valid_to={row.valid_to}")
    lines.append("")
    lines.append("--- 指标源（口径）完整性 ---")
    lines.append(f"ontology_metric_source 行数 = {rawSources}")
    lines.append(f"编译后保留的 source 条数 = {compiledSources}")
    lines.append(
        f"被丢弃（指向墓碑类）= {rawSources - compiledSources}"
        "  ← 丢弃即口径缺失"
    )
    for item in manifest["metrics"]:
        if not item.get("sources"):
            lines.append(f"  [口径缺失] {item['metricName']}：sources 为空")
    lines.append("")
    lines.append("--- 采购域切片 ---")
    lines.append(f"classNames 数 = {len(slice_.classNames)}")
    lines.append(f"businessObjects 数 = {len(slice_.businessObjects)}")
    for bo in slice_.businessObjects:
        lines.append(f"  BO {bo.get('code')} → 头类 {bo.get('headerClassName')}")
    lines.append(f"metrics 数 = {len(slice_.metrics)}")
    for metric in slice_.metrics:
        sourcesText = ", ".join(
            f"{src.get('className')}/{src.get('role')}"
            for src in metric.get("sources", ())
        )
        lines.append(f"  指标 {metric['metricName']} 源=[{sourcesText}]")
    lines.append(f"kpis 数 = {len(slice_.kpis)}")
    for kpi in slice_.kpis:
        lines.append(f"  KPI {kpi.get('kpiCode')} · {kpi.get('kpiName')}")
    return lines


async def main() -> int:
    parser = argparse.ArgumentParser(description="采购域切片可达性探针（只读）")
    parser.add_argument("--out", help="输出文件路径（默认写 stdout）")
    args = parser.parse_args()

    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl, pool_pre_ping=False)
    lines: list[str] = [
        "采购域切片可达性探针 · 只读（M1-1.4）",
        "口径：compileManifest（真实治理表 12 张）→ sliceManifestByDomain(PROCUREMENT)。",
        "",
    ]
    try:
        async with AsyncSession(engine) as session:
            lines.extend(await _collect(session))
    except Exception as exc:  # noqa: BLE001 —— 环境错要可读
        print(f"[ENV ] 探针无法执行：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

    output = "\n".join(lines) + "\n"
    if args.out:
        path = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(output)
        print(f"已写出：{path}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
