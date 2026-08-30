"""dwd_merge 纯函数单测：INSERT SELECT -> MERGE 增量语句生成。

不连库，用与 dw/02_dwd_master.sql + 03_dwd_facts.sql 同构的 SQL 片段断言
生成结果（列别名、水位注入、双源 JOIN、WHERE 合并、reload 回退）。
"""

from __future__ import annotations

import pytest

from scripts.dwd_merge import (
    buildDwdMerge,
    buildOdsMerge,
    buildReload,
    buildWatermarkQuery,
    parseCreatePks,
    parseDwdInserts,
)

# ---------- 与 dw/*.sql 同构的片段 ----------

SQL_SUPPLIER = """
CREATE TABLE THBI.DWD_SUPPLIER (
  supplier_code      VARCHAR2(20)  NOT NULL,
  supplier_name      VARCHAR2(90),
  zero_stock_flag    NUMBER,
  etl_load_ts        TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_SUPPLIER PRIMARY KEY (supplier_code)
);
INSERT INTO THBI.DWD_SUPPLIER
  (supplier_code, supplier_name, zero_stock_flag)
SELECT
  BPSNUM_0,
  NULLIF(BPSNAM_0, ' '),
  YPTHFLGM_0
FROM THBI.ODS_BPSUPPLIER;
"""

SQL_INVOICE_LINE = """
CREATE TABLE THBI.DWD_PURCHASE_INVOICE_LINE (
  invoice_type     NUMBER        NOT NULL,
  invoice_no       VARCHAR2(20)  NOT NULL,
  invoice_line_no  NUMBER        NOT NULL,
  quantity         NUMBER,
  etl_load_ts      TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_PURCHASE_INVOICE_LINE PRIMARY KEY
    (invoice_type, invoice_no, invoice_line_no)
);
INSERT INTO THBI.DWD_PURCHASE_INVOICE_LINE
  (invoice_type, invoice_no, invoice_line_no, quantity)
SELECT
  h.INVTYP_0,
  h.NUM_0,
  d.PIDLIN_0,
  d.QTYPUU_0
FROM THBI.ODS_PINVOICED d
JOIN THBI.ODS_PINVOICE h
  ON h.INVTYP_0 = d.INVTYP_0 AND h.NUM_0 = d.NUM_0
WHERE h.INVTYP_0 IN (1, 2);
"""

SQL_PRICE_HEADER = """
INSERT INTO THBI.DWD_SUPPLIER_PRICE_LIST_HEADER
  (price_list_code, price_list_record, line_count)
SELECT
  PLI_0,
  PLICRD_0,
  LINNBR_0
FROM THBI.ODS_PPRICFICH
WHERE TRIM(PLI_0) IS NOT NULL;
"""

SQL_BOM = """
CREATE TABLE THBI.DWD_BOM (
  material_code   VARCHAR2(20)  NOT NULL,
  bom_description VARCHAR2(40),
  etl_load_ts     TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL
);
INSERT INTO THBI.DWD_BOM
  (material_code, bom_description)
SELECT
  ITMREF_0,
  NULLIF(BOMDES_0, ' ')
FROM THBI.ODS_BOM;
"""


def _norm(sql: str) -> str:
    """去首尾空白与多余空行，便于断言。"""
    return "\n".join(ln.rstrip() for ln in sql.strip().splitlines() if ln.strip())


# ---------- parseDwdInserts ----------

def test_parse_simple_single_source() -> None:
    info = parseDwdInserts(SQL_SUPPLIER)["DWD_SUPPLIER"]
    assert info.target == "DWD_SUPPLIER"
    assert info.cols == ("supplier_code", "supplier_name", "zero_stock_flag")
    assert info.exprs == ("BPSNUM_0", "NULLIF(BPSNAM_0, ' ')", "YPTHFLGM_0")
    assert len(info.sources) == 1
    assert info.sources[0].table == "ODS_BPSUPPLIER"
    assert info.sources[0].alias is None
    assert info.where is None


