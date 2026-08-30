-- ============================================================
-- 05_dws.sql - DWS 层（5 张月度汇总）
-- 粒度：supplier [+ facility/material] + year_month
-- OTD 口径：收货行关联 PO 行（po_no+po_line_no），
--   准时 = receipt_date <= PO 行 promised_receipt_date，按收货日期归期
-- 质量口径：同表 QTYPUU（价格 UOM）内部一致（拒收率 = 拒收/收货）
-- ============================================================

-- ---------- 采购月度汇总（按订单日期归期） ----------
CREATE TABLE THBI.DWS_SUPPLIER_PURCHASE_MONTHLY (
  supplier_code       VARCHAR2(20)   NOT NULL,
  zero_stock_flag     NUMBER,
  facility_code       VARCHAR2(10),
  year_month          VARCHAR2(7)    NOT NULL,
  order_count         NUMBER,
  order_line_count    NUMBER,
  order_qty           NUMBER,
  order_amount_excl_tax NUMBER,
  material_count      NUMBER,
  etl_load_ts         TIMESTAMP(3)   DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWS_SUPPLIER_PURCHASE_MONTHLY PRIMARY KEY (supplier_code, facility_code, year_month)
);

INSERT INTO THBI.DWS_SUPPLIER_PURCHASE_MONTHLY
  (supplier_code, zero_stock_flag, facility_code, year_month, order_count,
   order_line_count, order_qty, order_amount_excl_tax, material_count)
SELECT
  f.supplier_code,
  s.zero_stock_flag,
  f.facility_code,
  TO_CHAR(f.order_date, 'YYYY-MM'),
  COUNT(DISTINCT f.po_no),
  COUNT(*),
  SUM(f.order_qty),
  SUM(f.line_amount_excl_tax),
  COUNT(DISTINCT f.material_code)
FROM THBI.DWD_PURCHASE_ORDER_LINE f
LEFT JOIN THBI.DWD_SUPPLIER s ON s.supplier_code = f.supplier_code
WHERE f.supplier_code IS NOT NULL AND f.order_date IS NOT NULL
GROUP BY f.supplier_code, s.zero_stock_flag, f.facility_code, TO_CHAR(f.order_date, 'YYYY-MM');

-- ---------- 交付月度汇总（OTD 核心表，按收货日期归期） ----------
CREATE TABLE THBI.DWS_SUPPLIER_DELIVERY_MONTHLY (
  supplier_code       VARCHAR2(20)   NOT NULL,
  zero_stock_flag     NUMBER,
  facility_code       VARCHAR2(10),
  year_month          VARCHAR2(7)    NOT NULL,
  received_line_count NUMBER,
  on_time_line_count  NUMBER,
  late_line_count     NUMBER,
  on_time_rate        NUMBER,
  avg_delay_days      NUMBER,
  received_qty        NUMBER,
  etl_load_ts         TIMESTAMP(3)   DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWS_SUPPLIER_DELIVERY_MONTHLY PRIMARY KEY (supplier_code, facility_code, year_month)
);

INSERT INTO THBI.DWS_SUPPLIER_DELIVERY_MONTHLY
  (supplier_code, zero_stock_flag, facility_code, year_month, received_line_count,
   on_time_line_count, late_line_count, on_time_rate, avg_delay_days,
   received_qty)
SELECT
  g.supplier_code,
  s.zero_stock_flag,
  g.facility_code,
  TO_CHAR(g.receipt_date, 'YYYY-MM'),
  COUNT(*),
  SUM(CASE WHEN g.receipt_date <= p.promised_receipt_date THEN 1 ELSE 0 END),
  SUM(CASE WHEN g.receipt_date > p.promised_receipt_date THEN 1 ELSE 0 END),
  ROUND(
    SUM(CASE WHEN g.receipt_date <= p.promised_receipt_date THEN 1 ELSE 0 END)
    / NULLIF(COUNT(*), 0), 4),
  ROUND(
    AVG(CASE WHEN g.receipt_date > p.promised_receipt_date
        THEN g.receipt_date - p.promised_receipt_date END), 1),
  SUM(g.received_qty)
FROM THBI.DWD_GOODS_RECEIPT_LINE g
JOIN THBI.DWD_PURCHASE_ORDER_LINE p
  ON p.po_no = g.po_no AND p.po_line_no = g.po_line_no
LEFT JOIN THBI.DWD_SUPPLIER s ON s.supplier_code = g.supplier_code
WHERE g.supplier_code IS NOT NULL
  AND g.receipt_date IS NOT NULL
  AND g.po_no IS NOT NULL
  AND p.promised_receipt_date IS NOT NULL
GROUP BY g.supplier_code, s.zero_stock_flag, g.facility_code, TO_CHAR(g.receipt_date, 'YYYY-MM');

