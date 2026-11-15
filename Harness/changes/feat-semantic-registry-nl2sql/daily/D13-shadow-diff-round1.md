# 语义发布影子比对报告

- 发布版本：v-p2-d12（id=1）
- manifest_hash：4d46898611e8acb9fe5a211df6e8b00d928fc33ac4f02938b842d39fb23a292a
- 类集合：两侧一致，共 37 类
- 直读侧文本：81486 字符
- 快照侧文本：81486 字符

## 差异指标

- **内容差异行数：0**（行多重集；>0 = 快照丢了事实）
- 顺序敏感差异行数：6524（逐行序列；含纯顺序差异）
- 仅列序不同的块：38

内容差异 = 0 ✅（两侧渲染的事实完全一致）

已知差异来源：治理表侧属性顺序 = 物理行序，快照侧 = propertyName 排序（38 个块受影响：类块 / 关键口径摘要块 / JOIN 块）。
  - ### DIM_FACILITY (公司及工厂的信息): table=DIM_FACILITY
  - ### DIM_IMATERIAL (物料主数据): table=DIM_IMATERIAL
  - ### DIM_SUPPLIER (供应商主数据): table=DIM_SUPPLIER
  - ### DWD_AP_INVOICE_DTL (发票信息): table=DWD_AP_INVOICE_DTL
  - ### DWD_ARRIVAL_ORDER_DTL (DWD到货单宽表): table=DWD_ARRIVAL_ORDER_DTL
  - ### DWD_BOM (BOM主数据): table=DWD_BOM  [无关联边，不可跨表JOIN]
  - ### DWD_BOM_DETAIL (BOM明细主数据): table=DWD_BOM_DETAIL  [无关联边，不可跨表JOIN]
  - ### DWD_BUSINESS_PARTNER (合作伙伴主数据): table=DWD_BUSINESS_PARTNER  [无关联边，不可跨表JOIN]
  - ### DWD_CUSTOMER (客户主数据): table=DWD_CUSTOMER  [无关联边，不可跨表JOIN]
  - ### DWD_GOODS_RECEIPT_DTL (DWD收货单宽表): table=DWD_GOODS_RECEIPT_DTL
  - ### DWD_PURCHASE_ORDER_DTL (DWD采购订单宽表): table=DWD_PURCHASE_ORDER_DTL
  - ### DWD_PURCHASE_QUOTATION_YEARLY_DTL (年度报价信息): table=DWD_PURCHASE_QUOTATION_YEARLY_DTL  [无关联边，不可跨表JOIN]
  - ### DWD_RECONCILIATION_BILL_DTL (DWD对账单): table=DWD_RECONCILIATION_BILL_DTL
  - ### DWD_SUPPLIER_PRICE_LIST: table=DWD_SUPPLIER_PRICE_LIST  [无关联边，不可跨表JOIN]
  - ### DWD_SUPPLIER_PRICE_LIST_CONFIG (DWD报价明细配置信息): table=DWD_SUPPLIER_PRICE_LIST_CONFIG  [无关联边，不可跨表JOIN]
  - ### DWD_SUPPLIER_PRICE_LIST_DEL (DWD报价清单): table=DWD_SUPPLIER_PRICE_LIST  [无关联边，不可跨表JOIN]
  - ### DWD_SUPPLIER_PRICE_LIST_HEADER: table=DWD_SUPPLIER_PRICE_LIST_HEADER  [无关联边，不可跨表JOIN]
  - ### ODS_BOM: table=ODS_BOM  [无关联边，不可跨表JOIN]
  - ### ODS_BOMD: table=ODS_BOMD  [无关联边，不可跨表JOIN]
  - ### ODS_BPARTNER: table=ODS_BPARTNER  [无关联边，不可跨表JOIN]
  ...（共 38 个类，完整清单见脚本输出）

## 逐条差异