def test_parse_dual_source_join_with_where() -> None:
    info = parseDwdInserts(SQL_INVOICE_LINE)["DWD_PURCHASE_INVOICE_LINE"]
    assert [s.table for s in info.sources] == ["ODS_PINVOICED", "ODS_PINVOICE"]
    assert [s.alias for s in info.sources] == ["d", "h"]
    assert info.where == "h.INVTYP_0 IN (1, 2)"
    assert "JOIN THBI.ODS_PINVOICE h" in info.from_clause
    assert "h.INVTYP_0 = d.INVTYP_0 AND h.NUM_0 = d.NUM_0" in info.from_clause


def test_parse_single_source_with_where() -> None:
    info = parseDwdInserts(SQL_PRICE_HEADER)["DWD_SUPPLIER_PRICE_LIST_HEADER"]
    assert info.where == "TRIM(PLI_0) IS NOT NULL"
    assert info.sources[0].table == "ODS_PPRICFICH"


# ---------- parseCreatePks ----------

def test_parse_create_pks() -> None:
    pks = parseCreatePks(SQL_SUPPLIER + SQL_BOM + SQL_INVOICE_LINE)
    assert pks["DWD_SUPPLIER"] == ["supplier_code"]
    assert pks["DWD_PURCHASE_INVOICE_LINE"] == [
        "invoice_type", "invoice_no", "invoice_line_no",
    ]
    assert pks["DWD_BOM"] == []  # 无 PRIMARY KEY


# ---------- buildDwdMerge ----------

def test_build_merge_simple() -> None:
    info = parseDwdInserts(SQL_SUPPLIER)["DWD_SUPPLIER"]
    sql = buildDwdMerge(info, ["supplier_code"])
    assert "MERGE INTO THBI.DWD_SUPPLIER t" in sql
    assert "BPSNUM_0 AS supplier_code" in sql
    assert "NULLIF(BPSNAM_0, ' ') AS supplier_name" in sql
    assert "FROM THBI.ODS_BPSUPPLIER" in sql
    assert "WHERE UPDDATTIM_0 >= :wm" in sql
    assert "ON (t.supplier_code = s.supplier_code)" in sql
    assert "WHEN MATCHED THEN UPDATE SET" in sql
    assert "t.supplier_name = s.supplier_name," in sql
    assert "t.zero_stock_flag = s.zero_stock_flag," in sql
    assert "t.etl_load_ts = SYSTIMESTAMP" in sql
    assert "WHEN NOT MATCHED THEN INSERT" in sql
    assert "(supplier_code, supplier_name, zero_stock_flag)" in sql
    assert "VALUES (s.supplier_code, s.supplier_name, s.zero_stock_flag)" in sql
    # 断言无 PK 列出现在 UPDATE SET（Oracle MERGE 禁止更新 ON 列）
    assert "t.supplier_code = s.supplier_code," not in sql


def test_build_merge_dual_source_watermark() -> None:
    info = parseDwdInserts(SQL_INVOICE_LINE)["DWD_PURCHASE_INVOICE_LINE"]
    sql = buildDwdMerge(info, ["invoice_type", "invoice_no", "invoice_line_no"])
    # NULL 侧 COALESCE 到 epoch，避免 GREATEST NULL 传播丢行
    assert ("GREATEST(COALESCE(d.UPDDATTIM_0, TIMESTAMP '1900-01-01 00:00:00'), "
            "COALESCE(h.UPDDATTIM_0, TIMESTAMP '1900-01-01 00:00:00')) >= :wm") in sql
    assert "AND h.INVTYP_0 IN (1, 2)" in sql
    on_clause = ("ON (t.invoice_type = s.invoice_type AND "
                 "t.invoice_no = s.invoice_no AND "
                 "t.invoice_line_no = s.invoice_line_no)")
    assert on_clause in sql
    # 双源别名列的表达式原样保留（清洗/JOIN 语义不变）
    assert "h.INVTYP_0 AS invoice_type" in sql
    assert "d.PIDLIN_0 AS invoice_line_no" in sql


def test_build_merge_pk_not_in_cols_raises() -> None:
    info = parseDwdInserts(SQL_SUPPLIER)["DWD_SUPPLIER"]
    with pytest.raises(ValueError):
        buildDwdMerge(info, ["supplier_code", "nonexistent_pk"])


