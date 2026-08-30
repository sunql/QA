-- ============================================================
-- 02_dwd_master.sql - DWD 层（主数据/制造/报价，12 表）
-- 命名：DWD_<实体>，英文 snake_case，精选核心字段
-- 清洗规则：
--   * X3 空日期占位符 1599-12-31 -> NULL（CASE WHEN d > DATE '1900-01-01'）
--   * 空串 ' ' -> NULL（NULLIF）
--   * X3 布尔 NUMBER(1) 0/1 -> NUMBER 保留原值
-- ============================================================

-- ---------- 供应商主数据 ----------
CREATE TABLE THBI.DWD_SUPPLIER (
  supplier_code      VARCHAR2(20)  NOT NULL,
  supplier_name      VARCHAR2(90),
  contact_name       VARCHAR2(40),
  payment_code       VARCHAR2(20),
  payment_term_type  VARCHAR2(20),
  currency_code      VARCHAR2(3),
  zero_stock_flag    NUMBER,
  creation_date      DATE,
  update_date        DATE,
  etl_load_ts        TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_SUPPLIER PRIMARY KEY (supplier_code)
);

-- zero_stock_flag：2=零库存供应商，1=非零库存供应商（X3 YPTHFLGM_0，DWD 起按标准保留原值）
INSERT INTO THBI.DWD_SUPPLIER
  (supplier_code, supplier_name, contact_name, payment_code,
   payment_term_type, currency_code, zero_stock_flag, creation_date, update_date)
SELECT
  BPSNUM_0,
  NULLIF(BPSNAM_0, ' '),
  NULLIF(CNTNAM_0, ' '),
  NULLIF(BPRPAY_0, ' '),
  NULLIF(BPTNUM_0, ' '),
  NULLIF(CUR_0, ' '),
  YPTHFLGM_0,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_BPSUPPLIER;

-- ---------- 业务伙伴目录 ----------
CREATE TABLE THBI.DWD_BUSINESS_PARTNER (
  partner_code    VARCHAR2(20)  NOT NULL,
  partner_name    VARCHAR2(90),
  partner_short_name VARCHAR2(40),
  vat_number      VARCHAR2(30),
  currency_code   VARCHAR2(3),
  creation_date   DATE,
  update_date     DATE,
  etl_load_ts     TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_BUSINESS_PARTNER PRIMARY KEY (partner_code)
);

INSERT INTO THBI.DWD_BUSINESS_PARTNER
  (partner_code, partner_name, partner_short_name, vat_number,
   currency_code, creation_date, update_date)
SELECT
  BPRNUM_0,
  NULLIF(BPRNAM_0, ' '),
  NULLIF(BPRSHO_0, ' '),
  NULLIF(VATNUM_0, ' '),
  NULLIF(CUR_0, ' '),
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_BPARTNER;

-- ---------- 客户主数据 ----------
CREATE TABLE THBI.DWD_CUSTOMER (
  customer_code    VARCHAR2(20)  NOT NULL,
  customer_name    VARCHAR2(90),
  customer_status  NUMBER,
  supplier_code    VARCHAR2(20),
  currency_code    VARCHAR2(3),
  creation_date    DATE,
  update_date      DATE,
  etl_load_ts      TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_CUSTOMER PRIMARY KEY (customer_code)
);

INSERT INTO THBI.DWD_CUSTOMER
  (customer_code, customer_name, customer_status, supplier_code,
   currency_code, creation_date, update_date)
SELECT
  BPCNUM_0,
  NULLIF(BPCNAM_0, ' '),
  BPCSTA_0,
  NULLIF(BPCBPSNUM_0, ' '),
  NULLIF(CUR_0, ' '),
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_BPCUSTOMER;

-- ---------- 承运商主数据（源 0 行，结构先行） ----------
CREATE TABLE THBI.DWD_CARRIER (
  carrier_code    VARCHAR2(20)  NOT NULL,
  carrier_name    VARCHAR2(90),
  creation_date   DATE,
  update_date     DATE,
  etl_load_ts     TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_CARRIER PRIMARY KEY (carrier_code)
);