```diff
--- direct(治理表)
+++ snapshot(发布快照)
@@ -10,43 +10,41 @@
 - DWD_RECONCILIATION_BILL_DTL.INVOICE_STATUS: 开票状态（0未开票/1已开发票/2已开红冲发票）
 ### DIM_FACILITY (公司及工厂的信息): table=DIM_FACILITY
   Columns:
-    UVYCOD_0: STRING
-    IVYFLG_0: DECIMAL
-    WRHGES_0: DECIMAL
-    RTNWRH_0: STRING
-    TRFWRH_0: STRING
-    YFTLB_0: DECIMAL
-    SPAOPEIGIC_0: DECIMAL
-    UPDTICK_0: DECIMAL
-    FCYNAM_0: STRING
-    FCYSHO_0: STRING
+    ACCCOD_0: STRING
+    AUUID_0: STRING
+    BIDNUM_0: STRING
+    BPAADD_0: STRING
+    BPTNUM_0: STRING
+    CCE_0: STRING
+    CCE_1: STRING
+    CCE_10: STRING
+    CCE_11: STRING
+    CCE_12: STRING
+    CCE_13: STRING
+    CCE_14: STRING
+    CCE_15: STRING
+    CCE_16: STRING
+    CCE_17: STRING
+    CCE_18: STRING
+    CCE_19: STRING
+    CCE_2: STRING
+    CCE_3: STRING
+    CCE_4: STRING
+    CCE_5: STRING
+    CCE_6: STRING
+    CCE_7: STRING
+    CCE_8: STRING
+    CCE_9: STRING
+    CNTNAM_0: STRING
+    CREDATTIM_0: DATETIME
+    CREDAT_0: DATETIME
+    CREUSR_0: STRING
+    CRN_0: STRING
     CRY_0: STRING
-    CRN_0: STRING
-    NAF_0: STRING
-    BPTNUM_0: STRING
-    MFGFLG_0: DECIMAL
-    SALFLG_0: DECIMAL
-    PURFLG_0: DECIMAL
-    WRHFLG_0: DECIMAL
-    FINFLG_0: DECIMAL
-    FINRSPFCY_0: STRING
+    DADFCY_0: STRING
     DADFLG_0: DECIMAL
-    PAYBAN_0: STRING
-    DADFCY_0: STRING
-    LEG_0: STRING
-    ACCCOD_0: STRING
-    BPAADD_0: STRING
-    GEOCOD_0: STRING
     DIE_0: STRING
     DIE_1: STRING
-    DIE_2: STRING
-    DIE_3: STRING
-    DIE_4: STRING
-    DIE_5: STRING
-    DIE_6: STRING
-    DIE_7: STRING
-    DIE_8: STRING
-    DIE_9: STRING
     DIE_10: STRING
     DIE_11: STRING
     DIE_12: STRING
@@ -57,18 +55,87 @@
     DIE_17: STRING
     DIE_18: STRING
     DIE_19: STRING
+    DIE_2: STRING
+    DIE_3: STRING
+    DIE_4: STRING
+    DIE_5: STRING
+    DIE_6: STRING
+    DIE_7: STRING
+    DIE_8: STRING
+    DIE_9: STRING
+    ETL_LOAD_TS: DATETIME
+    FCYDES_0: STRING
+    FCYNAM_0: STRING
+    FCYSHO_0: STRING
+    FCY_0 (工厂编码): STRING [PK] 说明: 工厂编号，一个公司有多少工厂
+    FINFLG_0: DECIMAL
+    FINRSPFCY_0: STRING
+    GEOCOD_0: STRING
     INSCTYFLG_0: STRING
-    BIDNUM_0: STRING
+    IVYFCY_0: STRING
+    IVYFLG_0: DECIMAL
+    LEGCPY_0: STRING 说明: 工厂的所属的公司编码
+    LEG_0: STRING
+    MFGFLG_0: DECIMAL
+    MFGWRH_0: STRING
+    MFPWRH_0: STRING
+    MFRWRH_0: STRING
+    NAF_0: STRING
+    PAYBAN_0: STRING
+    PURFLG_0: DECIMAL
+    RCPWRH_0: STRING
+    RTNWRH_0: STRING
+    SALFLG_0: DECIMAL
+    SCCWRH_0: STRING
+    SCOWRH_0: STRING
+    SHIWRH_0: STRING
+    SPAOPEIGIC_0: DECIMAL
+    TRAWRH_0: STRING
+    TRFWRH_0: STRING
+    UPDDATTIM_0: DATETIME
+    UPDDAT_0: DATETIME
+    UPDTICK_0: DECIMAL
+    UPDUSR_0: STRING
+    UVYCOD_0: STRING
+    UVYDAY_0: DECIMAL
+    UVYDAY_1: DECIMAL
+    UVYDAY_2: DECIMAL
+    UVYDAY_3: DECIMAL
+    UVYDAY_4: DECIMAL
+    UVYDAY_5: DECIMAL
+    UVYDAY_6: DECIMAL
+    WRHFLG_0: DECIMAL
+    WRHGES_0: DECIMAL
+    XCN07C12FT4_0: DECIMAL
+    XCN07FCY_0: STRING
+    XCN07FEETYP_0: DECIMAL
+    XCN07MFGC_0: DECIMAL
+    XCN07PERSTA_0: DECIMAL
+    XCN07PPVC_0: DECIMAL
+    XCN07SALC_0: DECIMAL
+    XCN07SPVC_0: DECIMAL
+    XCN07TRSTYP_0: STRING
+    XDOMAIN_0: STRING
+    XTRADEFLG_0: DECIMAL
+    XWSTZFCY_0: STRING
+    XWSTZLOC_0: STRING
+    YCSH_0: DATETIME
+    YFTLB_0: DECIMAL
+    YHSFS_0: DECIMAL
+    YRZC_0: DECIMAL
+    YYDCS_0: DECIMAL
+    YZJCHECK_0: DECIMAL
+    YZZCS_0: DECIMAL
+    ZBJHL_0: DECIMAL
+### DIM_IMATERIAL (物料主数据): table=DIM_IMATERIAL
+  Columns:
+    ACCCOD_0: STRING
+    ALTBOMHDK_0: DECIMAL
+    AUUID_0: STRING
+    BRDCOD_0: DECIMAL
+    BUY_0: STRING
     CCE_0: STRING
     CCE_1: STRING
-    CCE_2: STRING
-    CCE_3: STRING
-    CCE_4: STRING
-    CCE_5: STRING
-    CCE_6: STRING
-    CCE_7: STRING
-    CCE_8: STRING
-    CCE_9: STRING
     CCE_10: STRING
     CCE_11: STRING
     CCE_12: STRING
@@ -79,8 +146,1437 @@
     CCE_17: STRING
     CCE_18: STRING
     CCE_19: STRING
+    CCE_2: STRING
+    CCE_3: STRING
+    CCE_4: STRING
+    CCE_5: STRING
+    CCE_6: STRING
+    CCE_7: STRING
+    CCE_8: STRING
+    CCE_9: STRING
+    CFGBPRNUM_0: STRING
+    CFGBPRREF_0: STRING
+    CFGDELDAT_0: DATETIME
+    CFGFLDALP1_0: STRING
+    CFGFLDALP2_0: STRING
+    CFGFLDALP3_0: STRING
+    CFGFLDALP4_0: STRING
+    CFGFLDALP5_0: STRING
+    CFGFLDALP6_0: STRING
+    CFGFLDNUM1_0: DECIMAL
+    CFGFLDNUM2_0: DECIMAL
+    CFGFLDNUM3_0: DECIMAL
+    CFGFLDNUM4_0: DECIMAL
+    CFGFLDNUM5_0: DECIMAL
...（已截断，完整差异 6618 行）
```

