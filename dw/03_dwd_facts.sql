-- ============================================================
-- 03_dwd_facts.sql - DWD 层（采购事实，13 表）
-- 采购链核心：PR -> PO -> ASN -> GR -> INV -> PAY
-- 关键口径：
--   * promised_receipt_date = PORDERQ.EXTRCPDAT_0（预期收货日）
--   * receipt_date = PRECEIPTD.RCPDAT_0
--   * GR 行通过 po_no + po_line_no 关联 PO 行（OTD 计算基础）
--   * 收货行空 PO 引用（' '）-> NULL（非 PO 收货，如调拨）
-- ============================================================

-- ---------- 采购订单（头） ----------
CREATE TABLE THBI.DWD_PURCHASE_ORDER (
  po_no                 VARCHAR2(20)  NOT NULL,
  revision_no           NUMBER,
  company_code          VARCHAR2(10),
  facility_code         VARCHAR2(10),
  supplier_code         VARCHAR2(20),
  supplier_name         VARCHAR2(90),
  order_date            DATE,
  order_reference       VARCHAR2(20),
  currency_code         VARCHAR2(3),
  line_count            NUMBER,
  total_qty             NUMBER,
  total_amount_excl_tax NUMBER,
  total_amount_incl_tax NUMBER,
  total_tax_amount      NUMBER,
  validation_status     NUMBER,
  creation_date         DATE,
  update_date           DATE,
  etl_load_ts           TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_PURCHASE_ORDER PRIMARY KEY (po_no)
);

INSERT INTO THBI.DWD_PURCHASE_ORDER
  (po_no, revision_no, company_code, facility_code, supplier_code,
   supplier_name, order_date, order_reference, currency_code, line_count,
   total_qty, total_amount_excl_tax, total_amount_incl_tax, total_tax_amount,
   validation_status, creation_date, update_date)
SELECT
  POHNUM_0,
  REVNUM_0,
  NULLIF(CPY_0, ' '),
  NULLIF(POHFCY_0, ' '),
  NULLIF(BPSNUM_0, ' '),
  NULLIF(BPRNAM_0, ' '),
  CASE WHEN ORDDAT_0 > DATE '1900-01-01' THEN ORDDAT_0 END,
  NULLIF(ORDREF_0, ' '),
  NULLIF(CUR_0, ' '),
  LINNBR_0,
  TOTLINQTY_0,
  TOTLINAMT_0,
  TOTLINATI_0,
  TOTTAXAMT_0,
  VALSTA_0,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_PORDER;

-- ---------- 采购订单行（核心事实：OTD 承诺日期所在表） ----------
CREATE TABLE THBI.DWD_PURCHASE_ORDER_LINE (
  po_no                 VARCHAR2(20)  NOT NULL,
  po_line_no            NUMBER        NOT NULL,
  company_code          VARCHAR2(10),
  facility_code         VARCHAR2(10),
  supplier_code         VARCHAR2(20),
  material_code         VARCHAR2(20),
  supplier_material_code VARCHAR2(40),
  order_date            DATE,
  requested_receipt_date DATE,
  promised_receipt_date DATE,
  order_qty             NUMBER,
  received_qty          NUMBER,
  invoiced_qty          NUMBER,
  returned_qty          NUMBER,
  unit_price            NUMBER,
  line_amount_excl_tax  NUMBER,
  line_amount_incl_tax  NUMBER,
  currency_code         VARCHAR2(3),
  line_status           NUMBER,
  line_type             NUMBER,
  quotation_no          VARCHAR2(20),
  quotation_line_no     NUMBER,
  receipt_count         NUMBER,
  invoice_count         NUMBER,
  last_receipt_date     DATE,
  last_invoice_date     DATE,
  creation_date         DATE,
  update_date           DATE,
  etl_load_ts           TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_PURCHASE_ORDER_LINE PRIMARY KEY (po_no, po_line_no)
);

INSERT INTO THBI.DWD_PURCHASE_ORDER_LINE
  (po_no, po_line_no, company_code, facility_code, supplier_code,
   material_code, supplier_material_code, order_date,
   requested_receipt_date, promised_receipt_date, order_qty, received_qty,
   invoiced_qty, returned_qty, unit_price, line_amount_excl_tax,
   line_amount_incl_tax, currency_code, line_status, line_type,
   quotation_no, quotation_line_no, receipt_count, invoice_count,
   last_receipt_date, last_invoice_date, creation_date, update_date)