-- ---------- 质量月度汇总（拒收率，按收货日期归期） ----------
CREATE TABLE THBI.DWS_SUPPLIER_QUALITY_MONTHLY (
  supplier_code       VARCHAR2(20)   NOT NULL,
  zero_stock_flag     NUMBER,
  facility_code       VARCHAR2(10),
  material_code       VARCHAR2(20),
  year_month          VARCHAR2(7)    NOT NULL,
  received_qty        NUMBER,
  rejected_qty        NUMBER,
  reject_rate         NUMBER,
  etl_load_ts         TIMESTAMP(3)   DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWS_SUPPLIER_QUALITY_MONTHLY PRIMARY KEY
    (supplier_code, facility_code, material_code, year_month)
);

INSERT INTO THBI.DWS_SUPPLIER_QUALITY_MONTHLY
  (supplier_code, zero_stock_flag, facility_code, material_code, year_month,
   received_qty, rejected_qty, reject_rate)
SELECT
  f.supplier_code,
  s.zero_stock_flag,
  f.facility_code,
  f.material_code,
  TO_CHAR(f.receipt_date, 'YYYY-MM'),
  SUM(f.received_qty_price_uom),
  SUM(f.rejected_qty),
  ROUND(SUM(f.rejected_qty) / NULLIF(SUM(f.received_qty_price_uom), 0), 4)
FROM THBI.DWD_GOODS_RECEIPT_LINE f
LEFT JOIN THBI.DWD_SUPPLIER s ON s.supplier_code = f.supplier_code
WHERE f.supplier_code IS NOT NULL AND f.receipt_date IS NOT NULL
GROUP BY f.supplier_code, s.zero_stock_flag, f.facility_code, f.material_code, TO_CHAR(f.receipt_date, 'YYYY-MM');

-- ---------- 付款月度汇总（按 value_date 归期） ----------
CREATE TABLE THBI.DWS_SUPPLIER_PAYMENT_MONTHLY (
  supplier_code       VARCHAR2(20)   NOT NULL,
  zero_stock_flag     NUMBER,
  facility_code       VARCHAR2(10),
  currency_code       VARCHAR2(3),
  year_month          VARCHAR2(7)    NOT NULL,
  payment_count       NUMBER,
  payment_amount      NUMBER,
  etl_load_ts         TIMESTAMP(3)   DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWS_SUPPLIER_PAYMENT_MONTHLY PRIMARY KEY
    (supplier_code, facility_code, currency_code, year_month)
);

INSERT INTO THBI.DWS_SUPPLIER_PAYMENT_MONTHLY
  (supplier_code, zero_stock_flag, facility_code, currency_code, year_month,
   payment_count, payment_amount)
SELECT
  f.supplier_code,
  s.zero_stock_flag,
  f.facility_code,
  f.currency_code,
  TO_CHAR(f.value_date, 'YYYY-MM'),
  COUNT(*),
  SUM(f.payment_amount)
FROM THBI.DWD_SUPPLIER_PAYMENT f
LEFT JOIN THBI.DWD_SUPPLIER s ON s.supplier_code = f.supplier_code
WHERE f.supplier_code IS NOT NULL AND f.value_date IS NOT NULL
GROUP BY f.supplier_code, s.zero_stock_flag, f.facility_code, f.currency_code, TO_CHAR(f.value_date, 'YYYY-MM');

-- ---------- 物料价格月度趋势（按收货日期归期） ----------
CREATE TABLE THBI.DWS_MATERIAL_PRICE_MONTHLY (
  material_code       VARCHAR2(20)   NOT NULL,
  material_category   VARCHAR2(20),
  supplier_code       VARCHAR2(20),
  year_month          VARCHAR2(7)    NOT NULL,
  price_line_count    NUMBER,
  avg_net_unit_price  NUMBER,
  min_net_unit_price  NUMBER,
  max_net_unit_price  NUMBER,
  etl_load_ts         TIMESTAMP(3)   DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWS_MATERIAL_PRICE_MONTHLY PRIMARY KEY
    (material_code, supplier_code, year_month)
);

INSERT INTO THBI.DWS_MATERIAL_PRICE_MONTHLY
  (material_code, material_category, supplier_code, year_month, price_line_count,
   avg_net_unit_price, min_net_unit_price, max_net_unit_price)
SELECT
  f.material_code,
  m.material_category,
  f.supplier_code,
  TO_CHAR(f.receipt_date, 'YYYY-MM'),
  COUNT(*),
  ROUND(AVG(f.net_unit_price), 6),
  MIN(f.net_unit_price),
  MAX(f.net_unit_price)
FROM THBI.DWD_GOODS_RECEIPT_LINE f
LEFT JOIN THBI.DWD_MATERIAL m ON m.material_code = f.material_code
WHERE f.material_code IS NOT NULL
  AND f.receipt_date IS NOT NULL
  AND f.net_unit_price > 0
GROUP BY f.material_code, m.material_category, f.supplier_code, TO_CHAR(f.receipt_date, 'YYYY-MM');
