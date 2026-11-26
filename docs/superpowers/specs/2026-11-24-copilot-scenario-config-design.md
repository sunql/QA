# 设计：采购 Copilot 场景配置化（P4）

- 日期：2026-11-24　状态：**待评审**
- 前置阅读：`Harness/changes/2026-11-24-release-1124-fix02/summary.md`（待到货场景现状）
- 关联：迁移 `0170`；置顶跨设备同步并入本批

---

## 0. 目标与非目标

**目标**：把场景从「硬编码在 ProcurementCopilotPage.tsx（1417 行、6 处 PENDING_DELIVERY 常量）」
变成「DB 里可配置的业务资产」——加场景、调 chips、改槽位 = 运营配置，不再前端发版。

**非目标**：
- 不做新工具（供应商风险的工具已就位）；
- 不改执行链路（run / worker / 复用 / 轨迹全部不动）；
- 不做通用可视化搭建器——`result_renderer` 只能**选择**渲染器，不能发明新渲染；
- 第一期不做场景管理 UI（先 SQL/seed 管理，管理页另批）。

---

## 1. 数据模型（迁移 0170）

### 1.1 `copilot_scenario`（场景定义）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BigInt PK | |
| `scenario_code` | String(64) UNIQUE | 如 `pending_delivery` / `supplier_risk` |
| `name` | String(100) | 卡片标题（i18n key 备选，第一期直接存文案——场景名由运营维护，非产品词条） |
| `description` | Text | 卡片两行说明 |
| `icon_code` | String(30) | 卡片图标（ICON_REGISTRY 里的 code，如 `car`） |
| `agent_code` | String(64) FK→agent_definition.agent_code | **钉死的 Agent** |
| `slot_schema` | JSONB | 槽位定义数组（见 §2） |
| `question_template` | Text | 提问模板（含 `{slot}` 占位与仲裁尾注） |
| `chips` | JSONB | 预设槽位组合数组 |
| `result_renderer` | String(30) | `pending_delivery` / `generic_table` / `none` |
| `required_permission` | String(64) NULL | 可见角色（NULL=全员；`admin`=仅 admin——数据源门控并入此处） |
| `sort_order` | Integer | 卡片墙排序 |
| `enabled` | Boolean | 停用 = 卡片隐藏、路由 404 语义 |

约束：`scenario_code` 唯一；`enabled=true` 的行 `sort_order` 不强制唯一。

### 1.2 会话置顶（跨设备同步，并入本批）

`procurement_copilot_conversation` 加一列：

| 列 | 类型 | 说明 |
|---|---|---|
| `pinned_at` | DateTime(timezone) NULL | 非 NULL = 置顶（值即置顶时间；排序用） |

- 前端 localStorage 的置顶逻辑**移除**，改调 `POST /conversations/{id}/pin`（toggle）；
- 迁移时把各用户 localStorage 现状**不做回填**（本机数据，无法批量取回——可接受：
  置顶是轻操作，重新点一次即可）。

### 1.3 可回滚性

DDL 建表 + 加可空列 ⇒ `downgrade` 直接 drop，**无数据回填** ⇒ 不需要 pg_dump 锚。

---

## 2. `slot_schema` 格式（JSONB 数组，每项一个槽位）

```json
[
  {"key": "monthsBack", "type": "number", "label": "时间窗（月）",
   "min": 1, "max": 36, "default": 12},
  {"key": "dateRange", "type": "dateRange", "label": "时间段",
   "overrides": "monthsBack"},
  {"key": "supplierKeys", "type": "entityMulti", "label": "供应商",
   "entityType": "SUPPLIER", "conflictKeywords": ["供应商", "厂家"]},
  {"key": "materialKeys", "type": "entityMulti", "label": "物料",
   "entityType": "MATERIAL", "conflictKeywords": ["物料", "材料"]},
  {"key": "siteKeys", "type": "dictMulti", "label": "基地",
   "optionsEndpoint": "pending-delivery/sites",
   "conflictKeywords": ["基地", "厂区", "工厂"]},
  {"key": "minOverdueDays", "type": "number", "label": "最低逾期天数",
   "min": 0, "default": 0}
]
```

- `type` 枚举：`number` / `dateRange` / `entityMulti`（entity-mapping 远程搜索多选）/
  `dictMulti`（字典端点多选）/ `text`；
- `conflictKeywords`：对话框文字命中 ⇒ 该槽位让位（**把现在的两层启发式从代码移进配置**；
  `entityMulti` 默认自带编码模式识别，可覆盖）；
- `overrides`：dateRange 给出时覆盖 monthsBack（对应现行为）；
- 选项来源两种：`entityType`（entity-mapping 搜索）/ `optionsEndpoint`（字典端点）。

**治理约束**：`slot_schema` 由 admin 维护，渲染前前端仍按白名单校验 `type`；
未知 `type` 的槽位**不渲染**（fail-closed，不裸渲染未知的输入控件）。

---

## 3. 后端改动