SELECT
  POHNUM_0,
  POPLIN_0,
  NULLIF(CPY_0, ' '),
  NULLIF(POHFCY_0, ' '),
  NULLIF(BPSNUM_0, ' '),
  NULLIF(ITMREF_0, ' '),
  NULLIF(ITMREFBPS_0, ' '),
  CASE WHEN ORDDAT_0 > DATE '1900-01-01' THEN ORDDAT_0 END,
  CASE WHEN DEMRCPDAT_0 > DATE '1900-01-01' THEN DEMRCPDAT_0 END,
  CASE WHEN EXTRCPDAT_0 > DATE '1900-01-01' THEN EXTRCPDAT_0 END,
  QTYUOM_0,
  RCPQTYPUU_0,
  INVQTYPUU_0,
  RETQTYPUU_0,
  CPRPRI_0,
  LINAMT_0,
  LINATIAMT_0,
  NULLIF(NETCUR_0, ' '),
  LINSTA_0,
  LINTYP_0,
  NULLIF(PQHNUM_0, ' '),
  PPDLIN_0,
  LINRCPNBR_0,
  LININVNBR_0,
  CASE WHEN LASRCPDAT_0 > DATE '1900-01-01' THEN LASRCPDAT_0 END,
  CASE WHEN LASINVDAT_0 > DATE '1900-01-01' THEN LASINVDAT_0 END,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_PORDERQ;

-- ---------- 采购申请行 ----------
CREATE TABLE THBI.DWD_PURCHASE_REQUISITION_LINE (
  requisition_no        VARCHAR2(20)  NOT NULL,
  requisition_line_no   NUMBER        NOT NULL,
  company_code          VARCHAR2(10),
  facility_code         VARCHAR2(10),
  material_code         VARCHAR2(20),
  material_desc         VARCHAR2(130),
  suggested_supplier_code VARCHAR2(20),
  quantity              NUMBER,
  ordered_qty           NUMBER,
  unit_price_gross      NUMBER,
  unit_price_net        NUMBER,
  line_amount_excl_tax  NUMBER,
  currency_code         VARCHAR2(3),
  expected_order_date   DATE,
  expected_receipt_date DATE,
  buyer_code            VARCHAR2(20),
  quotation_no          VARCHAR2(20),
  ordered_flag          NUMBER,
  closed_flag           NUMBER,
  approved_flag         NUMBER,
  creation_date         DATE,
  update_date           DATE,
  etl_load_ts           TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_PURCHASE_REQUISITION_LINE PRIMARY KEY (requisition_no, requisition_line_no)
);

INSERT INTO THBI.DWD_PURCHASE_REQUISITION_LINE
  (requisition_no, requisition_line_no, company_code, facility_code,
   material_code, material_desc, suggested_supplier_code, quantity,
   ordered_qty, unit_price_gross, unit_price_net, line_amount_excl_tax,
   currency_code, expected_order_date, expected_receipt_date, buyer_code,
   quotation_no, ordered_flag, closed_flag, approved_flag,
   creation_date, update_date)
SELECT
  PSHNUM_0,
  PSDLIN_0,
  NULLIF(CPY_0, ' '),
  NULLIF(PSHFCY_0, ' '),
  NULLIF(ITMREF_0, ' '),
  NULLIF(ITMDES1_0, ' '),
  NULLIF(BPSNUM_0, ' '),
  QTYPUU_0,
  ORDQTYPUU_0,
  GROPRI_0,
  NETPRI_0,
  LINAMT_0,
  NULLIF(CUR_0, ' '),
  CASE WHEN EXTORDDAT_0 > DATE '1900-01-01' THEN EXTORDDAT_0 END,
  CASE WHEN EXTRCPDAT_0 > DATE '1900-01-01' THEN EXTRCPDAT_0 END,
  NULLIF(LINBUY_0, ' '),
  NULLIF(PQHNUM_0, ' '),
  LINORDFLG_0,
  LINCLEFLG_0,
  LINAPPFLG_0,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_PREQUISD;

-- ---------- 申请-订单关联 ----------
CREATE TABLE THBI.DWD_REQUISITION_ORDER_LINK (
  requisition_no       VARCHAR2(20)  NOT NULL,
  requisition_line_no  NUMBER        NOT NULL,
  po_no                VARCHAR2(20)  NOT NULL,
  po_line_no           NUMBER        NOT NULL,
  quantity             NUMBER,
  etl_load_ts          TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_REQUISITION_ORDER_LINK PRIMARY KEY
    (requisition_no, requisition_line_no, po_no, po_line_no)
);