---

## 第二轮（2026-11-15 改写工具后复跑）

评审要求两处补强，本轮已落地并复跑：

1. **退出码三态**：0 = 内容 0 **且** 顺序 0；1 = 有内容差异；3 = 内容 0 但顺序 > 0
   （退出码 0 被流水线读作「完全一致」，顺序差异被吞掉 ⇒ D14 判据失效）。
2. **子集模式** `--subset-mode none|each|groups`：生产渲染的是**召回子集**（≤ 30 类），
   子集外的父类 / 外键目标解析成 `None` 这条逻辑**只在子集里才被触发**；
   只比全集 ⇒ 这条逻辑一次都没跑到 ⇒ 发现不了子集语义下的偏差。

复跑结果（dev 库 5433 / `qa_metadata`，只读；快照 `v-p2-d12`）：

| 模式 | 子集数 | 内容差异行数 | 顺序敏感差异行数 | 仅列序不同的块 | 退出码 |
|---|---|---|---|---|---|
| `none`（全集） | 1 | **0** | 6524 | 38 | 3 |
| `each`（逐类） | 37 | **0** | 6004 | 37 | 3 |

⇒ 逐类子集下**内容差异仍为 0**：父类块、外键目标表、JOIN 段在「子集外 ⇒ 不渲染」
这条口径上，两侧**逐条一致**。
基线夹具用改写后的脚本重抓 ⇒ 与首轮基线**逐字节相同**（`diff` 零差异）。
