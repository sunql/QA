-- ============================================================
-- 04_dim.sql - DIM 层（5 张维度）
-- DIM_DATE 用 CONNECT BY 生成（2012-2027，覆盖数据实际范围）
-- 业务维度从 DWD 全量快照重建（SCD1，见 SSOT §9）
-- ============================================================

-- ---------- 日期维度 ----------
CREATE TABLE THBI.DIM_DATE (
  date_key       NUMBER        NOT NULL,
  calendar_date  DATE          NOT NULL,
  year_no        NUMBER,
  quarter_no     NUMBER,
  month_no       NUMBER,
  year_month     VARCHAR2(7),
  day_of_month   NUMBER,
  day_of_week    NUMBER,
  week_of_year   NUMBER,
  is_month_end   NUMBER,
  etl_load_ts    TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DIM_DATE PRIMARY KEY (date_key)
);

INSERT INTO THBI.DIM_DATE
  (date_key, calendar_date, year_no, quarter_no, month_no, year_month,
   day_of_month, day_of_week, week_of_year, is_month_end)
SELECT
  TO_NUMBER(TO_CHAR(d, 'YYYYMMDD')),
  d,
  EXTRACT(YEAR FROM d),
  TO_NUMBER(TO_CHAR(d, 'Q')),
  EXTRACT(MONTH FROM d),
  TO_CHAR(d, 'YYYY-MM'),
  EXTRACT(DAY FROM d),
  TO_CHAR(d, 'D') - 1,
  TO_NUMBER(TO_CHAR(d, 'IW')),
  CASE WHEN d = LAST_DAY(d) THEN 1 ELSE 0 END
FROM (
  SELECT DATE '2012-01-01' + (LEVEL - 1) AS d
  FROM dual
  CONNECT BY LEVEL <= (DATE '2027-12-31' - DATE '2012-01-01' + 1)
);

-- ---------- 供应商维度 ----------
CREATE TABLE THBI.DIM_SUPPLIER (
  supplier_key    NUMBER        NOT NULL,
  supplier_code   VARCHAR2(20)  NOT NULL,
  supplier_name   VARCHAR2(90),
  payment_code    VARCHAR2(20),
  currency_code   VARCHAR2(3),
  zero_stock_flag NUMBER,
  creation_date   DATE,
  update_date     DATE,
  etl_load_ts     TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DIM_SUPPLIER PRIMARY KEY (supplier_key),
  CONSTRAINT UQ_DIM_SUPPLIER_CODE UNIQUE (supplier_code)
);

CREATE SEQUENCE THBI.SQ_DIM_SUPPLIER START WITH 1 INCREMENT BY 1 CACHE 100;

INSERT INTO THBI.DIM_SUPPLIER
  (supplier_key, supplier_code, supplier_name, payment_code,
   currency_code, zero_stock_flag, creation_date, update_date)
SELECT
  THBI.SQ_DIM_SUPPLIER.NEXTVAL,
  supplier_code,
  supplier_name,
  payment_code,
  currency_code,
  zero_stock_flag,
  creation_date,
  update_date
FROM THBI.DWD_SUPPLIER;

-- ---------- 物料维度 ----------
CREATE TABLE THBI.DIM_MATERIAL (
  material_key      NUMBER        NOT NULL,
  material_code     VARCHAR2(20)  NOT NULL,
  description_1     VARCHAR2(130),
  material_status   NUMBER,
  item_category     VARCHAR2(20),
  material_category VARCHAR2(20),
  etl_load_ts       TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DIM_MATERIAL PRIMARY KEY (material_key),
  CONSTRAINT UQ_DIM_MATERIAL_CODE UNIQUE (material_code)
);

CREATE SEQUENCE THBI.SQ_DIM_MATERIAL START WITH 1 INCREMENT BY 1 CACHE 1000;

INSERT INTO THBI.DIM_MATERIAL
  (material_key, material_code, description_1, material_status, item_category, material_category)
SELECT
  THBI.SQ_DIM_MATERIAL.NEXTVAL,
  material_code,
  description_1,
  material_status,
  item_category,
  material_category
FROM THBI.DWD_MATERIAL;

-- ---------- 工厂维度 ----------
CREATE TABLE THBI.DIM_FACILITY (
  facility_key       NUMBER        NOT NULL,
  facility_code      VARCHAR2(10)  NOT NULL,
  facility_name      VARCHAR2(80),
  facility_short_name VARCHAR2(40),
  etl_load_ts        TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DIM_FACILITY PRIMARY KEY (facility_key),
  CONSTRAINT UQ_DIM_FACILITY_CODE UNIQUE (facility_code)
);

CREATE SEQUENCE THBI.SQ_DIM_FACILITY START WITH 1 INCREMENT BY 1 CACHE 10;

INSERT INTO THBI.DIM_FACILITY
  (facility_key, facility_code, facility_name, facility_short_name)
SELECT
  THBI.SQ_DIM_FACILITY.NEXTVAL,
  facility_code,
  facility_name,
  facility_short_name
FROM THBI.DWD_FACILITY;

-- ---------- 货币维度（从事实表 distinct 货币码派生） ----------
CREATE TABLE THBI.DIM_CURRENCY (
  currency_code   VARCHAR2(3)   NOT NULL,
  etl_load_ts     TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DIM_CURRENCY PRIMARY KEY (currency_code)
);

INSERT INTO THBI.DIM_CURRENCY (currency_code)
SELECT DISTINCT currency_code FROM THBI.DWD_PURCHASE_ORDER_LINE
  WHERE currency_code IS NOT NULL
UNION
SELECT DISTINCT currency_code FROM THBI.DWD_GOODS_RECEIPT_LINE
  WHERE currency_code IS NOT NULL
UNION
SELECT DISTINCT currency_code FROM THBI.DWD_PURCHASE_INVOICE
  WHERE currency_code IS NOT NULL
UNION
SELECT DISTINCT currency_code FROM THBI.DWD_SUPPLIER_PAYMENT
  WHERE currency_code IS NOT NULL;

-- ---------- DWD 事实表补充索引 ----------
-- 说明：复合主键已含 (po_no, po_line_no) 前缀的表跳过重复索引
CREATE INDEX THBI.IX_DWD_POL_SUPPLIER ON THBI.DWD_PURCHASE_ORDER_LINE (supplier_code, order_date);
CREATE INDEX THBI.IX_DWD_POL_MATERIAL ON THBI.DWD_PURCHASE_ORDER_LINE (material_code);
CREATE INDEX THBI.IX_DWD_GRL_PO ON THBI.DWD_GOODS_RECEIPT_LINE (po_no, po_line_no);
CREATE INDEX THBI.IX_DWD_GRL_SUPPLIER ON THBI.DWD_GOODS_RECEIPT_LINE (supplier_code, receipt_date);
CREATE INDEX THBI.IX_DWD_GRL_MATERIAL ON THBI.DWD_GOODS_RECEIPT_LINE (material_code);
CREATE INDEX THBI.IX_DWD_GRL_RECDATE ON THBI.DWD_GOODS_RECEIPT_LINE (receipt_date);
CREATE INDEX THBI.IX_DWD_INV_SUPPLIER ON THBI.DWD_PURCHASE_INVOICE (supplier_code, accounting_date);
CREATE INDEX THBI.IX_DWD_INVL_PO ON THBI.DWD_PURCHASE_INVOICE_LINE (po_no, po_line_no);
CREATE INDEX THBI.IX_DWD_PAY_SUPPLIER ON THBI.DWD_SUPPLIER_PAYMENT (supplier_code, value_date);