INSERT INTO THBI.DWD_REQUISITION_ORDER_LINK
  (requisition_no, requisition_line_no, po_no, po_line_no, quantity)
SELECT
  PSHNUM_0,
  PSDLIN_0,
  POHNUM_0,
  POPLIN_0,
  QTYPUU_0
FROM THBI.ODS_PREQUISO;

-- ---------- 到货通知（头） ----------
CREATE TABLE THBI.DWD_ARRIVAL_NOTICE (
  arrival_notice_no     VARCHAR2(20)  NOT NULL,
  company_code          VARCHAR2(10),
  facility_code         VARCHAR2(10),
  supplier_code         VARCHAR2(20),
  planned_receipt_date  DATE,
  currency_code         VARCHAR2(3),
  sales_order_no        VARCHAR2(20),
  creation_date         DATE,
  update_date           DATE,
  etl_load_ts           TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_ARRIVAL_NOTICE PRIMARY KEY (arrival_notice_no)
);

INSERT INTO THBI.DWD_ARRIVAL_NOTICE
  (arrival_notice_no, company_code, facility_code, supplier_code,
   planned_receipt_date, currency_code, sales_order_no,
   creation_date, update_date)
SELECT
  YPTHNUM_0,
  NULLIF(CPY_0, ' '),
  NULLIF(PRHFCY_0, ' '),
  NULLIF(BPSNUM_0, ' '),
  CASE WHEN RCPDAT_0 > DATE '1900-01-01' THEN RCPDAT_0 END,
  NULLIF(CUR_0, ' '),
  NULLIF(SOHNUM_0, ' '),
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_YPRECEIPT;

-- ---------- 到货通知行 ----------
CREATE TABLE THBI.DWD_ARRIVAL_NOTICE_LINE (
  arrival_notice_no      VARCHAR2(20)  NOT NULL,
  arrival_notice_line_no NUMBER        NOT NULL,
  company_code           VARCHAR2(10),
  facility_code          VARCHAR2(10),
  po_no                  VARCHAR2(20),
  po_line_no             NUMBER,
  supplier_code          VARCHAR2(20),
  material_code          VARCHAR2(20),
  planned_qty            NUMBER,
  actual_qty             NUMBER,
  rejected_qty           NUMBER,
  planned_receipt_date   DATE,
  actual_receipt_no      VARCHAR2(20),
  actual_receipt_line_no NUMBER,
  return_date            DATE,
  creation_date          DATE,
  update_date            DATE,
  etl_load_ts            TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_ARRIVAL_NOTICE_LINE PRIMARY KEY (arrival_notice_no, arrival_notice_line_no)
);

INSERT INTO THBI.DWD_ARRIVAL_NOTICE_LINE
  (arrival_notice_no, arrival_notice_line_no, company_code, facility_code,
   po_no, po_line_no, supplier_code, material_code, planned_qty,
   actual_qty, rejected_qty, planned_receipt_date, actual_receipt_no,
   actual_receipt_line_no, return_date, creation_date, update_date)
SELECT
  YPTHNUM_0,
  YPTDLIN_0,
  NULLIF(CPY_0, ' '),
  NULLIF(PRHFCY_0, ' '),
  NULLIF(POHNUM_0, ' '),
  CASE WHEN POPLIN_0 = 0 THEN NULL ELSE POPLIN_0 END,
  NULLIF(BPSNUM_0, ' '),
  NULLIF(ITMREF_0, ' '),
  QTYUOM_0,
  AQTYUOM_0,
  RRRQTYPUU_0,
  CASE WHEN RCPDAT_0 > DATE '1900-01-01' THEN RCPDAT_0 END,
  NULLIF(PTHNUM_0, ' '),
  YPTDLIN_0,
  CASE WHEN RTNDAT_0 > DATE '1900-01-01' THEN RTNDAT_0 END,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_YPRECEIPTD;

-- ---------- 收货单（头） ----------
CREATE TABLE THBI.DWD_GOODS_RECEIPT (
  receipt_no             VARCHAR2(20)  NOT NULL,
  company_code           VARCHAR2(10),
  facility_code          VARCHAR2(10),
  supplier_code          VARCHAR2(20),
  receipt_date           DATE,
  arrival_date           DATE,
  currency_code          VARCHAR2(3),
  line_count             NUMBER,
  total_qty              NUMBER,
  total_amount_excl_tax  NUMBER,
  total_amount_incl_tax  NUMBER,
  total_tax_amount       NUMBER,
  posting_date           DATE,
  creation_date          DATE,
  update_date            DATE,
  etl_load_ts            TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_GOODS_RECEIPT PRIMARY KEY (receipt_no)
);