INSERT INTO THBI.DWD_CARRIER
  (carrier_code, carrier_name, creation_date, update_date)
SELECT
  BPTNUM_0,
  NULLIF(BPTNAM_0, ' '),
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_BPCARRIER;

-- ---------- 物料主数据 ----------
CREATE TABLE THBI.DWD_MATERIAL (
  material_code       VARCHAR2(20)  NOT NULL,
  description_1       VARCHAR2(130),
  description_2       VARCHAR2(150),
  description_3       VARCHAR2(100),
  material_status     NUMBER,
  item_category       VARCHAR2(20),
  material_category   VARCHAR2(20),
  standard_weight     NUMBER,
  standard_volume     NUMBER,
  purchase_base_price NUMBER,
  creation_date       DATE,
  update_date         DATE,
  etl_load_ts         TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_MATERIAL PRIMARY KEY (material_code)
);

-- material_category：物料类别（X3 TCLCOD_0，产品大类，如 TH1/TJ1/A06E；item_category 为自定义分类 YITMCAT_0）
INSERT INTO THBI.DWD_MATERIAL
  (material_code, description_1, description_2, description_3,
   material_status, item_category, material_category, standard_weight, standard_volume,
   purchase_base_price, creation_date, update_date)
SELECT
  ITMREF_0,
  NULLIF(ITMDES1_0, ' '),
  NULLIF(ITMDES2_0, ' '),
  NULLIF(ITMDES3_0, ' '),
  ITMSTA_0,
  NULLIF(YITMCAT_0, ' '),
  NULLIF(TCLCOD_0, ' '),
  ITMWEI_0,
  ITMVOU_0,
  PURBASPRI_0,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_ITMMASTER;

-- ---------- 工厂/地点 ----------
CREATE TABLE THBI.DWD_FACILITY (
  facility_code    VARCHAR2(10)  NOT NULL,
  facility_name    VARCHAR2(80),
  facility_short_name VARCHAR2(40),
  legal_company    VARCHAR2(20),
  creation_date    DATE,
  update_date      DATE,
  etl_load_ts      TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_FACILITY PRIMARY KEY (facility_code)
);

INSERT INTO THBI.DWD_FACILITY
  (facility_code, facility_name, facility_short_name, legal_company,
   creation_date, update_date)
SELECT
  FCY_0,
  NULLIF(FCYNAM_0, ' '),
  NULLIF(FCYSHO_0, ' '),
  NULLIF(LEGCPY_0, ' '),
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_FACILITY;

-- ---------- 物料-工厂设置 ----------
CREATE TABLE THBI.DWD_ITEM_FACILITY (
  material_code    VARCHAR2(20)  NOT NULL,
  facility_code    VARCHAR2(10)  NOT NULL,
  stock_location   NUMBER,
  lot_qty          NUMBER,
  min_order_qty    NUMBER,
  creation_date    DATE,
  update_date      DATE,
  etl_load_ts      TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_ITEM_FACILITY PRIMARY KEY (material_code, facility_code)
);

INSERT INTO THBI.DWD_ITEM_FACILITY
  (material_code, facility_code, stock_location, lot_qty,
   min_order_qty, creation_date, update_date)
SELECT
  ITMREF_0,
  STOFCY_0,
  LOCNUM_0,
  MFGLOTQTY_0,
  REOMINQTY_0,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_ITMFACILIT;

-- ---------- BOM 头 ----------
CREATE TABLE THBI.DWD_BOM (
  material_code    VARCHAR2(20)  NOT NULL,
  bom_description  VARCHAR2(40),
  usage_status     NUMBER,
  valid_from       DATE,
  valid_to         DATE,
  base_qty         NUMBER,
  creation_date    DATE,
  update_date      DATE,
  etl_load_ts      TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL
);

INSERT INTO THBI.DWD_BOM
  (material_code, bom_description, usage_status, valid_from, valid_to,
   base_qty, creation_date, update_date)