| 项 | 内容 |
|---|---|
| 迁移 0170 | 建 `copilot_scenario` + `procurement_copilot_conversation.pinned_at` |
| 模型 | `CopilotScenario` ORM + `pinned_at` 列 |
| 端点 | `GET /procurement/copilot/scenarios`（当前用户可见、enabled、排序）——卡片墙/工作区数据源；`POST /{code}/pin-toggle`（会话置顶，替代前端 localStorage）；admin 管理端点（CRUD）一期只做 list+get，写路径用 seed/SQL |
| 校验 | 场景保存时校验 `agent_code` 存在且 ACTIVE、profile enabled、`result_renderer` ∈ 白名单、`slot_schema.type` ∈ 白名单 |
| 提问模板渲染 | **后端渲染**：`POST /copilot/messages` 时若命中场景（`scenario_code` 随请求传入），由后端按模板 + 槽位实参组装最终消息（含冲突让位逻辑——启发式关键词也移进 slot_schema）。**前端只发槽位值 + 用户文字，不再自己拼消息** ⇒ 前端发版才需要改提问逻辑的问题彻底消除 |

**关键决策：冲突让位与提问组装移到后端。** 现在前端拼消息（含尾注仲裁）的做法，
把「槽位让位」的启发式写死在前端；配置化后这段逻辑必须跟模板走（模板在 DB）⇒
移到后端 `prepareCopilotTurn` 的场景分支里。前端只传结构化槽位 + 用户文字。

### 场景分支的请求契约变化

`CopilotMessageRequest` 增加可选字段：

```json
{"scenarioCode": "pending_delivery",
 "slots": {"monthsBack": 12, "supplierKeys": ["F109"], ...},
 "message": "用户补充文字（可空）"}
```

- 带 `scenarioCode` ⇒ 走场景分支：后端查配置 → 渲染模板（冲突让位）→ 组装 input → createRun；
- 不带 ⇒ 走既有自由问答分支（完全兼容）；
- 槽位值**以结构化 JSON 传递**，不再由前端拼消息文本 ⇒ 规划器拿到的就是干净参数
  （顺带消除「规划器在矛盾指令里选错」这类问题的大部分场景）。

---

## 4. 前端改动

| 项 | 内容 |
|---|---|
| 卡片墙 | `listCopilotScenarios()` 渲染（图标/标题/说明从配置来）；点击 → `setView(scenario.code)` |
| 工作区 | 槽位表单按 `slot_schema` 动态渲染（`SlotField` 组件分派 number/dateRange/entityMulti/dictMulti/text） |
| 提问 | 发送时只传 `{scenarioCode, slots, message}`，**组装移到后端** |
| 结果区 | `result_renderer === "pending_delivery"` ⇒ 现有 `PendingDeliveryResultCard`；`generic_table` ⇒ 通用表渲染器（新增，按 rows 列定义渲染）；`none` ⇒ 只显示 Markdown |
| 数据源门控 | 从硬编码 `isAdmin` 改为读槽位配置的 `requiredRole`（如 `admin`） |
| 置顶 | localStorage 移除，改调 pin 端点 |

**拆分**（现页面 1417 行，超 800 上限）：`ProcurementCopilotPage` 拆为
`CopilotHome`（卡片墙）/ `ScenarioWorkspace`（槽位+对话，按场景配置驱动）/
`FreeWorkspace`（既有自由问答）/ 既有侧栏组件。

---

## 5. 迁移与兼容

| 项 | 说明 |
|---|---|
| 既有待到货场景 | 作为**种子配置**插入 `copilot_scenario`（行为不变——回归基准：迁移前后同参数产出相同消息） |
| 自由问答 | 无场景行 ⇒ 走既有分支，零影响 |
| 旧前端 + 新后端 | 兼容：不带 `scenarioCode` 的请求走老路径；新端点是新增不影响 |
| 新前端 + 旧后端 | **不兼容**（新前端依赖 scenarios 端点与后端模板渲染）⇒ 前后端必须同批发布 |

---

## 6. 测试策略

| 层 | 用例 |
|---|---|
| 迁移 | 建表/回滚；种子配置插入 |
| 后端 | 模板渲染（含冲突让位——启发式关键词从配置读取）/ 场景分支请求契约 / Agent 校验（停用 Agent 的场景保存被拒）/ pinned 端点 |
| 前端 | 卡片墙读配置渲染 / 槽位表单按 schema 动态生成 / 未知 slot type 不渲染 / admin 门控按配置 / 回归：待到货种子配置产出与硬编码版相同的消息 |
| 契约 | **回归基准**：迁移前后「同参数 + 同文字」发给规划器的最终 input 逐字节一致 |

---

## 7. 工作量与顺序

| 阶段 | 内容 | 量 |
|---|---|---|
| 1 | 迁移 0170 + 模型 + 种子配置（待到货迁入）+ 场景端点 | 后端为主 |
| 2 | 后端模板渲染 + 场景分支请求契约 + 测试 | 后端 |
| 3 | 前端卡片墙/工作区改配置驱动 + 页面拆分（1417 行超限） | 前端 |
| 4 | 供应商风险速览作为验收场景（纯配置加出） | 配置 |
| 5 | 置顶跨设备同步（pin 端点 + 前端 localStorage 移除） | 小 |

---

## 8. 风险

| 风险 | 缓解 |
|---|---|
| 模板渲染移后端 ⇒ 消息文本变化 ⇒ 规划器行为漂移 | §6 回归基准：迁移前后最终 input 逐字节比对 |
| `slot_schema` 配错（未知 type/缺字段） | 保存时白名单校验 + 渲染时 fail-closed（未知槽位不渲染） |
| 页面重构回归 | 待到货种子配置作为行为基准 + 既有 53 个页面用例全部保留（适配新结构） |