INSERT INTO THBI.DWD_GOODS_RECEIPT
  (receipt_no, company_code, facility_code, supplier_code, receipt_date,
   arrival_date, currency_code, line_count, total_qty,
   total_amount_excl_tax, total_amount_incl_tax, total_tax_amount,
   posting_date, creation_date, update_date)
SELECT
  PTHNUM_0,
  NULLIF(CPY_0, ' '),
  NULLIF(PRHFCY_0, ' '),
  NULLIF(BPSNUM_0, ' '),
  CASE WHEN RCPDAT_0 > DATE '1900-01-01' THEN RCPDAT_0 END,
  CASE WHEN ARVDAT_0 > DATE '1900-01-01' THEN ARVDAT_0 END,
  NULLIF(CUR_0, ' '),
  LINNBR_0,
  TOTLINQTY_0,
  TOTLINAMT_0,
  TOTAMTATI_0,
  TOTTAXAMT_0,
  CASE WHEN PSTDAT_0 > DATE '1900-01-01' THEN PSTDAT_0 END,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_PRECEIPT;

-- ---------- 收货单行（核心事实：OTD 实际收货日期 + 拒收数量所在表） ----------
CREATE TABLE THBI.DWD_GOODS_RECEIPT_LINE (
  receipt_no            VARCHAR2(20)  NOT NULL,
  receipt_line_no       NUMBER        NOT NULL,
  company_code          VARCHAR2(10),
  facility_code         VARCHAR2(10),
  receipt_date          DATE,
  po_no                 VARCHAR2(20),
  po_line_no            NUMBER,
  supplier_code         VARCHAR2(20),
  material_code         VARCHAR2(20),
  material_desc         VARCHAR2(130),
  received_qty          NUMBER,
  received_qty_price_uom NUMBER,
  rejected_qty          NUMBER,
  returned_qty          NUMBER,
  invoiced_qty          NUMBER,
  gross_unit_price      NUMBER,
  net_unit_price        NUMBER,
  line_amount_excl_tax  NUMBER,
  line_amount_incl_tax  NUMBER,
  currency_code         VARCHAR2(3),
  arrival_notice_no     VARCHAR2(20),
  arrival_notice_line_no NUMBER,
  posting_date          DATE,
  creation_date         DATE,
  update_date           DATE,
  etl_load_ts           TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_GOODS_RECEIPT_LINE PRIMARY KEY (receipt_no, receipt_line_no)
);

INSERT INTO THBI.DWD_GOODS_RECEIPT_LINE
  (receipt_no, receipt_line_no, company_code, facility_code, receipt_date,
   po_no, po_line_no, supplier_code, material_code, material_desc,
   received_qty, received_qty_price_uom, rejected_qty, returned_qty,
   invoiced_qty, gross_unit_price, net_unit_price, line_amount_excl_tax,
   line_amount_incl_tax, currency_code, arrival_notice_no,
   arrival_notice_line_no, posting_date, creation_date, update_date)
SELECT
  PTHNUM_0,
  PTDLIN_0,
  NULLIF(CPY_0, ' '),
  NULLIF(PRHFCY_0, ' '),
  CASE WHEN RCPDAT_0 > DATE '1900-01-01' THEN RCPDAT_0 END,
  NULLIF(POHNUM_0, ' '),
  CASE WHEN POPLIN_0 = 0 THEN NULL ELSE POPLIN_0 END,
  NULLIF(BPSNUM_0, ' '),
  NULLIF(ITMREF_0, ' '),
  NULLIF(ITMDES1_0, ' '),
  QTYUOM_0,
  QTYPUU_0,
  RRRQTYPUU_0,
  RTNQTYPUU_0,
  INVQTYPUU_0,
  GROPRI_0,
  NETPRI_0,
  LINAMT_0,
  LINATIAMT_0,
  NULLIF(NETCUR_0, ' '),
  NULLIF(YPTHNUM_0, ' '),
  YPTDLIN_0,
  CASE WHEN LINPSTDAT_0 > DATE '1900-01-01' THEN LINPSTDAT_0 END,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_PRECEIPTD;

