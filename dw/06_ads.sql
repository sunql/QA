-- ============================================================
-- 06_ads.sql - ADS 层（应用视图）
-- ADS_SUPPLIER_360：每供应商一行，采购域第一个 AI 应用样板
-- 口径：近 12 月按 value/receipt/order 日期滚动，ADD_MONTHS(-12)
-- ============================================================

CREATE OR REPLACE VIEW THBI.ADS_SUPPLIER_360 AS
SELECT
  s.supplier_key,
  s.supplier_code,
  s.supplier_name,
  s.zero_stock_flag,
  -- 近 12 月准时交付率（收货行口径，权重 = 各月收货行数）
  ROUND(
    (SELECT SUM(d.on_time_line_count) FROM THBI.DWS_SUPPLIER_DELIVERY_MONTHLY d
     WHERE d.supplier_code = s.supplier_code
       AND d.year_month >= TO_CHAR(ADD_MONTHS(SYSDATE, -12), 'YYYY-MM'))
    / NULLIF(
    (SELECT SUM(d.received_line_count) FROM THBI.DWS_SUPPLIER_DELIVERY_MONTHLY d
     WHERE d.supplier_code = s.supplier_code
       AND d.year_month >= TO_CHAR(ADD_MONTHS(SYSDATE, -12), 'YYYY-MM')), 0), 4)
    AS otd_rate_12m,
  -- 近 12 月平均逾期天数
  (SELECT ROUND(AVG(d.avg_delay_days), 1)
     FROM THBI.DWS_SUPPLIER_DELIVERY_MONTHLY d
     WHERE d.supplier_code = s.supplier_code
       AND d.year_month >= TO_CHAR(ADD_MONTHS(SYSDATE, -12), 'YYYY-MM')
       AND d.avg_delay_days IS NOT NULL)
    AS avg_delay_days_12m,
  -- 近 12 月拒收率（数量口径，QTYPUU）
  ROUND(
    (SELECT SUM(q.rejected_qty) FROM THBI.DWS_SUPPLIER_QUALITY_MONTHLY q
     WHERE q.supplier_code = s.supplier_code
       AND q.year_month >= TO_CHAR(ADD_MONTHS(SYSDATE, -12), 'YYYY-MM'))
    / NULLIF(
    (SELECT SUM(q.received_qty) FROM THBI.DWS_SUPPLIER_QUALITY_MONTHLY q
     WHERE q.supplier_code = s.supplier_code
       AND q.year_month >= TO_CHAR(ADD_MONTHS(SYSDATE, -12), 'YYYY-MM')), 0), 4)
    AS reject_rate_12m,
  -- 近 12 月订单金额（不含税）
  (SELECT NVL(SUM(p.order_amount_excl_tax), 0)
     FROM THBI.DWS_SUPPLIER_PURCHASE_MONTHLY p
     WHERE p.supplier_code = s.supplier_code
       AND p.year_month >= TO_CHAR(ADD_MONTHS(SYSDATE, -12), 'YYYY-MM'))
    AS order_amount_12m,
  -- 累计口径
  (SELECT COUNT(DISTINCT p.po_no) FROM THBI.DWD_PURCHASE_ORDER_LINE p
     WHERE p.supplier_code = s.supplier_code)
    AS total_po_count,
  (SELECT COUNT(DISTINCT p.material_code) FROM THBI.DWD_PURCHASE_ORDER_LINE p
     WHERE p.supplier_code = s.supplier_code)
    AS total_material_count,
  (SELECT MIN(p.order_date) FROM THBI.DWD_PURCHASE_ORDER_LINE p
     WHERE p.supplier_code = s.supplier_code AND p.order_date IS NOT NULL)
    AS first_order_date,
  (SELECT MAX(p.order_date) FROM THBI.DWD_PURCHASE_ORDER_LINE p
     WHERE p.supplier_code = s.supplier_code AND p.order_date IS NOT NULL)
    AS last_order_date,
  -- 近 12 月付款金额（原币，多币种混算需按币种过滤，见 SSOT §9）
  (SELECT NVL(SUM(pay.payment_amount), 0)
     FROM THBI.DWS_SUPPLIER_PAYMENT_MONTHLY pay
     WHERE pay.supplier_code = s.supplier_code
       AND pay.year_month >= TO_CHAR(ADD_MONTHS(SYSDATE, -12), 'YYYY-MM')
       AND pay.currency_code = s.currency_code)
    AS payment_amount_12m,
  s.currency_code
FROM THBI.DIM_SUPPLIER s;

-- 供应商活跃交易明细视图（NL2SQL 友好的宽表入口）
CREATE OR REPLACE VIEW THBI.ADS_SUPPLIER_ORDER_DETAIL AS
SELECT
  g.receipt_date,
  g.po_no,
  g.po_line_no,
  g.supplier_code,
  s.supplier_name,
  s.zero_stock_flag,
  g.material_code,
  m.description_1 AS material_desc,
  m.material_category,
  g.received_qty,
  g.rejected_qty,
  g.net_unit_price,
  g.line_amount_excl_tax,
  p.order_date,
  p.promised_receipt_date,
  CASE
    WHEN p.promised_receipt_date IS NULL THEN 'NO_PROMISED_DATE'
    WHEN g.receipt_date <= p.promised_receipt_date THEN 'ON_TIME'
    ELSE 'LATE'
  END AS delivery_status,
  CASE
    WHEN p.promised_receipt_date IS NULL THEN NULL
    ELSE g.receipt_date - p.promised_receipt_date
  END AS delay_days
FROM THBI.DWD_GOODS_RECEIPT_LINE g
LEFT JOIN THBI.DIM_SUPPLIER s ON s.supplier_code = g.supplier_code
LEFT JOIN THBI.DIM_MATERIAL m ON m.material_code = g.material_code
LEFT JOIN THBI.DWD_PURCHASE_ORDER_LINE p
  ON p.po_no = g.po_no AND p.po_line_no = g.po_line_no
WHERE g.po_no IS NOT NULL;
