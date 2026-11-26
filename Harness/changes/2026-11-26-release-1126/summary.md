# 发版 1126 摘要：到货跟踪（P5）+ 场景配置化（P4）+ 待到货迁本体

- 日期：2026-11-26　分支：`feat/ontology-evolution @ c63f8eed`（PR #178，未合 main 即部署，与 1124-pending01 同先例）
- 镜像：backend `qa-system-backend:amd64-1126` / frontend `qa-system-frontend:amd64-1126`；**worker 与 backend 同镜像**
  - ⚠️ 2026-11-26 构建未完成（本机 Docker Hub 不可达，见 runbook §1 顶部横幅），Image ID / sha256 待回填
- 迁移：**0169 → 0171**（0170 建 `copilot_scenario` + 会话 `pinned_at`；0171 轮次 `data_stamp`），零数据回填，降级边界 `0169`
- runbook：同目录 `runbook.md`（Phase 0–8，thdeploy 固定值预填）

## 功能面

1. **到货跟踪场景（P5）**：按时间段/供应商/订单看到货；企业月度周期 24 日→次月 24 日（4 个预设 chip）；
   结果卡主指标「订单到货率」+ 量级加权率（参考）+ 三态 Tag + 供应商表 + 双 Tab 订单明细（可展开物料行）。
2. **场景配置化（P4）**：`copilot_scenario` 表 + 槽位 schema + 服务端渲染提问（冲突让位）；
   **新增场景 = 插一行配置**；会话置顶跨设备同步（pin 端点）。
3. **待到货迁本体**：表名/列名全部改走 `procurement_source_binding` / `procurement_property_binding`；
   **彻底移除 `datasourceId`**（工具 schema / 场景槽位 / 前端下拉）；THBI 硬规则（数据源由绑定唯一确定）。

## 关键修复（同批）

- **观测超限致卡片空白**：本期真实数据 `data` 167,038 字符 > 编排硬上限 50,000 ⇒ 观测被截断成不完整 JSON
  ⇒ 前端解析失败、卡片全空。改为装配期按运行时预算 40,000 裁剪（裁剪后 36,427 字符）。
- **chips 仅待到货可见**：抽通用 `CopilotSuggestedQuestions`，任何场景配了 chips 即显示。
- **`delivery_tracking_query` 不在只读白名单** ⇒ 场景 409；**启动链不 seed 编排 profile** ⇒ 新 Agent 409。
- **名称读取误加 `source_system='MDM'` 过滤**：实数据 `entity_mapping` 只有 ERP 行 ⇒ 供应商/物料名全 `None`。

## 部署要点（详见 runbook）

1. **Phase 0 硬门禁**：本体绑定审计（3 条 BASE 源绑定 + 12 个必需属性）——不过就停，
   否则「待到货」从能用变 502；
2. 三容器 recreate：backend →（seed）→ worker → frontend；
3. seed 顺序：`seed_agents` → `seed_procurement_agent_profiles` → `seed_copilot_scenarios` → `seed_procurement_bindings`；
4. 验收：`copilot_scenario` 3 行、0171、新路由 401/403、bundle 含「到货达成概览」、浏览器端到端 4 条。

## 遗留

- 采购菜单项与场景可见性仍按现有口径（非 admin 是否可见按 `required_role=NULL` 全员可见处理）；
- `DWD_PURCHASE_ORDER_LINE` 类待用户确认后另行删除（本批未动）。
