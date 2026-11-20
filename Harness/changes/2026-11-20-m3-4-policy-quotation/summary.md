# 变更：M3-4 收尾批 —— quotation_query（年度报价明细）+ procurement_policy_search（ctx 扩 user）

- 日期：2026-11-20　分支：`feat/m3-4-policy-quotation`（堆叠 off documents 支 `93cfdf7f`）

## 内容（设计稿 §5.4 工具集的最后两个可行项）

1. **`quotation_query`**（业务库只读）——**换表依据**（源侧实测 source-capability.md:35）：
   `DWD_SUPPLIER_PRICE_LIST.MATERIAL_CODE` **全 NULL** ⇒ 不可用；
   落点 `DWD_PURCHASE_QUOTATION_YEARLY_DTL`（167,909 行，ITEM_CODE/SUPPLIER_CODE/
   SEG_YEAR/UNIT_PRICE_EX_TAX 100% 填充）。supplierKey/materialKey/segmentYear
   任一必填；同款只读管线（SQL Guard + adapter + 停用源 fail-closed）。
2. **`procurement_policy_search`**（M2-3 文档检索接工具）：
   **密级闸 fail-closed**——ctx.user 缺失 ⇒ 不检索（无密级核验 = 越权面）；
   ctx.user 在 ⇒ searchProcurementDocuments 全链（clearance 透传 + 证据契约）。
3. **AgentToolContext 扩 `user`**（可选）：runtime.run 透传（API 层 `_user` 依赖
   现状未使用 ⇒ 接上）；runner worker 路径 None ⇒ policy 工具显式降级（登记）。
4. **注册**：BUILTIN_HANDLERS + ARG_EXTRACTORS（quotation 抽 supplier key；
   policy 整条输入即问题）+ 三处集合守卫同步（第 5/6 个工具）。

## 顺带修复
`agent_runtime_service.py` 缺 `from typing import Any`（基线 F821 的根源）——补上
⇒ 基线 4 个 F821 Any 归零（剩 LlmMessage/F841 为基线既有）。

## M3 工具集终态（设计稿 §5.4 首期 7 个）
supplier_search ✅ / material_search ✅ / purchase_history_query ✅ / quotation_query ✅ /
procurement_policy_search ✅ / **contract_document_search**（=documents/search 复用，注册别名可后续加）/
**quotation_compare**（比价规则 D3/M5，挂起）。ctx 扩 user/datasource 均已就位。
