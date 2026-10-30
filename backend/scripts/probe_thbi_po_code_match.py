"""一次性只读探针 2：THBI 采购域编码 与 entity_mapping 编码的交集率。

从 Oracle 拉 distinct 编码（Supplier ~千级 / Material 先数 distinct 再决定），
与本地 qa_metadata.entity_mapping 求交集。只读，不写库。

    ./scripts/run_local.sh python -m scripts.probe_thbi_po_code_match
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.domain.models import DataSource, EntityMapping  # noqa: E402
from app.infrastructure.business_db_pool import (  # noqa: E402
    dispose_adapter,
    get_adapter,
)
from app.infrastructure.database import getSessionFactory  # noqa: E402


def _col(row: dict, name: str) -> str:
    return str(row.get(name) or row.get(name.lower()) or "").strip()


async def fetchDistinctOracle(adapter, sql: str, label: str) -> set[str]:
    rows = await adapter.execute_read_only(sql)
    codes = {_col(r, "code") for r in rows if _col(r, "code")}
    print(f"[probe] {label}: distinct {len(codes)}")
    return codes


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        thbi = (
            await session.execute(
                select(DataSource).where(
                    DataSource.type == "oracle", DataSource.is_active.is_(True)
                )
            )
        ).scalars().one()
        mappingRows = (
            await session.execute(
                select(EntityMapping.entity_type, EntityMapping.enterprise_code)
            )
        ).all()
    pgSup = {c for t, c in mappingRows if t == "SUPPLIER"}
    pgMat = {c for t, c in mappingRows if t == "MATERIAL"}
    print(f"[probe] entity_mapping: SUPPLIER={len(pgSup)} MATERIAL={len(pgMat)}")

    adapter = get_adapter(thbi.id, thbi)
    try:
        orSup = await fetchDistinctOracle(
            adapter,
            "SELECT DISTINCT supplier_code AS code FROM THBI.DWD_PURCHASE_ORDER "
            "WHERE supplier_code IS NOT NULL",
            "DWD_PURCHASE_ORDER.supplier_code",
        )
        orSupLine = await fetchDistinctOracle(
            adapter,
            "SELECT DISTINCT supplier_code AS code FROM THBI.DWD_PURCHASE_ORDER_LINE "
            "WHERE supplier_code IS NOT NULL",
            "DWD_PURCHASE_ORDER_LINE.supplier_code",
        )
        orMat = await fetchDistinctOracle(
            adapter,
            "SELECT DISTINCT material_code AS code FROM THBI.DWD_PURCHASE_ORDER_LINE "
            "WHERE material_code IS NOT NULL",
            "DWD_PURCHASE_ORDER_LINE.material_code",
        )
        orRcvSup = await fetchDistinctOracle(
            adapter,
            "SELECT DISTINCT supplier_code AS code FROM THBI.DWD_GOODS_RECEIPT "
            "WHERE supplier_code IS NOT NULL",
            "DWD_GOODS_RECEIPT.supplier_code",
        )
        orRcvMat = await fetchDistinctOracle(
            adapter,
            "SELECT DISTINCT material_code AS code FROM THBI.DWD_GOODS_RECEIPT_LINE "
            "WHERE material_code IS NOT NULL",
            "DWD_GOODS_RECEIPT_LINE.material_code",
        )

        def report(name: str, oracleCodes: set[str], pgCodes: set[str]) -> None:
            hit = oracleCodes & pgCodes
            print(
                f"[probe] {name}: oracle={len(oracleCodes)} pg={len(pgCodes)} "
                f"交集={len(hit)} ({len(hit) / max(len(oracleCodes), 1):.1%})"
            )
            miss = sorted(oracleCodes - pgCodes)[:8]
            print(f"        oracle 独有样例: {miss}")

        report("PO头.供应商", orSup, pgSup)
        report("PO行.供应商", orSupLine, pgSup)
        report("PO行.物料", orMat, pgMat)
        report("收货头.供应商", orRcvSup, pgSup)
        report("收货行.物料", orRcvMat, pgMat)

        hitSup = sorted(orSup & pgSup)[:8]
        hitMat = sorted(orMat & pgMat)[:8]
        print(f"[probe] 交集供应商样例: {hitSup}")
        print(f"[probe] 交集物料样例: {hitMat}")
    finally:
        await dispose_adapter(thbi.id)


if __name__ == "__main__":
    asyncio.run(main())