SELECT
  ITMREF_0,
  NULLIF(BOMDES_0, ' '),
  USESTA_0,
  CASE WHEN BOHSTRDAT_0 > DATE '1900-01-01' THEN BOHSTRDAT_0 END,
  CASE WHEN BOHENDDAT_0 > DATE '1900-01-01' THEN BOHENDDAT_0 END,
  BASQTY_0,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_BOM;

-- ---------- BOM 行 ----------
CREATE TABLE THBI.DWD_BOM_DETAIL (
  material_code     VARCHAR2(20)  NOT NULL,
  bom_sequence      NUMBER        NOT NULL,
  component_code    VARCHAR2(20),
  component_qty     NUMBER,
  valid_from        DATE,
  valid_to          DATE,
  creation_date     DATE,
  update_date       DATE,
  etl_load_ts       TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL
);

INSERT INTO THBI.DWD_BOM_DETAIL
  (material_code, bom_sequence, component_code, component_qty,
   valid_from, valid_to, creation_date, update_date)
SELECT
  ITMREF_0,
  BOMSEQNUM_0,
  NULLIF(CPNITMREF_0, ' '),
  BOMQTY_0,
  CASE WHEN BOMSTRDAT_0 > DATE '1900-01-01' THEN BOMSTRDAT_0 END,
  CASE WHEN BOMENDDAT_0 > DATE '1900-01-01' THEN BOMENDDAT_0 END,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_BOMD;

-- ---------- 工艺路线工序 ----------
CREATE TABLE THBI.DWD_ROUTING_OPERATION (
  material_code     VARCHAR2(20)  NOT NULL,
  operation_no      NUMBER        NOT NULL,
  facility_code     VARCHAR2(10),
  operation_desc    VARCHAR2(120),
  subcontractor_code VARCHAR2(20),
  subcontract_price NUMBER,
  base_qty          NUMBER,
  valid_from        DATE,
  valid_to          DATE,
  creation_date     DATE,
  update_date       DATE,
  etl_load_ts       TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL
);

INSERT INTO THBI.DWD_ROUTING_OPERATION
  (material_code, operation_no, facility_code, operation_desc,
   subcontractor_code, subcontract_price, base_qty, valid_from, valid_to,
   creation_date, update_date)
SELECT
  ITMREF_0,
  OPENUM_0,
  NULLIF(FCY_0, ' '),
  NULLIF(ROODES_0, ' '),
  NULLIF(BPRNUM_0, ' '),
  YPRI_0,
  BASQTY_0,
  CASE WHEN VALSTRDAT_0 > DATE '1900-01-01' THEN VALSTRDAT_0 END,
  CASE WHEN VALENDDAT_0 > DATE '1900-01-01' THEN VALENDDAT_0 END,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_ROUOPE;

-- ---------- 询价单（源 0 行，结构先行） ----------
CREATE TABLE THBI.DWD_QUOTATION (
  quotation_no     VARCHAR2(20)  NOT NULL,
  company_code     VARCHAR2(10),
  facility_code    VARCHAR2(10),
  quotation_date   DATE,
  reference        VARCHAR2(20),
  line_count       NUMBER,
  creation_date    DATE,
  update_date      DATE,
  etl_load_ts      TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_QUOTATION PRIMARY KEY (quotation_no)
);

INSERT INTO THBI.DWD_QUOTATION
  (quotation_no, company_code, facility_code, quotation_date,
   reference, line_count, creation_date, update_date)
SELECT
  PQHNUM_0,
  NULLIF(CPY_0, ' '),
  NULLIF(PQHFCY_0, ' '),
  CASE WHEN PQHDAT_0 > DATE '1900-01-01' THEN PQHDAT_0 END,
  NULLIF(PQHREF_0, ' '),
  LINNBR_0,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_PQUOTAT;

-- ---------- 询价单行（源 0 行，结构先行） ----------
CREATE TABLE THBI.DWD_QUOTATION_LINE (
  quotation_no          VARCHAR2(20)  NOT NULL,
  quotation_line_no     NUMBER        NOT NULL,
  material_code         VARCHAR2(20),
  material_desc         VARCHAR2(130),
  quantity              NUMBER,
  promised_receipt_date DATE,
  requisition_no        VARCHAR2(20),
  requisition_line_no   NUMBER,
  line_text             VARCHAR2(120),
  creation_date         DATE,
  update_date           DATE,
  etl_load_ts           TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT PK_DWD_QUOTATION_LINE PRIMARY KEY (quotation_no, quotation_line_no)
);