-- ---------- 采购发票（头） ----------
CREATE TABLE THBI.DWD_PURCHASE_INVOICE (
  invoice_type          NUMBER        NOT NULL,
  invoice_no            VARCHAR2(20)  NOT NULL,
  company_code          VARCHAR2(10),
  facility_code         VARCHAR2(10),
  supplier_code         VARCHAR2(20),
  supplier_name         VARCHAR2(90),
  accounting_date       DATE,
  invoice_date          DATE,
  invoice_reference     VARCHAR2(70),
  invoice_document_no   VARCHAR2(30),
  currency_code         VARCHAR2(3),
  amount_excl_tax       NUMBER,
  amount_incl_tax       NUMBER,
  status                NUMBER,
  creation_date         DATE,
  update_date           DATE,
  etl_load_ts           TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_PURCHASE_INVOICE PRIMARY KEY (invoice_type, invoice_no)
);

INSERT INTO THBI.DWD_PURCHASE_INVOICE
  (invoice_type, invoice_no, company_code, facility_code, supplier_code,
   supplier_name, accounting_date, invoice_date, invoice_reference,
   invoice_document_no, currency_code, amount_excl_tax, amount_incl_tax,
   status, creation_date, update_date)
SELECT
  INVTYP_0,
  NUM_0,
  NULLIF(CPY_0, ' '),
  NULLIF(FCY_0, ' '),
  NULLIF(BPR_0, ' '),
  NULLIF(BPRNAM_0, ' '),
  CASE WHEN ACCDAT_0 > DATE '1900-01-01' THEN ACCDAT_0 END,
  CASE WHEN BPRDAT_0 > DATE '1900-01-01' THEN BPRDAT_0 END,
  NULLIF(INVREF_0, ' '),
  NULLIF(INVNUM_0, ' '),
  NULLIF(CUR_0, ' '),
  AMTNOT_0,
  AMTATI_0,
  STA_0,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_PINVOICE
WHERE INVTYP_0 IN (1, 2);

-- ---------- 采购发票行 ----------
CREATE TABLE THBI.DWD_PURCHASE_INVOICE_LINE (
  invoice_type          NUMBER        NOT NULL,
  invoice_no            VARCHAR2(20)  NOT NULL,
  invoice_line_no       NUMBER        NOT NULL,
  company_code          VARCHAR2(10),
  facility_code         VARCHAR2(10),
  supplier_code         VARCHAR2(20),
  po_no                 VARCHAR2(20),
  po_line_no            NUMBER,
  receipt_no            VARCHAR2(20),
  receipt_line_no       NUMBER,
  material_code         VARCHAR2(20),
  material_desc         VARCHAR2(130),
  quantity              NUMBER,
  unit_price_net        NUMBER,
  line_amount_excl_tax  NUMBER,
  line_amount_incl_tax  NUMBER,
  currency_code         VARCHAR2(3),
  line_type             NUMBER,
  receipt_date          DATE,
  accounting_date       DATE,
  creation_date         DATE,
  update_date           DATE,
  etl_load_ts           TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_PURCHASE_INVOICE_LINE PRIMARY KEY (invoice_type, invoice_no, invoice_line_no)
);

INSERT INTO THBI.DWD_PURCHASE_INVOICE_LINE
  (invoice_type, invoice_no, invoice_line_no, company_code, facility_code,
   supplier_code, po_no, po_line_no, receipt_no, receipt_line_no,
   material_code, material_desc, quantity, unit_price_net,
   line_amount_excl_tax, line_amount_incl_tax, currency_code, line_type,
   receipt_date, accounting_date, creation_date, update_date)
SELECT
  h.INVTYP_0,
  h.NUM_0,
  d.PIDLIN_0,
  NULLIF(h.CPY_0, ' '),
  NULLIF(d.FCY_0, ' '),
  NULLIF(d.BPSNUM_0, ' '),
  NULLIF(d.POHNUM_0, ' '),
  CASE WHEN d.POPLIN_0 = 0 THEN NULL ELSE d.POPLIN_0 END,
  NULLIF(d.PTHNUM_0, ' '),
  CASE WHEN d.PTDLIN_0 = 0 THEN NULL ELSE d.PTDLIN_0 END,
  NULLIF(d.ITMREF_0, ' '),
  NULLIF(d.ITMDES1_0, ' '),
  d.QTYPUU_0,
  d.NETPRI_0,
  d.AMTNOTLIN_0,
  d.AMTATILIN_0,
  NULLIF(d.NETCUR_0, ' '),
  d.LINTYP_0,
  CASE WHEN d.RCPDAT_0 > DATE '1900-01-01' THEN d.RCPDAT_0 END,
  CASE WHEN h.ACCDAT_0 > DATE '1900-01-01' THEN h.ACCDAT_0 END,
  CASE WHEN h.CREDAT_0 > DATE '1900-01-01' THEN h.CREDAT_0 END,
  CASE WHEN h.UPDDAT_0 > DATE '1900-01-01' THEN h.UPDDAT_0 END
