"""rebind_ontology_thbi.py — 本体重建：27 个本体类从 ZJTH X3 源表切到 THBI DWD 表。

背景：数仓 DWD 层以英文 snake_case 为数据标准（dw/02_dwd_master.sql + 03_dwd_facts.sql），
本体需从 X3 直连表 rebind 到 DWD 表，让 NL2SQL 的 AI 查询跑在标准命名上。

执行内容（幂等，可重跑）：
1. 解析 DWD SQL -> 规格 spec[table] = {col: {type, pk, x3}}
2. 数据源：注册 THBI 为默认数据源；ZJTH 降为非默认
3. 类 rebind：source_table 切到 DWD 表（SupplierPriceList/SupplierPriceConf 保留 ODS）
4. 属性 rebind：按 X3 列 -> DWD 列映射更新 source_column/type/PK；
   - DWD 列缺失 -> 删除该属性（连带 Neo4j）
   - DWD 新列 / 需纠偏列 -> COLUMN_CN 提供中文名，否则告警跳过
5. Join rebind：X3 列经类 DWD 表映射翻译；某侧列消失 -> 删除该 join
6. Metric rebind：formula 改写为 DWD 列
7. Neo4j 本体图同步（upsert + reconcile + delete 失效节点）

Milvus 由 backfill_milvus_embeddings.py --cleanup 单独执行（PG 为真源）。

运行（backend 目录，DATABASE_URL 覆盖 5433）：
  DATABASE_URL=... uv run python scripts/rebind_ontology_thbi.py --dump-spec   # 只看解析/计划
  DATABASE_URL=... uv run python scripts/rebind_ontology_thbi.py --dry-run     # 预览变更
  DATABASE_URL=... uv run python scripts/rebind_ontology_thbi.py               # 实际执行
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from sqlalchemy import select, update

from app.domain.models import (
    DataSource,
    OntologyClass,
    OntologyJoin,
    OntologyMetric,
    OntologyProperty,
)
from app.infrastructure import neo4j_client as neo4j
from app.infrastructure.database import getEngine, getSessionFactory
from app.infrastructure.security.crypto import encryptApiKey
from app.services.ontology_service import makeJoinKey

from scripts.dwd_spec import dumpSpec, parseDwdFiles

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("rebind_ontology_thbi")

DW_DIR = Path(__file__).resolve().parents[2] / "dw"
DWD_FILES = ["02_dwd_master.sql", "03_dwd_facts.sql"]

# 保留 ODS 的类。当前全部切 DWD（价格表头/配置已补 DWD，见 SSOT §9），
# 留空集合保持既有分支不触发。
_ODS_TABLES: set[str] = set()

# class_name -> 目标表（DWD 裸表名 / ODS 保留）
CLASS_TABLE: dict[str, str] = {
    "ItemMaster": "DWD_MATERIAL",
    "BusinessPartner": "DWD_BUSINESS_PARTNER",
    "BOM": "DWD_BOM",
    "BOMDetail": "DWD_BOM_DETAIL",
    "ItemFacility": "DWD_ITEM_FACILITY",
    "Facility": "DWD_FACILITY",
    "RoutingOperation": "DWD_ROUTING_OPERATION",
    "ArrivalNotice": "DWD_ARRIVAL_NOTICE",
    "ArrivalNoticeDetail": "DWD_ARRIVAL_NOTICE_LINE",
    "Receipt": "DWD_GOODS_RECEIPT",
    "ReceiptDetail": "DWD_GOODS_RECEIPT_LINE",
    "PurchaseRequisitionDetail": "DWD_PURCHASE_REQUISITION_LINE",
    "RequisitionOrderLink": "DWD_REQUISITION_ORDER_LINK",
    "SupplierPriceList": "DWD_SUPPLIER_PRICE_LIST_HEADER",
    "SupplierPriceDetail": "DWD_SUPPLIER_PRICE_LIST",
    "PurchaseOrder": "DWD_PURCHASE_ORDER",
    "PurchaseOrderDetail": "DWD_PURCHASE_ORDER_LINE",
    "Customer": "DWD_CUSTOMER",
    "Supplier": "DWD_SUPPLIER",
    "SupplierPriceConf": "DWD_SUPPLIER_PRICE_LIST_CONFIG",
    "Carrier": "DWD_CARRIER",
    "Quotation": "DWD_QUOTATION",
    "QuotationDetail": "DWD_QUOTATION_LINE",
    "PurchaseInvoice": "DWD_PURCHASE_INVOICE",
    "PurchaseInvoiceDetail": "DWD_PURCHASE_INVOICE_LINE",
    "Payment": "DWD_SUPPLIER_PAYMENT",
    "PaymentDetail": "DWD_SUPPLIER_PAYMENT_LINE",
}

# ETL 技术列，不建本体属性
SKIP_COLS = {"etl_load_ts"}

# DWD 列名 -> 属性中文名（新建/覆盖旧误译）。按列名匹配，X3 已映射且名字合适的保留原名。
COLUMN_CN: dict[str, str] = {
    # 用户明确要求区分的两个区分维度
    "zero_stock_flag": "零库存供应商标志",  # X3 YPTHFLGM_0，旧名"收货模式"误译
    "material_category": "物料类别",  # X3 TCLCOD_0，旧名"物料类型代码"
    # DWD 中语义与 X3 原名有位移的关键事实列
    "received_qty": "收货数量",  # 旧 ReceiptDetail"库存数量"(QTYUOM_0) 语义不对
    "received_qty_price_uom": "收货数量(采购单位)",  # QTYPUU_0
    "rejected_qty": "拒收数量",  # RRRQTYPUU_0，旧名"采购R数量"
    "net_unit_price": "净价",  # NETPRI_0
    "gross_unit_price": "采购价",  # GROPRI_0
    # DWD 新列（旧本体未建模的 X3 列 / 需要明确中文名的列）
    "payment_term_type": "付款条件",  # BPTNUM_0，旧 Supplier"承运人"误译
    "payment_code": "付款码",  # BPRPAY_0
    "standard_volume": "标准体积",  # ITMVOU_0
    "purchase_base_price": "采购基价",  # PURBASPRI_0
    "stock_location": "库位",  # LOCNUM_0
    "lot_qty": "批次数量",  # MFGLOTQTY_0
    "min_order_qty": "最小订购量",  # REOMINQTY_0
    "subcontract_price": "外协价格",  # YPRI_0
    "arrival_notice_line_no": "到货行号",  # YPTDLIN_0
    "planned_receipt_date": "计划收货日期",  # RCPDAT_0
    "arrival_date": "到货日期",  # ARVDAT_0
    "buyer_code": "采购员",  # LINBUY_0
    "material_code": "物料编号",  # CPNITMREF_0
    "min_qty": "最小数量",  # MINQTY_0
    "max_qty": "最大数量",  # MAXQTY_0
    "revision_no": "修订号",  # REVNUM_0
    "order_reference": "订单参考",  # ORDREF_0
    "requested_receipt_date": "需求日期",  # DEMRCPDAT_0
    "quotation_no": "询价单号",  # PQHNUM_0
    "quotation_line_no": "询价单行号",  # PPDLIN_0
    "supplier_code": "关联供应商编号",  # BPCBPSNUM_0
    "carrier_code": "承运商编号",  # BPTNUM_0
    "carrier_name": "承运商名称",  # BPTNAM_0
    "company_code": "公司",  # CPY_0
    "facility_code": "工厂",  # FCY_0 / PRHFCY_0
    "invoice_document_no": "发票凭证号",  # INVNUM_0
    "line_type": "行类型",  # LINTYP_0
    "receipt_date": "收货日期",  # RCPDAT_0
    "bill_date": "票据日期",  # BILDAT_0
    "payment_type": "付款类型",  # PAYTYP_0
    "line_amount": "行金额",  # AMTLIN_0
    # 价格表三件套：明细补的关联键（新建属性需要中文名）
    "price_list_code": "价格表号",  # PLI_0
    "price_list_record": "价格表记录",  # PLICRD_0
}

# Oracle 类型 -> 本体 data_type
_TYPE_MAP: dict[str, str] = {
    "VARCHAR2": "STRING",
    "VARCHAR": "STRING",
    "NUMBER": "DECIMAL",
    "INTEGER": "INT",
    "DATE": "DATETIME",
    "TIMESTAMP": "DATETIME",
}


def _dwdDataType(oracleType: str) -> str:
    base = oracleType.upper()
    for key in ("VARCHAR2", "VARCHAR", "NUMBER", "INTEGER", "TIMESTAMP", "DATE"):
        if base.startswith(key):
            return _TYPE_MAP[key]
    return "STRING"


# metric formula 改写：X3 列 -> DWD 列
METRIC_FORMULAS: dict[str, str] = {
    "KPI_TOTAL_QTY": "SUM(t.order_qty)",
    "KPI_RECEIPT_QTY": "SUM(t.received_qty)",
    "KPI_ORDER_AMT": "SUM(t.line_amount_excl_tax)",
    "KPI_AVG_PRICE": "SUM(t.line_amount_excl_tax) / NULLIF(SUM(t.order_qty), 0)",
    "KPI_RETURN_QTY": "SUM(t.returned_qty)",
    "KPI_AVG_RECEIPT_PRICE": "SUM(t.line_amount_excl_tax) / NULLIF(SUM(t.received_qty), 0)",
    "KPI_INVOICE_AMT": "SUM(t.line_amount_excl_tax)",
    "KPI_PAYMENT_AMT": "SUM(t.payment_amount)",
    "KPI_QUOTATION_QTY": "SUM(t.quantity)",
}

# 显式删除的属性（历史脏数据）：(class_name, property_name)
# SupplierPriceConf.没有用处 与 价格清单说明 同映射 LANDESSHO_0，
# rebind 后两属性都叫 local_name 会撞 (class_id, property_name) 唯一约束。
DELETE_PROPS: set[tuple[str, str]] = {("SupplierPriceConf", "没有用处")}

# 新增 join：(源类, 源列, 目标类, 目标列)
# 明细↔表头 用 (price_list_code, price_list_record) 版本键；配置↔明细 用价目表号。
# 这两条 join 此前因明细缺关联键列被删除，DWD 补键后恢复。
NEW_JOINS: list[tuple[str, list[str], str, list[str]]] = [
    (
        "SupplierPriceDetail",
        ["price_list_code", "price_list_record"],
        "SupplierPriceList",
        ["price_list_code", "price_list_record"],
    ),
    (
        "SupplierPriceConf",
        ["price_list_code"],
        "SupplierPriceDetail",
        ["price_list_code"],
    ),
]

# =============================================================================
# 计划计算（纯函数，可 --dump-spec / --dry-run 预览）
# =============================================================================


def _x3ToDwd(cols: dict[str, dict]) -> dict[str, str]:
    return {c["x3"]: col for col, c in cols.items() if c["x3"]}


class PropertyPlan:
    def __init__(self) -> None:
        self.renames: dict[str, str] = {}  # dwd_col -> 新中文名
        self.new_names: dict[str, str] = {}  # dwd_col -> 中文名
        self.unmatched_new: list[str] = []  # 无名字的 DWD 新列（跳过创建）
        self.to_delete: list[OntologyProperty] = []


def planProperties(
    cols: dict[str, dict],
    props: list[OntologyProperty],
) -> PropertyPlan:
    """对单个 DWD 表规划属性 rebind：返回改名/新建/删除。

    映射属性：source_column 由 X3 列 -> DWD 列；名字按 COLUMN_CN 覆盖。
    新建属性：DWD 列无对应旧属性 -> 按 COLUMN_CN 取名，缺名记 unmatched_new。
    删除属性：旧 X3 列在 DWD 中不存在。
    """
    plan = PropertyPlan()
    x3_to_dwd = _x3ToDwd(cols)
    used: set[str] = set()
    for p in props:
        src = p.source_column
        if not src:
            continue
        new_col = x3_to_dwd.get(src)
        if new_col is None and src in cols:
            new_col = src  # 已是 DWD 列名
        if new_col is None:
            plan.to_delete.append(p)
            continue
        used.add(new_col)
        if new_col in COLUMN_CN:
            plan.renames[new_col] = COLUMN_CN[new_col]
    for col, c in cols.items():
        if col in SKIP_COLS or col in used:
            continue
        cn = COLUMN_CN.get(col)
        if cn:
            plan.new_names[col] = cn
        else:
            plan.unmatched_new.append(col)
    return plan


def _translateColumn(
    table: str,
    column: str,
    spec: dict[str, dict[str, dict]],
) -> str | None:
    """把列翻译到类目标表列：ODS 表原样返回；DWD 表经 X3 映射。"""
    if table in _ODS_TABLES:
        return column
    cols = spec.get(table)
    if cols is None:
        return None
    x3_to_dwd = _x3ToDwd(cols)
    if column in x3_to_dwd:
        return x3_to_dwd[column]
    if column in cols:
        return column
    return None


def planJoins(
    joins: list[OntologyJoin],
    classToTable: dict[int, str],
    spec: dict[str, dict[str, dict]],
) -> tuple[list[tuple[OntologyJoin, list[str], list[str]]], list[OntologyJoin]]:
    """规划 join 翻译：返回 (保留 [(join, 新源列, 新目标列)], 删除)。

    不原地修改 join，翻译结果由调用方应用（保证预览不改内存态）。
    """
    keep: list[tuple[OntologyJoin, list[str], list[str]]] = []
    drop: list[OntologyJoin] = []
    for j in joins:
        new_src = [
            _translateColumn(classToTable.get(j.source_class_id, ""), c, spec)
            for c in j.source_columns
        ]
        new_tgt = [
            _translateColumn(classToTable.get(j.target_class_id, ""), c, spec)
            for c in j.target_columns
        ]
        if any(c is None for c in new_src + new_tgt):
            drop.append(j)
            continue
        keep.append((j, new_src, new_tgt))  # type: ignore[arg-type]
    return keep, drop


def dropJunkProps(
    classes: list[OntologyClass],
    propsByClass: dict[int, list[OntologyProperty]],
) -> list[OntologyProperty]:
    """删除 DELETE_PROPS 指定的脏属性，并原地从本地 propsByClass 快照移除。

    只改内存快照（每次运行从 DB 重建），由调用方 session.delete 落库。
    """
    classIdByName = {c.class_name: c.id for c in classes}
    dropped: list[OntologyProperty] = []
    for class_name, prop_name in DELETE_PROPS:
        cid = classIdByName.get(class_name)
        if cid is None:
            continue
        props = propsByClass.get(cid, [])
        for p in list(props):
            if p.property_name == prop_name:
                props.remove(p)
                dropped.append(p)
    return dropped


def planNewJoins(
    classes: list[OntologyClass],
    joins: list[OntologyJoin],
) -> list[dict]:
    """生成 NEW_JOINS 中尚未存在的 join 定义（幂等：已有同键 join 则跳过）。

    返回 dict 列表，由调用方创建 OntologyJoin；只计算不写库。
    """
    classIdByName = {c.class_name: c.id for c in classes}
    existing = {
        (j.source_class_id, tuple(j.source_columns), j.target_class_id, tuple(j.target_columns))
        for j in joins
    }
    out: list[dict] = []
    for src_name, src_cols, tgt_name, tgt_cols in NEW_JOINS:
        sid = classIdByName.get(src_name)
        tid = classIdByName.get(tgt_name)
        if sid is None or tid is None:
            continue
        key = (sid, tuple(src_cols), tid, tuple(tgt_cols))
        if key in existing:
            continue
        out.append({
            "source_class_id": sid,
            "source_columns": src_cols,
            "target_class_id": tid,
            "target_columns": tgt_cols,
        })
    return out


# =============================================================================
# 执行
# =============================================================================


def _thbiPassword() -> str:
    """THBI 密码走环境变量 THBI_PASSWORD，绝不写死进脚本/文档。"""
    pwd = os.environ.get("THBI_PASSWORD")
    if not pwd:
        raise RuntimeError("缺少环境变量 THBI_PASSWORD（THBI 数据仓库密码）")
    return pwd


async def _registerDataSource(session) -> DataSource:
    """注册 THBI 为默认数据源（已存在则复用）；默认排他。"""
    thbi = (
        (await session.execute(select(DataSource).where(DataSource.name == "THBI-Oracle")))
        .scalars()
        .first()
    )
    if thbi is None:
        thbi = DataSource(
            name="THBI-Oracle",
            type="oracle",
            host="192.168.205.70",
            port=1521,
            database_name="X3V71ORA",
            username="THBI",
            password_encrypted=encryptApiKey(_thbiPassword()),
            description="THBI 数仓（ODS/DWD/DIM/DWS/ADS），默认业务数据源",
            is_active=True,
            is_default=True,
            created_by="rebind_ontology_thbi",
            oracle_version="11g",
        )
        session.add(thbi)
        await session.flush()
        logger.info("注册 THBI 数据源 id=%s", thbi.id)
    else:
        if not thbi.password_encrypted:
            thbi.password_encrypted = encryptApiKey(_thbiPassword())
        logger.info("复用 THBI 数据源 id=%s", thbi.id)
    # 默认排他：全库清 is_default，再置 THBI
    await session.execute(update(DataSource).values(is_default=False))
    thbi.is_default = True
    thbi.is_active = True
    return thbi


async def _dumpOrDry(args, session, spec, classes, classById, classToTable, propsByClass, joins, metrics):
    """dump-spec / dry-run 共用：输出计划，不写库。"""
    dropped_junk = dropJunkProps(classes, propsByClass)
    if dropped_junk:
        print("===== 显式删除脏属性（DELETE_PROPS）=====")
        for p in dropped_junk:
            print(f"  - 删除 {p.property_name}（同列重复映射）")

    print("===== 属性 rebind 计划 =====")
    total_delete = 0
    for c in classes:
        table = classToTable.get(c.id)
        if table is None:
            continue
        if table in _ODS_TABLES:
            print(f"  [保留 ODS] {c.class_name} -> {table}")
            continue
        cols = spec.get(table)
        if cols is None:
            print(f"  [!! 无 DWD 规格] {c.class_name} -> {table}")
            continue
        plan = planProperties(cols, propsByClass[c.id])
        total_delete += len(plan.to_delete)
        kept = len(propsByClass[c.id]) - len(plan.to_delete)
        line = f"[{c.class_name}] -> {table} 保留={kept} 删={len(plan.to_delete)} 新建={len(plan.new_names)}"
        if plan.unmatched_new:
            line += f" 缺中文名跳过={plan.unmatched_new}"
        print("  " + line)
    print(f"\n合计删除属性 {total_delete} 个")

    print("\n===== Join 翻译预览 =====")
    kept_j, dropped_j = planJoins(joins, classToTable, spec)
    print(f"  保留 {len(kept_j)} / 删除 {len(dropped_j)}")
    for j in dropped_j:
        src = classById[j.source_class_id].class_name
        tgt = classById[j.target_class_id].class_name
        print(f"  - 删除 {src}({j.source_columns}) -> {tgt}({j.target_columns})")
    new_j = planNewJoins(classes, joins)
    if new_j:
        print(f"  + 新增 {len(new_j)} 条 DWD 价格表关联")
        for d in new_j:
            s = classById[d["source_class_id"]].class_name
            t = classById[d["target_class_id"]].class_name
            print(f"  + {s}({d['source_columns']}) -> {t}({d['target_columns']})")

    print("\n===== Metric 公式 =====")
    for m in metrics:
        new_f = METRIC_FORMULAS.get(m.metric_name, "（无改写）")
        print(f"  {m.metric_name}: {m.formula} -> {new_f}")

    if args.dump_spec:
        print("\n===== DWD 新列（需中文名，当前缺名跳过）=====")
        seen: set[str] = set()
        for c in classes:
            table = classToTable.get(c.id)
            if not table or table in _ODS_TABLES:
                continue
            cols = spec.get(table)
            if cols is None:
                continue
            plan = planProperties(cols, propsByClass[c.id])
            for col in plan.unmatched_new:
                if col not in seen:
                    seen.add(col)
                    print(f"  {table}.{col}")
        print("\n===== 属性改名预览 =====")
        for c in classes:
            table = classToTable.get(c.id)
            if not table or table in _ODS_TABLES:
                continue
            cols = spec.get(table)
            if cols is None:
                continue
            plan = planProperties(cols, propsByClass[c.id])
            for col, cn in plan.renames.items():
                src_x3 = cols[col]["x3"]
                print(f"  {c.class_name}.{src_x3} -> {cn} (DWD col={col})")


async def run(args) -> None:
    engine = getEngine()
    engine.echo = False
    factory = getSessionFactory()

    async with factory() as session:
        spec = parseDwdFiles([DW_DIR / f for f in DWD_FILES])
        if args.dump_spec:
            print(dumpSpec(spec))
            print(f"\n共 {len(spec)} 张 DWD 表（25 张：20 类切 DWD + 2 类留 ODS）")

        classes = (await session.execute(select(OntologyClass))).scalars().all()
        classById = {c.id: c for c in classes}
        classToTable = {
            c.id: CLASS_TABLE[c.class_name]
            for c in classes
            if c.class_name in CLASS_TABLE
        }
        propsByClass = {c.id: list(c.properties) for c in classes}
        joins = (await session.execute(select(OntologyJoin))).scalars().all()
        metrics = (await session.execute(select(OntologyMetric))).scalars().all()

        if args.dump_spec or args.dry_run:
            await _dumpOrDry(
                args, session, spec, classes, classById, classToTable,
                propsByClass, joins, metrics,
            )
            return

        # ---- 应用变更 ----
        thbi = await _registerDataSource(session)
        await session.commit()
        logger.info("数据源：THBI 默认 id=%s，ZJTH 非默认", thbi.id)

        # 显式删除脏属性（如 SupplierPriceConf.没有用处，同列重复映射）
        for p in dropJunkProps(classes, propsByClass):
            await session.delete(p)
            logger.info("删脏属性 %s.%s", p.class_id, p.property_name)

        # 类 rebind
        for c in classes:
            table = classToTable.get(c.id)
            if table is None or c.source_table == table:
                continue
            c.source_table = table
            note = "\n（本体重建：绑定 %s，英文 snake_case 标准命名）" % table
            c.description = (c.description or "") + note
            logger.info("类 %s source_table -> %s", c.class_name, table)

        # 属性 rebind
        for c in classes:
            table = classToTable.get(c.id)
            if not table or table in _ODS_TABLES:
                continue
            cols = spec.get(table)
            if cols is None:
                continue
            plan = planProperties(cols, propsByClass[c.id])
            for p in plan.to_delete:
                await session.delete(p)
                logger.info("删属性 %s.%s", c.class_name, p.property_name)
            # 先 flush 删除，释放 (class_id, property_name) 唯一名，
            # 避免新 DWD 列沿用同名（如 需求日期: RETRCPDAT_0 -> DEMRCPDAT_0）时 INSERT 撞唯一约束。
            await session.flush()
            for p in propsByClass[c.id]:
                if p in plan.to_delete:
                    continue
                new_col = _x3ToDwd(cols).get(p.source_column or "")
                if new_col is None and p.source_column in cols:
                    new_col = p.source_column
                if new_col is None:
                    continue
                p.source_column = new_col
                p.data_type = _dwdDataType(cols[new_col]["type"])
                p.is_primary_key = p.is_primary_key or bool(cols[new_col]["pk"])
                new_name = COLUMN_CN.get(new_col)
                if new_name and new_name != p.property_name:
                    logger.info(
                        "属性改名 %s.%s -> %s (col=%s)",
                        c.class_name, p.property_name, new_name, new_col,
                    )
                    p.property_name = new_name
            for col, cn in plan.new_names.items():
                c_col = cols[col]
                session.add(OntologyProperty(
                    class_id=c.id,
                    property_name=cn,
                    property_alias=None,
                    description=f"DWD 标准列 {col}（X3 {c_col['x3']}）",
                    data_type=_dwdDataType(c_col["type"]),
                    is_primary_key=bool(c_col["pk"]),
                    is_foreign_key=False,
                    source_column=col,
                ))
                logger.info("新建属性 %s.%s (col=%s)", c.class_name, cn, col)

        # join rebind
        kept_j, dropped_j = planJoins(joins, classToTable, spec)
        for j in dropped_j:
            await session.delete(j)
            logger.info(
                "删 join %s->%s（列在 DWD 中缺失）",
                classById[j.source_class_id].class_name,
                classById[j.target_class_id].class_name,
            )
        for j, new_src, new_tgt in kept_j:
            if j.source_columns != new_src or j.target_columns != new_tgt:
                j.source_columns = new_src
                j.target_columns = new_tgt
                j.join_key = makeJoinKey(j.source_class_id, new_src, j.target_class_id, new_tgt)
                logger.info(
                    "join 重绑 %s->%s: %s == %s",
                    classById[j.source_class_id].class_name,
                    classById[j.target_class_id].class_name,
                    new_src, new_tgt,
                )
        for nd in planNewJoins(classes, joins):
            src_id = nd["source_class_id"]
            tgt_id = nd["target_class_id"]
            session.add(OntologyJoin(
                source_class_id=src_id,
                source_columns=nd["source_columns"],
                target_class_id=tgt_id,
                target_columns=nd["target_columns"],
                join_type="INNER",
                relation_type="business",
                description="DWD 价格表关联（版本键/价目表号）",
                join_key=makeJoinKey(src_id, nd["source_columns"], tgt_id, nd["target_columns"]),
            ))
            logger.info(
                "新增 join %s.%s == %s.%s",
                classById[src_id].class_name, nd["source_columns"],
                classById[tgt_id].class_name, nd["target_columns"],
            )

        # metric rebind
        for m in metrics:
            new_f = METRIC_FORMULAS.get(m.metric_name)
            if new_f and new_f != m.formula:
                m.formula = new_f
                logger.info("metric %s formula -> %s", m.metric_name, new_f)

        await session.commit()

        # ---- Neo4j 同步（阻塞 I/O 移线程） ----
        fresh_props = (await session.execute(select(OntologyProperty))).scalars().all()
        await asyncio.to_thread(_syncNeo4j, classes, fresh_props, metrics)

        logger.info("完成：数据源/类/属性/join/metric 已 rebind，Neo4j 已同步")
        logger.info("下一步：uv run python scripts/backfill_milvus_embeddings.py --cleanup")
        await session.commit()

    await engine.dispose()


def _syncNeo4j(
    classes: list[OntologyClass],
    props: list[OntologyProperty],
    metrics: list[OntologyMetric],
) -> None:
    """Neo4j 同步：upsert 类/属性/指标 + HAS_PROPERTY/REFERENCES/DERIVED_FROM。

    单个节点失败只跳过并告警（PG 已就绪，可重跑补齐）。
    """
    classById = {c.id: c for c in classes}
    for c in classes:
        try:
            neo4j.upsertClassNode(
                id=c.id,
                name=c.class_name,
                alias=c.class_alias,
                description=c.description,
                sourceTable=c.source_table or "",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Neo4j 同步失败 Class %s: %s", c.class_name, exc)
    for p in props:
        try:
            neo4j.upsertPropertyNode(
                id=p.id,
                name=p.property_name,
                alias=p.property_alias,
                dataType=p.data_type,
                sourceColumn=p.source_column or "",
                isPrimaryKey=p.is_primary_key,
                isForeignKey=p.is_foreign_key,
            )
            neo4j.linkClassHasProperty(p.class_id, p.id)
            neo4j.reconcilePropertyReferences(p.id, p.ref_class_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Neo4j 同步失败属性 %s: %s", p.property_name, exc)
    for m in metrics:
        try:
            neo4j.upsertMetricNode(
                id=m.id,
                name=m.metric_name,
                alias=m.metric_alias,
                formula=m.formula,
                aggFunction=m.agg_function,
            )
            if m.target_class_id:
                neo4j.linkMetricDerivedFrom(m.id, m.target_class_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Neo4j 同步失败指标 %s: %s", m.metric_name, exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="本体重建：本体绑定切到 THBI DWD 表")
    parser.add_argument("--dump-spec", action="store_true", help="只输出 DWD 规格与计划，不写库")
    parser.add_argument("--dry-run", action="store_true", help="预览属性/join/metric 变更，不写库")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
