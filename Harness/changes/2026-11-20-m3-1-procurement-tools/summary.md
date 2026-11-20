# 变更：M3-1 采购只读工具第一批（material_search / supplier_search）

- 日期：2026-11-20　分支：`feat/procurement-knowledge-documents`（堆叠，off 知识支 `43e5bdc0`）
- 设计稿 §5.4 工具契约：三参 handler（session/args/ctx → ToolResult）+ 白名单登记 + DB 元数据分工

## 内容
- **新模块** `app/services/agent_tools_procurement.py`（同 wiki 分工：handler 在代码、元数据在 DB）：
  - `material_search`：编码/名称/统一键 → 标准物料（复用 `searchMappings` trigram 语义）；
  - `supplier_search` **双路**：`materialKey` ⇒ Neo4j `SUPPLIES` 反查（图路，35 万边，
    `via=graph_supplies` 自证）；否则名称/编码（`via=entity_mapping`）；
    空选择器 ⇒ 友好提示（不异常）。**零 LLM / 零写**（与 supplier_360 同款纪律）。
- **Neo4j helper** `findSuppliersOfMaterial`（+Async 包装，对齐模块 to_thread 模式）；
  label 白名单校验同款；物料无供应商 ⇒ 空列表（调用方区分语义）。
- **注册**：BUILTIN_HANDLERS + ARG_EXTRACTORS（chat 前奏语义对齐 wiki_text：整条输入即检索词）；
  **三个集合相等守卫同步更新**（test_agent_tool_registry / test_agent_tool_assembly ×2 套）。
- extractor 签名勘误：`Callable[[str], dict | None]`（raw 是问句文本非 dict）——初版直通 lambda
  已改为 wiki_text 同款。

## M3 剩余（已 recon 定界）
- `purchase_history_query` / `quotation_query`：需业务库 adapter 管线（datasource 解析 +
  只读守卫）⇒ M3-2；`quotation_compare` 需比价规则（D3/M5）⇒ 挂起；
- `procurement_policy_search`：M2-3 的 documents/search 已就绪，但 ctx 无 user（密级闸需要身份）
  ⇒ 待 ctx 扩 user（独立批）；
- DB `agent_tool_config` 元数据行 = admin 运营步骤（handler_ref=BUILTIN 已可用）。

## 测试（6 新用例 + 2 守卫文件同步）
白名单登记 ×1 / material_search 真实 PG ×2 / supplier_search 双路 + 空选择器 ×3 /
registry 守卫 ×N（三处集合相等全部同步）。工具家族回归 31 passed；ruff 净新增 0。
