"""采购源侧能力实测探针（只读）。

用途：为「问卷回收摄入」提供**可复跑的事实底座**——某张表有没有数据、某个日期列
填充率多少、某个状态列的值域是什么。没有这层事实，口径讨论只能靠问卷里的文字断言。

纪律（不可放宽）：

1. **只读**：全部语句是 ``SELECT`` 聚合（``COUNT`` / ``MIN`` / ``MAX`` / ``GROUP BY``），
   不取明细行，不取个人字段。
2. **走系统自带只读通道**：``BusinessDbAdapter.execute_read_only``，即 SQL Guard
   校验过的路径；不自己拼连接、不自己解密口令。
3. **不落密**：口令由系统 Fernet 解密后仅用于建连，不打印、不落盘、不进 argv。
4. **目标库**：``--datasource-id`` 指定；默认同时跑 THBI Oracle(1) 与 SRM-TH-01(10)。

用法::

    cd backend
    uv run python scripts/probe_procurement_source_capability.py
    uv run python scripts/probe_procurement_source_capability.py --datasource-id 1
    uv run python scripts/probe_procurement_source_capability.py --out ../docs/update/1118/M0/evidence/source-capability-scan.txt

2026-11-18 首跑结果见 ``docs/update/1118/M0/evidence/source-capability-scan.txt``。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

sys.path.insert(0, os.getcwd())

from app.config import getSettings  # noqa: E402
from app.domain.models import DataSource  # noqa: E402
from app.infrastructure.business_db_pool import get_adapter  # noqa: E402


@dataclass(frozen=True)
class Probe:
    """一条只读探针：标题 + SQL。"""

    title: str
    sql: str


# ---------------------------------------------------------------- THBI Oracle

THBI_PROBES: tuple[Probe, ...] = (
    Probe("行数 · 采购订单行", "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_ORDER_LINE"),
    Probe(
        "填充率 · 承诺交货日期 / 请求交货日期",
        "SELECT COUNT(PROMISED_RECEIPT_DATE) AS promised_filled, COUNT(REQUESTED_RECEIPT_DATE) AS requested_filled, "
        "COUNT(*) AS total FROM THBI.DWD_PURCHASE_ORDER_LINE",
    ),
    Probe(
        "订单行按年分布",
        "SELECT TO_CHAR(O.ORDER_DATE,'YYYY') AS y, COUNT(*) AS n "
        "FROM THBI.DWD_PURCHASE_ORDER_LINE O GROUP BY TO_CHAR(O.ORDER_DATE,'YYYY') ORDER BY 1",
    ),
    Probe(
        "订单行完成度分布（2025 起）—— A1.1「全完成/部分完成/未完成」三分档",
        "SELECT COUNT(*) AS lines, "
        "SUM(CASE WHEN O.RECEIVED_QTY >= O.ORDER_QTY THEN 1 ELSE 0 END) AS full_done, "
        "SUM(CASE WHEN O.RECEIVED_QTY > 0 AND O.RECEIVED_QTY < O.ORDER_QTY THEN 1 ELSE 0 END) AS partial, "
        "SUM(CASE WHEN NVL(O.RECEIVED_QTY,0) = 0 THEN 1 ELSE 0 END) AS none_done "
        "FROM THBI.DWD_PURCHASE_ORDER_LINE O WHERE O.ORDER_DATE >= DATE '2025-01-01'",
    ),
    Probe("行数 · 收货行", "SELECT COUNT(*) AS n FROM THBI.DWD_GOODS_RECEIPT_LINE"),
    Probe(
        "跨度 · 收货日期",
        "SELECT MIN(RECEIPT_DATE) AS min_d, MAX(RECEIPT_DATE) AS max_d FROM THBI.DWD_GOODS_RECEIPT_LINE",
    ),
    Probe(
        "收货行：2025 起有 PO 键的比例",
        "SELECT COUNT(*) AS total, COUNT(PO_NO) AS with_po, COUNT(PO_LINE_NO) AS with_po_line "
        "FROM THBI.DWD_GOODS_RECEIPT_LINE WHERE RECEIPT_DATE >= DATE '2025-01-01'",
    ),
    Probe("行数 · 到货检验行（IQC 落地表）", "SELECT COUNT(*) AS n FROM THBI.DWD_ARRIVAL_ORDER_DTL"),
    Probe(
        "填充率 · 到货数量 / 合格数量",
        "SELECT COUNT(ARR_QTY) AS qty_filled, COUNT(QUALIFIED_QTY) AS qual_filled, COUNT(*) AS total "
        "FROM THBI.DWD_ARRIVAL_ORDER_DTL",
    ),
    Probe(
        "不良行数 · 到货 > 合格（= A9 分子）",
        "SELECT COUNT(*) AS n FROM THBI.DWD_ARRIVAL_ORDER_DTL WHERE QUALIFIED_QTY < ARR_QTY",
    ),
    Probe(
        "到货检验：总行数 vs 去重 PO 行键（判「多次检验」= 返工可能）",
        "SELECT COUNT(*) AS rows_total, COUNT(DISTINCT A.PO_NO || '#' || A.PO_LINE_NO) AS distinct_keys "
        "FROM THBI.DWD_ARRIVAL_ORDER_DTL A",
    ),
    Probe(
        "到货检验：单据状态值域",
        "SELECT A.DOC_STATUS_CODE AS st, COUNT(*) AS n FROM THBI.DWD_ARRIVAL_ORDER_DTL A "
        "GROUP BY A.DOC_STATUS_CODE ORDER BY n DESC",
    ),
    Probe("行数 · 发票行", "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_INVOICE_LINE"),
    Probe(
        "填充率 · 发票行收货单键（三单匹配的接缝）",
        "SELECT COUNT(RECEIPT_NO) AS with_receipt, COUNT(*) AS total FROM THBI.DWD_PURCHASE_INVOICE_LINE",
    ),
    Probe("行数 · 供应商付款", "SELECT COUNT(*) AS n FROM THBI.DWD_SUPPLIER_PAYMENT"),
    Probe(
        "付款：日期列填充率（到期日/付款日/单据日/创建日）",
        "SELECT COUNT(DUE_DATE) AS due_f, COUNT(VALUE_DATE) AS val_f, COUNT(BILL_DATE) AS bill_f, "
        "COUNT(CREATION_DATE) AS cdate_f, COUNT(*) AS total FROM THBI.DWD_SUPPLIER_PAYMENT",
    ),
    Probe(
        "付款：到期日 − 单据日 的间隔区间（A8.2「收货后固定天数」核验）",
        "SELECT MIN(TRUNC(DUE_DATE)-TRUNC(BILL_DATE)) AS min_gap, MAX(TRUNC(DUE_DATE)-TRUNC(BILL_DATE)) AS max_gap "
        "FROM THBI.DWD_SUPPLIER_PAYMENT",
    ),
    Probe(
        "付款：付款日晚于单据日的笔数（A8.1「付款申请日」是否独立存在）",
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN TRUNC(VALUE_DATE) > TRUNC(BILL_DATE) THEN 1 ELSE 0 END) AS paid_after_bill "
        "FROM THBI.DWD_SUPPLIER_PAYMENT",
    ),
    Probe("行数 · 请购行", "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_REQUISITION_LINE"),
    Probe("行数 · 价目表", "SELECT COUNT(*) AS n FROM THBI.DWD_SUPPLIER_PRICE_LIST"),
    Probe(
        "价目表：有效期覆盖 2025 起的比例（A5.1「历史均价」可算性）",
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN P.VALID_FROM >= DATE '2025-01-01' THEN 1 ELSE 0 END) AS valid_from_2025 "
        "FROM THBI.DWD_SUPPLIER_PRICE_LIST P",
    ),
    Probe("行数 · 供应商主数据", "SELECT COUNT(*) AS n FROM THBI.DIM_SUPPLIER"),
    Probe("行数 · 报价行", "SELECT COUNT(*) AS n FROM THBI.DWD_QUOTATION_LINE"),
    Probe("行数 · 采购合同（A6 归因里治理层引用的表）", "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_CONTRACT"),
    Probe(
        "付款：银行日期（BANK_DATE）填充率 —— A8.1「付款申请日」的第三个候选",
        "SELECT COUNT(BANK_DATE) AS bank_f, COUNT(*) AS total FROM THBI.DWD_SUPPLIER_PAYMENT",
    ),
    Probe(
        "付款：银行日期 − 单据日 的间隔区间（判断 BANK_DATE 是不是「申请日」）",
        "SELECT MIN(TRUNC(BANK_DATE)-TRUNC(BILL_DATE)) AS min_gap, MAX(TRUNC(BANK_DATE)-TRUNC(BILL_DATE)) AS max_gap, "
        "SUM(CASE WHEN TRUNC(BANK_DATE) <> TRUNC(VALUE_DATE) THEN 1 ELSE 0 END) AS diff_from_value "
        "FROM THBI.DWD_SUPPLIER_PAYMENT",
    ),
    Probe(
        "付款：到期日 − 银行日期 的间隔区间（A8.2「收货后固定天数」再核）",
        "SELECT MIN(TRUNC(DUE_DATE)-TRUNC(BANK_DATE)) AS min_gap, MAX(TRUNC(DUE_DATE)-TRUNC(BANK_DATE)) AS max_gap "
        "FROM THBI.DWD_SUPPLIER_PAYMENT",
    ),
    Probe(
        "收货行：拒收 / 退货数量填充率与合计（A9 不良数量的备选源）",
        "SELECT COUNT(*) AS total, COUNT(REJECTED_QTY) AS rejected_f, COUNT(RETURNED_QTY) AS returned_f, "
        "SUM(NVL(REJECTED_QTY,0)) AS rejected_sum, SUM(NVL(RETURNED_QTY,0)) AS returned_sum "
        "FROM THBI.DWD_GOODS_RECEIPT_LINE WHERE RECEIPT_DATE >= DATE '2025-01-01'",
    ),
    Probe(
        "到货检验：检验日期 / 到货日期 填充率（2025 起）",
        "SELECT COUNT(*) AS total, COUNT(A.INSPECT_DATE) AS inspect_f, COUNT(A.ARR_DATE) AS arr_f "
        "FROM THBI.DWD_ARRIVAL_ORDER_DTL A WHERE A.ARR_DATE >= DATE '2025-01-01'",
    ),
    Probe(
        "到货检验：2025 起去重 PO 行键（判「首次检验」是否可判定）",
        "SELECT COUNT(*) AS rows_total, COUNT(DISTINCT A.PO_NO || '#' || A.PO_LINE_NO) AS distinct_keys "
        "FROM THBI.DWD_ARRIVAL_ORDER_DTL A WHERE A.ARR_DATE >= DATE '2025-01-01'",
    ),
    Probe(
        "价目表：近 1 年有价物料数 vs 订单行涉及物料数（A5.2「过去 1 年历史均价」可算性）",
        "SELECT (SELECT COUNT(DISTINCT P.MATERIAL_CODE) FROM THBI.DWD_SUPPLIER_PRICE_LIST P "
        "        WHERE P.VALID_FROM >= DATE '2025-01-01') AS priced_materials, "
        "(SELECT COUNT(DISTINCT O.MATERIAL_CODE) FROM THBI.DWD_PURCHASE_ORDER_LINE O "
        "        WHERE O.ORDER_DATE >= DATE '2025-01-01') AS ordered_materials FROM DUAL",
    ),
    Probe(
        "订单行：单价 / 不含税金额填充率（A3.3「订单金额加权」可算性）",
        "SELECT COUNT(*) AS total, COUNT(O.UNIT_PRICE) AS price_f, COUNT(O.LINE_AMOUNT_EXCL_TAX) AS amt_f "
        "FROM THBI.DWD_PURCHASE_ORDER_LINE O WHERE O.ORDER_DATE >= DATE '2025-01-01'",
    ),
    Probe(
        "请购行：创建日期 / 金额填充率（A3.1「起点=采购申请」可算性）",
        "SELECT COUNT(*) AS total, COUNT(L.CREATION_DATE) AS cdate_f, COUNT(L.LINE_AMOUNT_EXCL_TAX) AS amt_f "
        "FROM THBI.DWD_PURCHASE_REQUISITION_LINE L",
    ),
    Probe(
        "收货行 → 请购行 能否接上（A3 周期起点需要 PO→请购 的映射）",
        "SELECT COUNT(*) AS total, COUNT(O.PO_NO) AS with_po FROM THBI.DWD_PURCHASE_ORDER_LINE O "
        "WHERE O.ORDER_DATE >= DATE '2025-01-01'",
    ),
    Probe(
        "价目表：关键列填充率（A5.1/A5.2「基准价 = 过去 1 年历史均价」可算性）",
        "SELECT COUNT(*) AS total, COUNT(P.MATERIAL_CODE) AS mat_f, COUNT(P.UNIT_PRICE) AS price_f, "
        "COUNT(P.VALID_FROM) AS vfrom_f, COUNT(P.VALID_TO) AS vto_f, COUNT(DISTINCT P.PRICE_LIST_CODE) AS lists "
        "FROM THBI.DWD_SUPPLIER_PRICE_LIST P",
    ),
    Probe(
        "价目表：VALID_FROM ≥ 2025 的行的物料编码 / 单价填充率（上一问 distinct=0 的成因）",
        "SELECT COUNT(*) AS total, COUNT(P.MATERIAL_CODE) AS mat_f, COUNT(P.UNIT_PRICE) AS price_f, "
        "COUNT(DISTINCT P.MATERIAL_CODE) AS distinct_mat FROM THBI.DWD_SUPPLIER_PRICE_LIST P "
        "WHERE P.VALID_FROM >= DATE '2025-01-01'",
    ),
    Probe(
        "价目表：按物料的价目条数分布（多版本 vs 单版本）",
        "SELECT MIN(c) AS min_per_material, MAX(c) AS max_per_material, AVG(c) AS avg_per_material FROM "
        "(SELECT COUNT(*) AS c FROM THBI.DWD_SUPPLIER_PRICE_LIST GROUP BY MATERIAL_CODE)",
    ),
    Probe(
        "年度报价明细：行数 / 物料数 / 供应商数 / 价目表数（A5.1 基准价的真正候选源）",
        "SELECT COUNT(*) AS n, COUNT(DISTINCT Q.ITEM_CODE) AS items, COUNT(DISTINCT Q.SUPPLIER_CODE) AS sups, "
        "COUNT(DISTINCT Q.PRICE_LIST_CODE) AS lists FROM THBI.DWD_PURCHASE_QUOTATION_YEARLY_DTL Q",
    ),
    Probe(
        "年度报价明细：按年度分布",
        "SELECT Q.SEG_YEAR AS y, COUNT(*) AS n FROM THBI.DWD_PURCHASE_QUOTATION_YEARLY_DTL Q "
        "GROUP BY Q.SEG_YEAR ORDER BY 1",
    ),
    Probe(
        "年度报价明细：2025/2026 的不含税单价与生效区间填充率",
        "SELECT COUNT(*) AS total, COUNT(Q.UNIT_PRICE_EX_TAX) AS net_f, COUNT(Q.SEG_START_DATE) AS seg_f, "
        "COUNT(Q.SEG_END_DATE) AS segend_f FROM THBI.DWD_PURCHASE_QUOTATION_YEARLY_DTL Q WHERE Q.SEG_YEAR IN (2025,2026)",
    ),
    Probe(
        "基准价覆盖率（订单行级，供应商+物料 联合命中 2025+ 年度报价）",
        "SELECT COUNT(*) AS order_lines, SUM(CASE WHEN Q.SUPPLIER_CODE IS NOT NULL THEN 1 ELSE 0 END) AS hit_lines FROM "
        "THBI.DWD_PURCHASE_ORDER_LINE O LEFT JOIN "
        "(SELECT DISTINCT SUPPLIER_CODE, ITEM_CODE FROM THBI.DWD_PURCHASE_QUOTATION_YEARLY_DTL WHERE SEG_YEAR >= 2025) Q "
        "ON Q.SUPPLIER_CODE=O.SUPPLIER_CODE AND Q.ITEM_CODE=O.MATERIAL_CODE WHERE O.ORDER_DATE >= DATE '2025-01-01'",
    ),
    Probe(
        "基准价覆盖率（订单行级，仅物料命中）",
        "SELECT COUNT(*) AS order_lines, SUM(CASE WHEN Q.ITEM_CODE IS NOT NULL THEN 1 ELSE 0 END) AS hit_lines FROM "
        "THBI.DWD_PURCHASE_ORDER_LINE O LEFT JOIN "
        "(SELECT DISTINCT ITEM_CODE FROM THBI.DWD_PURCHASE_QUOTATION_YEARLY_DTL WHERE SEG_YEAR >= 2025) Q "
        "ON Q.ITEM_CODE=O.MATERIAL_CODE WHERE O.ORDER_DATE >= DATE '2025-01-01'",
    ),
    Probe(
        "报价单头 / 报价行 行数（A3 周期起点经报价单中转的可能性）",
        "SELECT (SELECT COUNT(*) FROM THBI.DWD_QUOTATION) AS q_hdr, "
        "(SELECT COUNT(*) FROM THBI.DWD_QUOTATION_LINE) AS q_line FROM DUAL",
    ),
    Probe(
        "对账明细行数（D1.6「不做对账」的范围外对象）",
        "SELECT COUNT(*) AS n FROM THBI.DWD_RECONCILIATION_BILL_DTL",
    ),
    # -- 对照组：34 个候选类的「_DTL 变体」 vs 指标实际引用的墓碑类物理表 ---------
    # A8 §3.1：7 个墓碑类仍被 8 条 KPI 引用（id14/16/17/20/22/25/27），
    # 而 A8 的 34 候选用的是 _DTL 命名。两套名字指向**不同的物理表**吗？
    Probe(
        "对照行数 · DWD_PURCHASE_ORDER_DTL（34 候选变体）",
        "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_ORDER_DTL",
    ),
    Probe(
        "对照行数 · DWD_GOODS_RECEIPT_DTL（34 候选变体）",
        "SELECT COUNT(*) AS n FROM THBI.DWD_GOODS_RECEIPT_DTL",
    ),
    Probe(
        "对照行数 · DWD_AP_INVOICE_DTL（34 候选变体）",
        "SELECT COUNT(*) AS n FROM THBI.DWD_AP_INVOICE_DTL",
    ),
    Probe(
        "对照行数 · DWD_ARRIVAL_ORDER_DTL（34 候选变体，与前同表）",
        "SELECT COUNT(*) AS n, COUNT(ARR_QTY) AS arr_qty, COUNT(QUALIFIED_QTY) AS qualified_qty "
        "FROM THBI.DWD_ARRIVAL_ORDER_DTL",
    ),
    Probe("对照行数 · DWD_SUPPLIER_PRICE_LIST_HEADER", "SELECT COUNT(*) AS n FROM THBI.DWD_SUPPLIER_PRICE_LIST_HEADER"),
    Probe("对照行数 · DWD_SUPPLIER_PRICE_LIST_CONFIG", "SELECT COUNT(*) AS n FROM THBI.DWD_SUPPLIER_PRICE_LIST_CONFIG"),
    Probe("对照行数 · ODS_PORDER", "SELECT COUNT(*) AS n FROM THBI.ODS_PORDER"),
    Probe("对照行数 · ODS_YPRECEIPTD", "SELECT COUNT(*) AS n FROM THBI.ODS_YPRECEIPTD"),
    Probe("对照行数 · ODS_PINVOICED", "SELECT COUNT(*) AS n FROM THBI.ODS_PINVOICED"),
    Probe("对照行数 · ODS_PAYMENTD", "SELECT COUNT(*) AS n FROM THBI.ODS_PAYMENTD"),
    Probe("对照行数 · ODS_PAYMENTH", "SELECT COUNT(*) AS n FROM THBI.ODS_PAYMENTH"),
    Probe("对照行数 · ODS_PORDERQ", "SELECT COUNT(*) AS n FROM THBI.ODS_PORDERQ"),
    Probe("对照行数 · ODS_PQUOTAT", "SELECT COUNT(*) AS n FROM THBI.ODS_PQUOTAT"),
    Probe("对照行数 · ODS_ITMMASTER", "SELECT COUNT(*) AS n FROM THBI.ODS_ITMMASTER"),
    Probe(
        "对照 · 两套命名的关键列存在性（_DTL 候选 vs 墓碑类物理表）",
        "SELECT table_name, COUNT(*) AS col_cnt, "
        "MAX(CASE WHEN column_name='ORDER_DATE' THEN 1 ELSE 0 END) AS c_order_date, "
        "MAX(CASE WHEN column_name='PROMISED_RECEIPT_DATE' THEN 1 ELSE 0 END) AS c_promised, "
        "MAX(CASE WHEN column_name='ORDER_QTY' THEN 1 ELSE 0 END) AS c_order_qty, "
        "MAX(CASE WHEN column_name='LINE_AMOUNT_EXCL_TAX' THEN 1 ELSE 0 END) AS c_line_amount, "
        "MAX(CASE WHEN column_name='MATERIAL_CODE' THEN 1 ELSE 0 END) AS c_material, "
        "MAX(CASE WHEN column_name='SUPPLIER_CODE' THEN 1 ELSE 0 END) AS c_supplier, "
        "MAX(CASE WHEN column_name='PO_NO' THEN 1 ELSE 0 END) AS c_po_no, "
        "MAX(CASE WHEN column_name='PO_LINE_NO' THEN 1 ELSE 0 END) AS c_po_line, "
        "MAX(CASE WHEN column_name='RECEIPT_DATE' THEN 1 ELSE 0 END) AS c_receipt_date, "
        "MAX(CASE WHEN column_name='REJECTED_QTY' THEN 1 ELSE 0 END) AS c_rejected, "
        "MAX(CASE WHEN column_name='RECEIPT_NO' THEN 1 ELSE 0 END) AS c_receipt_no, "
        "MAX(CASE WHEN column_name='INVOICE_DATE' THEN 1 ELSE 0 END) AS c_invoice_date, "
        "MAX(CASE WHEN column_name='BILL_DATE' THEN 1 ELSE 0 END) AS c_bill_date, "
        "MAX(CASE WHEN column_name='VALUE_DATE' THEN 1 ELSE 0 END) AS c_value_date, "
        "MAX(CASE WHEN column_name='DUE_DATE' THEN 1 ELSE 0 END) AS c_due_date, "
        "MAX(CASE WHEN column_name='BANK_DATE' THEN 1 ELSE 0 END) AS c_bank_date, "
        "MAX(CASE WHEN column_name='ARR_QTY' THEN 1 ELSE 0 END) AS c_arr_qty, "
        "MAX(CASE WHEN column_name='QUALIFIED_QTY' THEN 1 ELSE 0 END) AS c_qualified, "
        "MAX(CASE WHEN column_name='ARR_DATE' THEN 1 ELSE 0 END) AS c_arr_date, "
        "MAX(CASE WHEN column_name='INSPECT_DATE' THEN 1 ELSE 0 END) AS c_inspect_date "
        "FROM ALL_TAB_COLUMNS WHERE owner='THBI' AND table_name IN ("
        "'DWD_PURCHASE_ORDER_DTL','DWD_PURCHASE_ORDER_LINE','DWD_GOODS_RECEIPT_DTL',"
        "'DWD_GOODS_RECEIPT_LINE','DWD_AP_INVOICE_DTL','DWD_PURCHASE_INVOICE',"
        "'DWD_PURCHASE_INVOICE_LINE','DWD_ARRIVAL_ORDER_DTL','DWD_SUPPLIER_PAYMENT',"
        "'ODS_PORDERQ','ODS_PINVOICED','ODS_YPRECEIPTD','ODS_PAYMENTD') "
        "GROUP BY table_name ORDER BY table_name",
    ),
    # -- 复活墓碑类（KPI 读路径实际引用的 7 张表） --------------------------------
    Probe(
        "复活墓碑类 · DWD_REQUISITION_ORDER_LINK 行数与键覆盖（PO↔请购 关联表）",
        "SELECT COUNT(*) AS n, COUNT(DISTINCT PO_NO) AS pos, COUNT(DISTINCT PO_LINE_NO) AS po_lines, "
        "COUNT(DISTINCT REQUISITION_NO) AS reqs FROM THBI.DWD_REQUISITION_ORDER_LINK",
    ),
    Probe(
        "复活墓碑类 · DWD_PURCHASE_REQUISITION_LINE 行数与关键列",
        "SELECT COUNT(*) AS n, COUNT(REQUISITION_NO) AS req_no, COUNT(QUOTATION_NO) AS quo_no, "
        "COUNT(CREATION_DATE) AS creation_date, COUNT(ORDERED_FLAG) AS ordered_flag, "
        "COUNT(LINE_AMOUNT_EXCL_TAX) AS amount FROM THBI.DWD_PURCHASE_REQUISITION_LINE",
    ),
    Probe(
        "复活墓碑类 · 请购行的引号单号能否接上订单行（A3 周期起点的第二条路径）",
        "SELECT COUNT(*) AS order_lines, "
        "SUM(CASE WHEN R.QUOTATION_NO IS NOT NULL THEN 1 ELSE 0 END) AS matched_by_quotation "
        "FROM THBI.DWD_PURCHASE_ORDER_LINE O LEFT JOIN "
        "(SELECT DISTINCT QUOTATION_NO FROM THBI.DWD_PURCHASE_REQUISITION_LINE WHERE QUOTATION_NO IS NOT NULL) R "
        "ON R.QUOTATION_NO = O.QUOTATION_NO WHERE O.ORDER_DATE >= DATE '2025-01-01'",
    ),
    Probe(
        "复活墓碑类 · 请购行属性列是否都存在（DWD_PURCHASE_REQUISITION_LINE，23 属性）",
        "SELECT COUNT(*) AS missing FROM ("
        "SELECT 'APPROVED_FLAG' c FROM DUAL UNION ALL SELECT 'BUYER_CODE' FROM DUAL UNION ALL "
        "SELECT 'CLOSED_FLAG' FROM DUAL UNION ALL SELECT 'COMPANY_CODE' FROM DUAL UNION ALL "
        "SELECT 'CREATION_DATE' FROM DUAL UNION ALL SELECT 'CURRENCY_CODE' FROM DUAL UNION ALL "
        "SELECT 'ETL_LOAD_TS' FROM DUAL UNION ALL SELECT 'EXPECTED_ORDER_DATE' FROM DUAL UNION ALL "
        "SELECT 'EXPECTED_RECEIPT_DATE' FROM DUAL UNION ALL SELECT 'FACILITY_CODE' FROM DUAL UNION ALL "
        "SELECT 'LINE_AMOUNT_EXCL_TAX' FROM DUAL UNION ALL SELECT 'MATERIAL_CODE' FROM DUAL UNION ALL "
        "SELECT 'MATERIAL_DESC' FROM DUAL UNION ALL SELECT 'ORDERED_FLAG' FROM DUAL UNION ALL "
        "SELECT 'ORDERED_QTY' FROM DUAL UNION ALL SELECT 'QUANTITY' FROM DUAL UNION ALL "
        "SELECT 'QUOTATION_NO' FROM DUAL UNION ALL SELECT 'REQUISITION_LINE_NO' FROM DUAL UNION ALL "
        "SELECT 'REQUISITION_NO' FROM DUAL UNION ALL SELECT 'SUGGESTED_SUPPLIER_CODE' FROM DUAL UNION ALL "
        "SELECT 'UNIT_PRICE_GROSS' FROM DUAL UNION ALL SELECT 'UNIT_PRICE_NET' FROM DUAL UNION ALL "
        "SELECT 'UPDATE_DATE' FROM DUAL) P WHERE P.c NOT IN "
        "(SELECT column_name FROM ALL_TAB_COLUMNS WHERE owner='THBI' AND table_name='DWD_PURCHASE_REQUISITION_LINE')",
    ),
    Probe(
        "复活墓碑类 · 关联表属性列是否都存在（DWD_REQUISITION_ORDER_LINK，6 属性）",
        "SELECT COUNT(*) AS missing FROM ("
        "SELECT 'ETL_LOAD_TS' c FROM DUAL UNION ALL SELECT 'PO_LINE_NO' FROM DUAL UNION ALL "
        "SELECT 'PO_NO' FROM DUAL UNION ALL SELECT 'QUANTITY' FROM DUAL UNION ALL "
        "SELECT 'REQUISITION_LINE_NO' FROM DUAL UNION ALL SELECT 'REQUISITION_NO' FROM DUAL) P WHERE P.c NOT IN "
        "(SELECT column_name FROM ALL_TAB_COLUMNS WHERE owner='THBI' AND table_name='DWD_REQUISITION_ORDER_LINK')",
    ),
    Probe(
        "复活墓碑类 · DWD_PURCHASE_INVOICE 头表行数",
        "SELECT COUNT(*) AS n FROM THBI.DWD_PURCHASE_INVOICE",
    ),
    Probe(
        "复活墓碑类 · 关联表键列填充率（PO_NO / PO_LINE_NO / REQUISITION_NO / REQUISITION_LINE_NO）",
        "SELECT COUNT(*) AS n, COUNT(PO_NO) AS po_no, COUNT(PO_LINE_NO) AS po_line, "
        "COUNT(REQUISITION_NO) AS req_no, COUNT(REQUISITION_LINE_NO) AS req_line, COUNT(QUANTITY) AS qty "
        "FROM THBI.DWD_REQUISITION_ORDER_LINK",
    ),
    Probe(
        "复活墓碑类 · 订单行能否经关联表接上请购单（A3 周期起点精确匹配覆盖率，按订单行去重）",
        "SELECT COUNT(DISTINCT O.PO_NO || '#' || O.PO_LINE_NO) AS order_lines, "
        "COUNT(DISTINCT CASE WHEN L.REQUISITION_NO IS NOT NULL THEN O.PO_NO || '#' || O.PO_LINE_NO END) "
        "AS matched_by_po, COUNT(DISTINCT O.PO_NO) AS pos, "
        "COUNT(DISTINCT L.PO_NO) AS pos_with_requisition "
        "FROM THBI.DWD_PURCHASE_ORDER_LINE O LEFT JOIN "
        "(SELECT PO_NO, MIN(REQUISITION_NO) AS REQUISITION_NO FROM THBI.DWD_REQUISITION_ORDER_LINK "
        "WHERE REQUISITION_NO IS NOT NULL GROUP BY PO_NO) L ON L.PO_NO = O.PO_NO "
        "WHERE O.ORDER_DATE >= DATE '2025-01-01'",
    ),
    Probe(
        "复活墓碑类 · 关联表覆盖的时间范围（按请购行创建日回看）",
        "SELECT COUNT(*) AS link_rows, COUNT(R.REQUISITION_NO) AS joined_requisition, "
        "MIN(R.CREATION_DATE) AS min_req_creation, MAX(R.CREATION_DATE) AS max_req_creation "
        "FROM THBI.DWD_REQUISITION_ORDER_LINK L LEFT JOIN THBI.DWD_PURCHASE_REQUISITION_LINE R "
        "ON R.REQUISITION_NO = L.REQUISITION_NO",
    ),
)

# ---------------------------------------------------------------- SRM MySQL

SRM_PROBES: tuple[Probe, ...] = (
    Probe("行数 · 供应商准入", "SELECT COUNT(*) AS n FROM supply_access"),
    Probe("行数 · 资质", "SELECT COUNT(*) AS n FROM supply_access_qualification"),
    Probe("行数 · 银行信息", "SELECT COUNT(*) AS n FROM supply_access_bank"),
    Probe("行数 · 品类准入", "SELECT COUNT(*) AS n FROM supply_access_class"),
    Probe("行数 · 供应商主数据", "SELECT COUNT(*) AS n FROM main_data_supplier"),
    Probe("行数 · 来料检验", "SELECT COUNT(*) AS n FROM supply_inspection"),
    Probe("行数 · 检验项", "SELECT COUNT(*) AS n FROM supply_inspection_factors"),
    Probe("行数 · 采购订单行", "SELECT COUNT(*) AS n FROM order_purchase_order_info"),
    Probe("行数 · 报价明细", "SELECT COUNT(*) AS n FROM pricing_quote_form_info"),
    Probe("行数 · 入库单行", "SELECT COUNT(*) AS n FROM order_warehousing_order_info"),
    Probe("行数 · 合同", "SELECT COUNT(*) AS n FROM order_contract"),
    Probe("行数 · 绩效评估", "SELECT COUNT(*) AS n FROM performance_assessment"),
    Probe("行数 · 退货单", "SELECT COUNT(*) AS n FROM supply_return_order"),
    Probe(
        "值域 · 准入状态 access_state（A12「黑名单」判定字段）",
        "SELECT access_state, COUNT(*) AS n FROM supply_access GROUP BY access_state ORDER BY n DESC",
    ),
    Probe(
        "值域 · 供货状态 supply_state",
        "SELECT supply_state, COUNT(*) AS n FROM supply_access GROUP BY supply_state ORDER BY n DESC",
    ),
    Probe(
        "值域 · 供应商状态 state（A12.5「未停用」判定字段）",
        "SELECT state, COUNT(*) AS n FROM main_data_supplier GROUP BY state ORDER BY n DESC",
    ),
    Probe(
        "过期资质（effective_end_date < 今天）",
        "SELECT COUNT(*) AS n FROM supply_access_qualification WHERE effective_end_date < CURDATE()",
    ),
    Probe(
        "30 天内到期资质（A12.4）",
        "SELECT COUNT(*) AS n FROM supply_access_qualification "
        "WHERE effective_end_date >= CURDATE() AND effective_end_date < DATE_ADD(CURDATE(), INTERVAL 30 DAY)",
    ),
    Probe(
        "银行信息缺失（bank_name 为空）",
        "SELECT COUNT(*) AS n FROM supply_access_bank WHERE bank_name IS NULL OR bank_name = ''",
    ),
    Probe(
        "采购订单行含承诺交货日期（A2.1「承诺交货日期为空」核验）",
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN required_delivery_date IS NOT NULL THEN 1 ELSE 0 END) AS with_due "
        "FROM order_purchase_order_info",
    ),
    Probe(
        "报价明细含单价（A5.1「历史均价」备选源核验）",
        "SELECT COUNT(*) AS total, SUM(CASE WHEN unit_price IS NOT NULL THEN 1 ELSE 0 END) AS with_price "
        "FROM pricing_quote_form_info",
    ),
    Probe(
        "入库单行含来料数量（A9.2「来料数量」源核验）",
        "SELECT COUNT(*) AS total, SUM(CASE WHEN plan_qty IS NOT NULL THEN 1 ELSE 0 END) AS with_qty "
        "FROM order_warehousing_order_info",
    ),
)

PROBE_SETS: dict[int, tuple[Probe, ...]] = {1: THBI_PROBES, 10: SRM_PROBES}


async def runDatasource(session: AsyncSession, datasourceId: int) -> list[str]:
    """对单个数据源跑完探针，返回可打印的行（失败也保留，便于取证）。"""
    ds = (await session.execute(select(DataSource).where(DataSource.id == datasourceId))).scalar_one_or_none()
    if ds is None:
        return [f"=== datasource {datasourceId} 不存在 ==="]
    lines = [f"=== datasource {datasourceId} = {ds.name} / {ds.type} / db={ds.database_name} ==="]
    adapter = get_adapter(datasourceId, ds)
    try:
        for probe in PROBE_SETS[datasourceId]:
            lines.append("")
            lines.append(f"--- {probe.title} ---")
            lines.append(f"SQL: {probe.sql}")
            try:
                rows = await adapter.execute_read_only(probe.sql, maxRows=30)
            except Exception as exc:  # noqa: BLE001 —— 探针要把失败本身记下来
                lines.append(f"[ERROR] {type(exc).__name__}: {exc}")
                continue
            for row in rows:
                lines.append("  " + " | ".join(f"{k}={v}" for k, v in row.items()))
            if not rows:
                lines.append("  (0 行)")
    finally:
        await adapter.dispose()
    return lines


async def main() -> int:
    parser = argparse.ArgumentParser(description="采购源侧能力实测探针（只读）")
    parser.add_argument("--datasource-id", type=int, action="append", help="只跑指定数据源（可重复）")
    parser.add_argument("--out", help="输出文件路径（默认写 stdout）")
    args = parser.parse_args()

    targets = args.datasource_id or sorted(PROBE_SETS)
    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl, pool_pre_ping=False)
    lines: list[str] = []
    try:
        async with AsyncSession(engine) as session:
            for datasourceId in targets:
                lines.extend(await runDatasource(session, datasourceId))
    finally:
        await engine.dispose()

    text = "\n".join(lines)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"written: {args.out}（{len(lines)} 行）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