def test_build_merge_col_expr_mismatch_raises() -> None:
    info = parseDwdInserts(SQL_SUPPLIER)["DWD_SUPPLIER"]
    # 手工构造列数 != 表达式数的 InsertInfo
    from dataclasses import replace
    bad = replace(info, cols=("supplier_code",))  # 只保留 1 列
    with pytest.raises(ValueError):
        buildDwdMerge(bad, ["supplier_code"])


# ---------- buildReload ----------

def test_build_reload_reconstructs_insert_select() -> None:
    info = parseDwdInserts(SQL_BOM)["DWD_BOM"]
    sql = buildReload(info)
    assert sql.startswith("INSERT INTO THBI.DWD_BOM")
    assert "(material_code, bom_description)" in sql
    assert "ITMREF_0" in sql
    assert "NULLIF(BOMDES_0, ' ')" in sql
    assert "FROM THBI.ODS_BOM" in sql


def test_build_reload_keeps_where() -> None:
    info = parseDwdInserts(SQL_PRICE_HEADER)["DWD_SUPPLIER_PRICE_LIST_HEADER"]
    sql = buildReload(info)
    assert "WHERE TRIM(PLI_0) IS NOT NULL" in sql


# ---------- buildOdsMerge ----------

def test_build_ods_merge() -> None:
    sql = buildOdsMerge("PORDER", "ODS_PORDER",
                        ["POHNUM_0", "REVNUM_0", "ETL_LOAD_TS"],
                        ["POHNUM_0"])
    assert "MERGE INTO THBI.ODS_PORDER t" in sql
    assert "FROM ZJTH.PORDER s" in sql
    assert "WHERE s.UPDDATTIM_0 >= :wm" in sql
    assert "SELECT s.*, SYSTIMESTAMP AS ETL_LOAD_TS" in sql
    assert "ON (t.POHNUM_0 = s.POHNUM_0)" in sql
    assert "t.REVNUM_0 = s.REVNUM_0," in sql
    assert "t.ETL_LOAD_TS = SYSTIMESTAMP" in sql
    assert "(POHNUM_0, REVNUM_0, ETL_LOAD_TS)" in sql
    assert "VALUES (s.POHNUM_0, s.REVNUM_0, SYSTIMESTAMP)" in sql


# ---------- buildWatermarkQuery ----------

def test_build_watermark_query_single_source() -> None:
    info = parseDwdInserts(SQL_SUPPLIER)["DWD_SUPPLIER"]
    q = buildWatermarkQuery(info)
    assert q == "SELECT MAX(UPDDATTIM_0) FROM THBI.ODS_BPSUPPLIER"


def test_build_watermark_query_dual_source() -> None:
    info = parseDwdInserts(SQL_INVOICE_LINE)["DWD_PURCHASE_INVOICE_LINE"]
    q = buildWatermarkQuery(info)
    assert q.startswith("SELECT MAX(GREATEST(COALESCE(d.UPDDATTIM_0, TIMESTAMP '1900-01-01 00:00:00'), ")
    assert "COALESCE(h.UPDDATTIM_0, TIMESTAMP '1900-01-01 00:00:00')))" in q
    assert "FROM THBI.ODS_PINVOICED d" in q
    assert "JOIN THBI.ODS_PINVOICE h" in q
    # 水位查询必须保留业务 WHERE，避免不合格行抬高水位导致合格行静默漏数
    assert "WHERE h.INVTYP_0 IN (1, 2)" in q


def test_build_watermark_query_preserves_where() -> None:
    """单源带业务过滤的表，水位查询必须带同谓词 WHERE（HIGH 修复回归）。"""
    info = parseDwdInserts(SQL_PRICE_HEADER)["DWD_SUPPLIER_PRICE_LIST_HEADER"]
    q = buildWatermarkQuery(info)
    assert q == ("SELECT MAX(UPDDATTIM_0) FROM THBI.ODS_PPRICFICH\n"
                 "WHERE TRIM(PLI_0) IS NOT NULL")
