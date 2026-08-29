"""对照 seed_ontology 期望 vs 运行时 PG 本体差异的守门脚本。

读取 seed_ontology.PROPERTIES / CLASSES 作为期望真源（不双份定义），
与 PG ontology_property / ontology_class / ontology_join 三表比对：
  - missing：seed 有，PG 无（说明 seed 重跑未生效或字段被误删）
  - mismatch：命中但 data_type / is_foreign_key / ref_class_id / source_column 不同
  - extra：PG 有，seed 无（孤儿，可能是已删除或新增但未补回 seed）

用法：cd backend && uv run python scripts/verify_ontology_consistency.py
     cd backend && uv run python scripts/verify_ontology_consistency.py --strict
     cd backend && uv run python scripts/verify_ontology_consistency.py --out /tmp/diff

幂等：只读 PG，不写任何表。
非零差异以 exit code 1 退出（适合 CI）。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import select

BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BACKEND_DIR.parent / "docker" / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("SECRET_KEY="):
            os.environ.setdefault("SECRET_KEY", line.split("=", 1)[1].strip())

sys.path.insert(0, str(BACKEND_DIR))

from app.domain.models import OntologyClass, OntologyJoin, OntologyProperty  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402

import seed_ontology  # noqa: E402

logger = logging.getLogger("verify_ontology_consistency")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

DEFAULT_OUT_DIR = BACKEND_DIR.parent / "docs" / "excel"


def _expected_join_key(join: tuple) -> tuple:
    """把 seed 的 BUSINESS_JOINS 项归一成 (src_table, [src_cols], tgt_table, [tgt_cols]) 元组。

    seed 的 BUSINESS_JOINS 每项为 (src_table, [src_cols], tgt_table, [tgt_cols], join_type, description)。
    """
    src_table, src_cols, tgt_table, tgt_cols = join[0], join[1], join[2], join[3]
    return (src_table, tuple(src_cols), tgt_table, tuple(tgt_cols))


async def main(args: argparse.Namespace) -> int:
    factory = getSessionFactory()
    out_dir = Path(args.out) if args.out else DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()

    async with factory() as session:
        classes = (await session.execute(select(OntologyClass))).scalars().all()
        cid_by_table: dict[str, int] = {c.source_table: c.id for c in classes}
        table_by_cid: dict[int, str] = {c.id: c.source_table for c in classes}
        props = (await session.execute(select(OntologyProperty))).scalars().all()
        joins = (await session.execute(select(OntologyJoin))).scalars().all()

    prop_by_key: dict[tuple[int, str], OntologyProperty] = {
        (p.class_id, p.property_name): p for p in props
    }

    # ---------- 1) Property 维度 ----------
    missing: list[dict] = []
    mismatch: list[dict] = []
    extra: list[dict] = []
    seen_keys: set[tuple[int, str]] = set()

    for src_table, expected_list in seed_ontology.PROPERTIES.items():
        cid = cid_by_table.get(src_table)
        for ep in expected_list:
            if cid is None:
                missing.append(
                    {
                        "source_table": src_table,
                        "property": ep["name"],
                        "expected_alias": ep.get("alias", ""),
                        "reason": "class_not_in_pg",
                    }
                )
                continue
            key = (cid, ep["name"])
            seen_keys.add(key)
            cp = prop_by_key.get(key)
            if cp is None:
                missing.append(
                    {
                        "source_table": src_table,
                        "property": ep["name"],
                        "expected_alias": ep.get("alias", ""),
                        "expected_type": ep.get("type", ""),
                        "reason": "missing_in_pg",
                    }
                )
                continue
            # data_type
            if cp.data_type != ep["type"]:
                mismatch.append(
                    {
                        "source_table": src_table,
                        "property": ep["name"],
                        "field": "data_type",
                        "expected": ep["type"],
                        "actual": cp.data_type or "",
                    }
                )
            # is_foreign_key + ref_class_id
            exp_fk = ep.get("fk")
            exp_ref_id = cid_by_table.get(exp_fk) if exp_fk else None
            if exp_fk:
                # 期望是外键
                if not cp.is_foreign_key:
                    mismatch.append(
                        {
                            "source_table": src_table,
                            "property": ep["name"],
                            "field": "is_foreign_key",
                            "expected": "True",
                            "actual": "False",
                        }
                    )
                if cp.ref_class_id != exp_ref_id:
                    mismatch.append(
                        {
                            "source_table": src_table,
                            "property": ep["name"],
                            "field": "ref_class_id",
                            "expected": exp_fk,
                            "actual": table_by_cid.get(cp.ref_class_id, ""),
                        }
                    )
            else:
                # 期望不是外键
                if cp.is_foreign_key:
                    mismatch.append(
                        {
                            "source_table": src_table,
                            "property": ep["name"],
                            "field": "is_foreign_key",
                            "expected": "False",
                            "actual": "True",
                        }
                    )
            # source_column
            exp_col = ep.get("col") or ep.get("alias")
            if exp_col and cp.source_column != exp_col:
                mismatch.append(
                    {
                        "source_table": src_table,
                        "property": ep["name"],
                        "field": "source_column",
                        "expected": exp_col,
                        "actual": cp.source_column or "",
                    }
                )
            # is_primary_key
            exp_pk = bool(ep.get("pk"))
            if cp.is_primary_key != exp_pk:
                mismatch.append(
                    {
                        "source_table": src_table,
                        "property": ep["name"],
                        "field": "is_primary_key",
                        "expected": str(exp_pk),
                        "actual": str(cp.is_primary_key),
                    }
                )

    for p in props:
        if (p.class_id, p.property_name) not in seen_keys:
            extra.append(
                {
                    "source_table": table_by_cid.get(p.class_id, ""),
                    "property": p.property_name,
                    "alias": p.property_alias or "",
                    "data_type": p.data_type or "",
                }
            )

    # ---------- 2) Class 维度 ----------
    class_missing: list[dict] = []
    class_extra: list[dict] = []
    expected_class_tables = {c["source_table"] for c in seed_ontology.CLASSES}
    actual_class_tables = set(cid_by_table.keys())
    for t in expected_class_tables - actual_class_tables:
        class_missing.append({"source_table": t, "reason": "missing_in_pg"})
    for t in actual_class_tables - expected_class_tables:
        class_extra.append({"source_table": t, "reason": "extra_in_pg"})

    # ---------- 3) Join 维度 ----------
    join_missing: list[dict] = []
    join_extra: list[dict] = []
    join_mismatch: list[dict] = []
    expected_join_keys: set[tuple] = set()
    expected_by_key: dict[tuple, tuple] = {}
    for j in seed_ontology.BUSINESS_JOINS:
        key = _expected_join_key(j)
        expected_join_keys.add(key)
        expected_by_key[key] = j
    actual_join_keys: set[tuple] = {
        (
            table_by_cid.get(j.source_class_id, ""),
            tuple(j.source_columns or []),
            table_by_cid.get(j.target_class_id, ""),
            tuple(j.target_columns or []),
        )
        for j in joins
    }
    for k in expected_join_keys - actual_join_keys:
        join_missing.append(
            {
                "source_table": k[0],
                "source_columns": ",".join(k[1]),
                "target_table": k[2],
                "target_columns": ",".join(k[3]),
            }
        )
    for k in actual_join_keys - expected_join_keys:
        join_extra.append(
            {
                "source_table": k[0],
                "source_columns": ",".join(k[1]),
                "target_table": k[2],
                "target_columns": ",".join(k[3]),
            }
        )
    # 找到的 join：检查 join_type（按 (src_table, src_cols, tgt_table, tgt_cols) 对齐）
    for j in joins:
        key = (
            table_by_cid.get(j.source_class_id, ""),
            tuple(j.source_columns or []),
            table_by_cid.get(j.target_class_id, ""),
            tuple(j.target_columns or []),
        )
        if key not in expected_join_keys:
            continue
        exp = expected_by_key[key]
        exp_join_type = exp[4] if len(exp) >= 5 else ""
        if (j.join_type or "").lower() != (exp_join_type or "").lower():
            join_mismatch.append(
                {
                    "source_table": key[0],
                    "source_columns": ",".join(key[1]),
                    "target_table": key[2],
                    "field": "join_type",
                    "expected": exp_join_type or "",
                    "actual": j.join_type or "",
                }
            )

    # ---------- 4) 写 CSV ----------
    csv_files = {
        "property_missing": missing,
        "property_mismatch": mismatch,
        "property_extra": extra,
        "class_missing": class_missing,
        "class_extra": class_extra,
        "join_missing": join_missing,
        "join_mismatch": join_mismatch,
        "join_extra": join_extra,
    }
    for label, rows in csv_files.items():
        out = out_dir / f"ontology_diff_{label}_{today}.csv"
        fieldnames = (
            list(rows[0].keys()) if rows else ["source_table", "property", "expected", "actual"]
        )
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        logger.info("wrote %s (%d rows)", out.name, len(rows))

    # ---------- 5) 报告 ----------
    n_class_mis = len(class_missing) + len(class_extra)
    n_prop_mis = len(missing) + len(mismatch) + len(extra)
    n_join_mis = len(join_missing) + len(join_mismatch) + len(join_extra)
    logger.info(
        "本体一致性: classes=%d (期望 %d, 缺 %d 多 %d) | properties=%d (期望 %d, 缺 %d 不符 %d 多 %d) | joins=%d (期望 %d, 缺 %d 不符 %d 多 %d)",
        len(cid_by_table),
        len(expected_class_tables),
        len(class_missing),
        len(class_extra),
        len(props),
        sum(len(v) for v in seed_ontology.PROPERTIES.values()),
        len(missing),
        len(mismatch),
        len(extra),
        len(joins),
        len(expected_join_keys),
        len(join_missing),
        len(join_mismatch),
        len(join_extra),
    )

    fatal = bool(missing or class_missing or join_missing)
    # mismatch 在 --strict 下才算致命，否则只 INFO
    if args.strict:
        fatal = fatal or bool(mismatch or join_mismatch)
        # extra 默认不算致命（PG 可能有手动加的属性）
    if fatal:
        logger.error("❌ 不一致（missing/--strict 失败）")
        return 1
    logger.info("✅ 一致")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--strict",
        action="store_true",
        help="把 mismatch 也算致命失败（默认仅 missing 算致命）",
    )
    p.add_argument(
        "--out",
        type=str,
        default=None,
        help=f"CSV 输出目录（默认 {DEFAULT_OUT_DIR}）",
    )
    return p.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