INSERT INTO THBI.DWD_QUOTATION_LINE
  (quotation_no, quotation_line_no, material_code, material_desc,
   quantity, promised_receipt_date, requisition_no, requisition_line_no,
   line_text, creation_date, update_date)
SELECT
  PQHNUM_0,
  PQDLIN_0,
  NULLIF(ITMREF_0, ' '),
  NULLIF(ITMDES1_0, ' '),
  QTYPUU_0,
  CASE WHEN RCPDAT_0 > DATE '1900-01-01' THEN RCPDAT_0 END,
  NULLIF(PSHNUM_0, ' '),
  PSDLIN_0,
  NULLIF(LINTEX_0, ' '),
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_PQUOTATD;

-- ---------- 供应商价格表（行级明细；表头/取价配置见下方 HEADER / CONFIG 两表） ----------
CREATE TABLE THBI.DWD_SUPPLIER_PRICE_LIST (
  price_list_code    VARCHAR2(24)  NOT NULL,   -- PLI_0 价目表号
  price_list_record  VARCHAR2(80)  NOT NULL,   -- PLICRD_0 价目表版本（关联表头）
  price_list_line    NUMBER        NOT NULL,   -- PLILIN_0 行号
  material_code      VARCHAR2(20),
  currency_code      VARCHAR2(12),
  min_qty            NUMBER,
  max_qty            NUMBER,
  unit_price         NUMBER,
  valid_from         DATE,
  valid_to           DATE,
  creation_date      DATE,
  update_date        DATE,
  etl_load_ts        TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT pk_dwd_supplier_price_list
    PRIMARY KEY (price_list_code, price_list_record, price_list_line)
);

INSERT INTO THBI.DWD_SUPPLIER_PRICE_LIST
  (price_list_code, price_list_record, price_list_line, material_code, currency_code,
   min_qty, max_qty, unit_price, valid_from, valid_to, creation_date, update_date)
SELECT
  PLI_0,
  PLICRD_0,
  PLILIN_0,
  NULLIF(CPNITMREF_0, ' '),
  NULLIF(CUR_0, ' '),
  MINQTY_0,
  MAXQTY_0,
  PRI_0,
  CASE WHEN PLISTRDAT_0 > DATE '1900-01-01' THEN PLISTRDAT_0 END,
  CASE WHEN PLIENDDAT_0 > DATE '1900-01-01' THEN PLIENDDAT_0 END,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_PPRICLIST;

-- ---------- 供应商价格表表头（版本主数据：每行一个价目表版本，PLI_0+PLICRD_0 唯一） ----------
CREATE TABLE THBI.DWD_SUPPLIER_PRICE_LIST_HEADER (
  price_list_code    VARCHAR2(24)  NOT NULL,   -- PLI_0 价目表号
  price_list_record  VARCHAR2(80)  NOT NULL,   -- PLICRD_0 价目表版本
  valid_from         DATE,                     -- PLISTRD_0 生效日期
  valid_to           DATE,                     -- PLIENDDAT_0 失效日期
  line_count         NUMBER,                   -- LINNBR_0 行数
  validity_status    NUMBER,                   -- VALSTA_0 有效状态
  created_by         VARCHAR2(20),             -- CREUSR_0
  creation_date      DATE,                     -- CREDAT_0
  updated_by         VARCHAR2(20),             -- UPDUSR_0
  update_date        DATE,                     -- UPDDAT_0
  etl_load_ts        TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT pk_dwd_supplier_price_list_header
    PRIMARY KEY (price_list_code, price_list_record)
);

INSERT INTO THBI.DWD_SUPPLIER_PRICE_LIST_HEADER
  (price_list_code, price_list_record, valid_from, valid_to, line_count,
   validity_status, created_by, creation_date, updated_by, update_date)
