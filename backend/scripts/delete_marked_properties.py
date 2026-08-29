"""按 Excel `不需要=1` 列裁剪 ontology_property（一次性数据修复）。

背景：docs/excel/*.xlsx 新增 `不需要` 列，列值=1 的属性须从系统中移除。
本脚本从 /tmp/delete_plan.json 读取待删清单（236 个），逐条调用
OntologyService.deleteProperty（每条独立 commit，避免 autoflush 误删，见
memory [[autoflush-delete-ordering]]）。每条 deleteProperty 内部已完成：
- PG 实体删除（cascade 处理）
- Neo4j Property 节点 DETACH DELETE
- Milvus best-effort deleteByOntologyId

Milvus 的删集重建由 scripts/backfill_milvus_embeddings.py --cleanup 兜底
（见 memory [[milvus-bulk-delete-unreliable]]），本脚本不重复该路径。

幂等：删过的不存在（getProperty 抛 NotFoundError 即跳过）。可重跑。
用法：cd backend && uv run python scripts/delete_marked_properties.py [--dry-run] [--plan PATH]

退出码：0 全部完成；非 0 有失败。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.ontology_service import OntologyService  # noqa: E402

logger = logging.getLogger("delete_marked_properties")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DEFAULT_PLAN = Path("/tmp/delete_plan.json")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="按 Excel `不需要=1` 列裁剪 ontology_property")
    p.add_argument("--dry-run", action="store_true", help="只打印待删清单，不真正删除")
    p.add_argument("--plan", default=str(DEFAULT_PLAN), help=f"delete_plan.json 路径（默认 {DEFAULT_PLAN}）")
    return p.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    plan_path = Path(args.plan)
    if not plan_path.exists():
        logger.error("找不到计划文件 %s — 先跑解析脚本生成 delete_plan.json", plan_path)
        return 1

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    matched = plan["matched"]
    not_matched = plan["not_matched"]
    logger.info("待删 %d 条，未匹配 %d 条（疑似新增）", len(matched), len(not_matched))

    if args.dry_run:
        logger.info("[dry-run] 仅打印，不写入")
        for m in matched:
            logger.info("  delete prop_id=%d  %s.%s  (col=%s)",
                        m["prop_id"], m["class"], m["prop_name"], m["col"])
        return 0

    svc = OntologyService()
    factory = getSessionFactory()

    ok = fail = skipped = 0
    failures: list[tuple[int, str, str]] = []

    for m in matched:
        pid = m["prop_id"]
        async with factory() as session:
            try:
                # 先确认仍在（可能被并发删掉）
                row = await session.execute(
                    text("SELECT id FROM ontology_property WHERE id = :id"), {"id": pid}
                )
                if row.fetchone() is None:
                    skipped += 1
                    logger.info("skip (already gone) prop_id=%d %s.%s",
                                pid, m["class"], m["prop_name"])
                    continue
                await svc.deleteProperty(session, pid)
                ok += 1
                logger.info("ok prop_id=%d %s.%s (col=%s)",
                            pid, m["class"], m["prop_name"], m["col"])
            except Exception as exc:  # noqa: BLE001
                fail += 1
                failures.append((pid, m["prop_name"], str(exc)[:200]))
                logger.error("FAIL prop_id=%d %s.%s: %s",
                             pid, m["class"], m["prop_name"], exc)

    logger.info("=" * 60)
    logger.info("汇总: ok=%d skipped=%d fail=%d (total planned=%d)",
                ok, skipped, fail, len(matched))
    if failures:
        logger.info("失败明细:")
        for pid, name, msg in failures:
            logger.info("  id=%d %s — %s", pid, name, msg)
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