FROM THBI.ODS_PINVOICED d
JOIN THBI.ODS_PINVOICE h
  ON h.INVTYP_0 = d.INVTYP_0 AND h.NUM_0 = d.NUM_0
WHERE h.INVTYP_0 IN (1, 2);

-- ---------- 付款单（头） ----------
CREATE TABLE THBI.DWD_SUPPLIER_PAYMENT (
  payment_no            VARCHAR2(20)  NOT NULL,
  payment_type          VARCHAR2(10)  NOT NULL,
  company_code          VARCHAR2(10),
  facility_code         VARCHAR2(10),
  supplier_code         VARCHAR2(20),
  currency_code         VARCHAR2(3),
  payment_amount        NUMBER,
  bank_amount           NUMBER,
  due_date              DATE,
  bank_date             DATE,
  value_date            DATE,
  bill_date             DATE,
  status                NUMBER,
  payment_reference     VARCHAR2(80),
  creation_date         DATE,
  update_date           DATE,
  etl_load_ts           TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_SUPPLIER_PAYMENT PRIMARY KEY (payment_no, payment_type)
);

INSERT INTO THBI.DWD_SUPPLIER_PAYMENT
  (payment_no, payment_type, company_code, facility_code, supplier_code,
   currency_code, payment_amount, bank_amount, due_date, bank_date,
   value_date, bill_date, status, payment_reference,
   creation_date, update_date)
SELECT
  NUM_0,
  PAYTYP_0,
  NULLIF(CPY_0, ' '),
  NULLIF(FCY_0, ' '),
  NULLIF(BPR_0, ' '),
  NULLIF(CUR_0, ' '),
  AMTCUR_0,
  AMTBAN_0,
  CASE WHEN DUDDAT_0 > DATE '1900-01-01' THEN DUDDAT_0 END,
  CASE WHEN BANDAT_0 > DATE '1900-01-01' THEN BANDAT_0 END,
  CASE WHEN VALDAT_0 > DATE '1900-01-01' THEN VALDAT_0 END,
  CASE WHEN BILDAT_0 > DATE '1900-01-01' THEN BILDAT_0 END,
  STA_0,
  NULLIF(REF_0, ' '),
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_PAYMENTH
WHERE PAYTYP_0 IN ('BKPAY', 'DRPAY', 'CAPAY');

-- ---------- 付款单行 ----------
CREATE TABLE THBI.DWD_SUPPLIER_PAYMENT_LINE (
  payment_no            VARCHAR2(20)  NOT NULL,
  payment_type          VARCHAR2(10)  NOT NULL,
  payment_line_no       NUMBER        NOT NULL,
  voucher_type          VARCHAR2(10),
  voucher_no            VARCHAR2(20),
  linked_invoice_no     VARCHAR2(10),
  line_amount           NUMBER,
  currency_code         VARCHAR2(3),
  creation_date         DATE,
  update_date           DATE,
  etl_load_ts           TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_SUPPLIER_PAYMENT_LINE PRIMARY KEY (payment_no, payment_type, payment_line_no)
);

INSERT INTO THBI.DWD_SUPPLIER_PAYMENT_LINE
  (payment_no, payment_type, payment_line_no, voucher_type, voucher_no,
   linked_invoice_no, line_amount, currency_code, creation_date, update_date)
SELECT
  h.NUM_0,
  h.PAYTYP_0,
  d.LIN_0,
  d.VCRTYP_0,
  NULLIF(d.VCRNUM_0, ' '),
  NULLIF(d.BPRINV_0, ' '),
  d.AMTLIN_0,
  NULLIF(d.CURLIN_0, ' '),
  CASE WHEN h.CREDAT_0 > DATE '1900-01-01' THEN h.CREDAT_0 END,
  CASE WHEN h.UPDDAT_0 > DATE '1900-01-01' THEN h.UPDDAT_0 END
FROM THBI.ODS_PAYMENTD d
JOIN THBI.ODS_PAYMENTH h
  ON h.NUM_0 = d.NUM_0
WHERE h.PAYTYP_0 IN ('BKPAY', 'DRPAY', 'CAPAY');
