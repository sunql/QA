"""实证「采购价格」问题的本体类裁剪：召回集里有没有 SupplierPriceDetail/List。

对话链路 _selectRelevantClasses 用 searchByKeyword 召回 topK 类，只把命中子集
送进 plan/SQL 阶段。本脚本直接复现该调用，打印召回类清单与两个价格表的去向。

用法：cd backend && uv run python scripts/diag_relevant_classes.py
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

env = Path(__file__).resolve().parents[2] / "docker" / ".env"
for line in env.read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("SECRET_KEY="):
        os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1].strip()

from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.ontology_service import OntologyService  # noqa: E402

QUESTION = "统计查询2025年采购量最多的10种物料的采购价格"
TOP_K = 15


async def main() -> None:
    factory = getSessionFactory()
    async with factory() as session:
        service = OntologyService()
        all_classes = await service.listClasses(session)
        try:
            hits = await service.searchByKeyword(
                QUESTION, topK=TOP_K, typeFilter="class"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"检索失败（回退全量）: {getattr(exc, 'detail', None) or exc}")
            return
        by_id = {c.id: c for c in all_classes}
        hit_ids = {h.id for h in hits}
        recalled = [by_id[i] for i in hit_ids if i in by_id]
        print(f"召回 {len(hits)} 条，可解析为类 {len(recalled)}/{len(all_classes)}")
        print("\n== 召回类（按 score）==")
        for h in sorted(hits, key=lambda x: x.score, reverse=True):
            c = by_id.get(h.id)
            table = c.source_table if c else "?"
            print(f"  {h.score:.4f}  {table}  {c.class_name if c else h.name}")
        print("\n== 两个价格表是否在召回集 ==")
        for tbl in ("PPRICLIST", "PPRICFICH"):
            c = next((x for x in all_classes if x.source_table == tbl), None)
            if c is None:
                print(f"  {tbl}: 本体无此类")
            elif c.id in hit_ids:
                print(f"  {tbl}: 在召回集 ✓")
            else:
                print(f"  {tbl}: 不在召回集 ✗（被裁剪，LLM 看不到）")
        print(f"\n召回率: {len(recalled)}/{len(all_classes)}（topK={TOP_K}）")


if __name__ == "__main__":
    asyncio.run(main())
