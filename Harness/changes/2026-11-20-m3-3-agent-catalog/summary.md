# 变更：M3-3 采购 Agent 场景目录 + 场景 Agent 注册（设计稿 §5.2 / §8.4）

- 日期：2026-11-20　分支：`feat/procurement-knowledge-documents`（堆叠累积）

## 内容
1. **`GET /api/v1/procurement/agents/catalog`**（新路由 `procurement_agents.py`，
   挂 `/api/v1/procurement` 前缀 + 双接线 + 接线守卫）：
   目录口径 = `PROCUREMENT_` 前缀场景 Agent；条目 = 场景 + 工具能力说明 +
   **可运行性自证**（runnable = tool_name 非空 + config enabled）；
   **不暴露 handlerRef**（能力面非实现面，测试断言钉死）。
2. **纯函数 builder** `services/agent_catalog.buildProcurementCatalog`
   （agent_definition 行 + tool config → 目录条目）。
3. **场景 Agent 注册 ×3**（seed_agents.py SSOT，幂等）：
   PROCUREMENT_MATERIAL_SEARCH_AGENT / PROCUREMENT_SUPPLIER_SEARCH_AGENT /
   PROCUREMENT_PURCHASE_HISTORY_AGENT——M3-1/2 三个工具各自的运行 Agent；
   绑定（_AGENT_DEFAULT_BINDINGS）+ TOOL_SEEDS 元数据（seed_agent_tool_configs）
   三方同步，**种子一致性守卫**钉死（绑定工具必须同时存在于 TOOL_SEEDS 与
   BUILTIN_HANDLERS，缺一 = seed 出来不可运行但目录误导）。

## 测试（6 新用例）
builder ×4（能力摘要/占位不可运行/禁用工具不可运行/未知工具）+ 种子一致性 ×1 +
双接线 ×1。Agent 家族回归 **50 passed**；ruff 仅 B008 ×2（仓内路由惯例基线）。
