"""一次性修复 seed_ontology 已写入的本体元数据错误（FK 目标 + 类型映射）。

背景：seed_ontology._seedProperties 对已存在属性是 SKIP 语义（不更新
data_type / is_foreign_key / ref_class_id / source_column），所以发现
错误后必须经 OntologyService.updateProperty 定向落库；updateProperty
内部已自动 reconcile Neo4j REFERENCES 边（基于 is_foreign_key /
ref_class_id 是否变化），无需额外操作图谱。

用法：cd backend && uv run python scripts/apply_ontology_fixes.py
幂等：可重跑；同一属性再次写相同值 = no-op；非 0 失败时非零退出。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import select

# 让 import seed_ontology 前的 CONFIG 可用：从 docker/.env 加载 SECRET_KEY 等
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

from app.domain.models import OntologyClass, OntologyProperty  # noqa: E402
from app.domain.schemas import OntologyPropertyUpdate  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.ontology_service import OntologyService  # noqa: E402

logger = logging.getLogger("apply_ontology_fixes")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# 9 个 FK 修复：(source_table, property_name, new_ref_source_table)
# 业务核对：8 处供应商从 BPSUPPLIER 改 BPARTNER（统一伙伴主档）；
# 1 处 PRECEIPT.BPTNUM_0（承运人）改 BPCARRIER（承运人主档）；
# 1 处 PPRICLIST.价格表号 改 PPRICCONF（价格配置主档，而非 PPRICFICH 价格表）。
FK_FIXES: list[tuple[str, str, str]] = [
    ("YPRECEIPT", "供应商", "BPARTNER"),
    ("YPRECEIPTD", "供应商", "BPARTNER"),
    ("PRECEIPT", "供应商", "BPARTNER"),
    ("PRECEIPT", "承运人", "BPCARRIER"),
    ("PRECEIPTD", "供应商", "BPARTNER"),
    ("PORDERQ", "供应商", "BPARTNER"),
    ("PORDER", "供应商", "BPARTNER"),
    ("PREQUISD", "供应商", "BPARTNER"),
    ("PPRICLIST", "价格表号", "PPRICCONF"),
]


# 26 个 type 修复：(source_table, property_name, new_data_type)
# 业务核对：Sage X3 `M` 类型为菜单/枚举码，存的是代码字符串，应用 STRING；
# `C` 类型为计数器，存的是整数，应用 INT；
# 现误标为 INT（让 LLM 走数字比较）或 DECIMAL（让 LLM 走带小数比较），
# 改 STRING/INT 后 NL2SQL 走正确谓词。
TYPE_FIXES: list[tuple[str, str, str]] = [
    ("PPRICFICH", "有效状态", "INT"),
    ("YPRECEIPT", "审核标志", "STRING"),
    ("YPRECEIPT", "到货标志", "STRING"),
    ("YPRECEIPTD", "订单类型", "STRING"),
    ("YPRECEIPTD", "行审核标志", "STRING"),
    ("YPRECEIPTD", "质检标志", "STRING"),
    ("PRECEIPT", "采购类型", "STRING"),
    ("PRECEIPT", "汇率类型", "STRING"),
    ("PRECEIPT", "税则类型", "STRING"),
    ("PRECEIPT", "打印标志", "STRING"),
    ("PRECEIPT", "发票标志", "STRING"),
    ("PRECEIPT", "过账标志", "STRING"),
    ("PRECEIPT", "自动发货标志", "STRING"),
    ("PRECEIPTD", "订单类型", "STRING"),
    ("PRECEIPTD", "打印标志", "STRING"),
    ("PRECEIPTD", "发票标志", "STRING"),
    ("PRECEIPTD", "过账标志", "STRING"),
    ("PRECEIPTD", "质检标志", "STRING"),
    ("PRECEIPTD", "行类型", "STRING"),
    ("PRECEIPTD", "行类别", "STRING"),
    ("PORDER", "采购订单类型", "STRING"),
    ("PORDER", "采购类型", "STRING"),
    ("PREQUISD", "汇率类型", "STRING"),
    ("PREQUISD", "采购类型", "STRING"),
    ("PREQUISD", "关闭标志", "STRING"),
    ("PREQUISD", "下单标志", "STRING"),
    ("PREQUISD", "审核标志", "STRING"),
    ("PPRICLIST", "价格除数", "INT"),
]


async def _find_property(
    session,
    classes_by_table: dict[str, "OntologyClass"],
    src_table: str,
    prop_name: str,
) -> "OntologyProperty | None":
    """按 (source_table, property_name) 定位现有属性。"""
    cls = classes_by_table.get(src_table)
    if cls is None:
        return None
    stmt = select(OntologyProperty).where(
        OntologyProperty.class_id == cls.id,
        OntologyProperty.property_name == prop_name,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _apply_type_fix(
    session,
    service: "OntologyService",
    src_table: str,
    prop_name: str,
    new_type: str,
) -> str:
    """修复单条类型。返回 'fixed' / 'skipped' / 'missing'。"""
    cls = (await session.execute(
        select(OntologyClass).where(OntologyClass.source_table == src_table)
    )).scalar_one_or_none()
    if cls is None:
        return "missing"
    prop = await _find_property(session, {src_table: cls}, src_table, prop_name)
    if prop is None:
        return "missing"
    if prop.data_type == new_type:
        return "skipped"
    dto = OntologyPropertyUpdate(data_type=new_type)
    await service.updateProperty(session, prop.id, dto)
    return "fixed"


async def _apply_fk_fix(
    session,
    service: "OntologyService",
    src_table: str,
    prop_name: str,
    new_ref_table: str,
    classes_by_table: dict[str, "OntologyClass"],
) -> str:
    """修复单条 FK 目标。返回 'fixed' / 'skipped' / 'missing'。"""
    new_cls = classes_by_table.get(new_ref_table)
    if new_cls is None:
        return "missing"
    prop = await _find_property(session, classes_by_table, src_table, prop_name)
    if prop is None:
        return "missing"
    if prop.ref_class_id == new_cls.id:
        return "skipped"
    dto = OntologyPropertyUpdate(is_foreign_key=True, ref_class_id=new_cls.id)
    await service.updateProperty(session, prop.id, dto)
    return "fixed"


async def main() -> int:
    factory = getSessionFactory()
    service = OntologyService()

    n_type_fixed = 0
    n_type_skipped = 0
    n_type_missing = 0
    n_fk_fixed = 0
    n_fk_skipped = 0
    n_fk_missing = 0

    async with factory() as session:
        # 加载所有类，构建 source_table -> class 映射
        classes = (await session.execute(select(OntologyClass))).scalars().all()
        classes_by_table: dict[str, OntologyClass] = {
            c.source_table: c for c in classes if c.source_table
        }

        # Phase 1: 26 个 type 修复
        logger.info("=== Phase 1: type 修复 (%d 条) ===", len(TYPE_FIXES))
        for src_table, prop_name, new_type in TYPE_FIXES:
            try:
                result = await _apply_type_fix(
                    session, service, src_table, prop_name, new_type,
                )
                if result == "fixed":
                    n_type_fixed += 1
                    logger.info("TYPE: %s.%s -> %s", src_table, prop_name, new_type)
                elif result == "skipped":
                    n_type_skipped += 1
                    logger.debug("TYPE skip: %s.%s already %s", src_table, prop_name, new_type)
                else:  # missing
                    n_type_missing += 1
                    logger.warning("TYPE 缺失属性: %s.%s", src_table, prop_name)
            except Exception as exc:  # noqa: BLE001
                logger.exception("TYPE 失败 %s.%s: %s", src_table, prop_name, exc)
                await session.rollback()

        # Phase 2: 9 个 FK 修复
        logger.info("=== Phase 2: FK 修复 (%d 条) ===", len(FK_FIXES))
        for src_table, prop_name, new_ref_table in FK_FIXES:
            try:
                result = await _apply_fk_fix(
                    session, service, src_table, prop_name, new_ref_table,
                    classes_by_table,
                )
                if result == "fixed":
                    n_fk_fixed += 1
                    logger.info(
                        "FK: %s.%s -> %s", src_table, prop_name, new_ref_table,
                    )
                elif result == "skipped":
                    n_fk_skipped += 1
                    logger.debug(
                        "FK skip: %s.%s already %s",
                        src_table, prop_name, new_ref_table,
                    )
                else:
                    n_fk_missing += 1
                    logger.warning(
                        "FK 缺失属性: %s.%s", src_table, prop_name,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("FK 失败 %s.%s: %s", src_table, prop_name, exc)
                await session.rollback()

    # 摘要
    logger.info(
        "=== 完成 ===\n"
        "TYPE: fixed=%d skipped=%d missing=%d (期望 %d)\n"
        "FK:   fixed=%d skipped=%d missing=%d (期望 %d)",
        n_type_fixed, n_type_skipped, n_type_missing, len(TYPE_FIXES),
        n_fk_fixed, n_fk_skipped, n_fk_missing, len(FK_FIXES),
    )

    # 成功标准：fixed + skipped == 期望（fixed 真正改写，skipped 已为目标值 = no-op）
    expected_type = len(TYPE_FIXES)
    expected_fk = len(FK_FIXES)
    type_ok = (n_type_fixed + n_type_skipped) == expected_type and n_type_missing == 0
    fk_ok = (n_fk_fixed + n_fk_skipped) == expected_fk and n_fk_missing == 0
    return 0 if type_ok and fk_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