SELECT
  PLI_0,
  PLICRD_0,
  CASE WHEN PLISTRDAT_0 > DATE '1900-01-01' THEN PLISTRDAT_0 END,
  CASE WHEN PLIENDDAT_0 > DATE '1900-01-01' THEN PLIENDDAT_0 END,
  LINNBR_0,
  VALSTA_0,
  CREUSR_0,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  UPDUSR_0,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_PPRICFICH
WHERE TRIM(PLI_0) IS NOT NULL;  -- 滤掉 21 行 PLI_0 空白的垃圾版本

-- ---------- 供应商价格表取价配置（每价目表一条；条件结构 + 取价规则） ----------
CREATE TABLE THBI.DWD_SUPPLIER_PRICE_LIST_CONFIG (
  price_list_code          VARCHAR2(24)   NOT NULL,   -- PLI_0 价目表号
  local_name               VARCHAR2(240),             -- LANDESSHO_0 价目表说明
  price_list_status        VARCHAR2(40),              -- PLISTC_0 状态
  price_list_type          NUMBER,                    -- PLITYP_0
  price_priority           NUMBER,                    -- PIO_0 取价优先级
  enabled_flag             NUMBER,                    -- PLIENAFLG_0 启用标志
  price_field              VARCHAR2(1000),            -- PRIFLD_0 价格字段
  price_type               NUMBER,                    -- PRITYP_0
  currency_code            VARCHAR2(12),              -- CUR_0
  criteria_count           NUMBER,                    -- CRINBR_0 条件数量
  supplier_criterion_count NUMBER,                    -- CRIBPSNUM_0
  uom_criterion_count      NUMBER,                    -- CRIUOMNUM_0
  item_criterion_count     NUMBER,                    -- CRIITMNUM_0
  currency_criterion_count NUMBER,                    -- CRICURNUM_0
  criterion1_field         VARCHAR2(48),              -- FLD_0 条件1字段
  criterion1_abbr          VARCHAR2(12),              -- ABB_0 条件1简称
  criterion1_description   VARCHAR2(80),              -- CRIDES_0 条件1描述
  criterion1_table         VARCHAR2(48),              -- FIL_0 条件1来源表
  created_by               VARCHAR2(20),              -- CREUSR_0
  creation_date            DATE,                      -- CREDAT_0
  updated_by               VARCHAR2(20),              -- UPDUSR_0
  update_date              DATE,                      -- UPDDAT_0
  etl_load_ts              TIMESTAMP(3)  DEFAULT SYSTIMESTAMP NOT NULL,
  CONSTRAINT pk_dwd_supplier_price_list_config
    PRIMARY KEY (price_list_code)
);

INSERT INTO THBI.DWD_SUPPLIER_PRICE_LIST_CONFIG
  (price_list_code, local_name, price_list_status, price_list_type, price_priority,
   enabled_flag, price_field, price_type, currency_code, criteria_count,
   supplier_criterion_count, uom_criterion_count, item_criterion_count,
   currency_criterion_count, criterion1_field, criterion1_abbr, criterion1_description,
   criterion1_table, created_by, creation_date, updated_by, update_date)
SELECT
  PLI_0,
  NULLIF(LANDESSHO_0, ' '),
  NULLIF(PLISTC_0, ' '),
  PLITYP_0,
  PIO_0,
  PLIENAFLG_0,
  PRIFLD_0,
  PRITYP_0,
  NULLIF(CUR_0, ' '),
  CRINBR_0,
  CRIBPSNUM_0,
  CRIUOMNUM_0,
  CRIITMNUM_0,
  CRICURNUM_0,
  FLD_0,
  ABB_0,
  CRIDES_0,
  FIL_0,
  CREUSR_0,
  CASE WHEN CREDAT_0 > DATE '1900-01-01' THEN CREDAT_0 END,
  UPDUSR_0,
  CASE WHEN UPDDAT_0 > DATE '1900-01-01' THEN UPDDAT_0 END
FROM THBI.ODS_PPRICCONF;
