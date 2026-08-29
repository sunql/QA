"""检查 Milvus ontology_embeddings 集合状态：按 type 统计实体数。

用法：cd backend && uv run python scripts/diag_milvus_embedding_state.py
"""
from __future__ import annotations

import asyncio

from app.infrastructure import milvus_client as milvus


async def main() -> None:
    try:
        coll = milvus.ensureCollection()
        coll.load()
        data = coll.query("", output_fields=["id", "type"], limit=15000)
        by_type: dict[str, int] = {}
        for d in data:
            by_type[d.get("type", "?")] = by_type.get(d.get("type", "?"), 0) + 1
        print(f"ontology_embeddings 总数: {len(data)}")
        for k, v in sorted(by_type.items()):
            print(f"  type={k}: {v}")
        # 各 type 最新几条 id（新种子在尾部，便于核对）
        for etype in ("class", "property"):
            ids = [d["id"] for d in data if d.get("type") == etype][-5:]
            print(f"  {etype} 最新 id 样本: {ids}")
    except Exception as exc:  # noqa: BLE001
        print("ERR:", exc)


if __name__ == "__main__":
    asyncio.run(main())
