# 变更：I3 采购 Copilot 入口 + 合同检索工具（设计稿 §8.4 / §5.2）

- 日期：2026-11-21　分支：`main`（工作区）
- 起点：`design-compliance.md` 里 I3 = **75%**，缺 `copilot/messages`、
  `quotation_compare`、`contract_document_search`

## 一、先修的三处「拦路」缺陷（都是实测撞出来的，不是设计出来的）

| # | 现象 | 根因 | 处置 |
|---|---|---|---|
| 1 | `qa-agent-orchestration-worker` **重启 73 次** | 容器镜像是 11-19 的，`BUILTIN_HANDLERS` 里没有 M3-1 新增工具 ⇒ warmup 期 `ValueError` | 重建镜像（与 backend 同 Dockerfile） |
| 2 | 3 个采购场景 Agent **不在库里**；在库的也没有启用的编排 profile | M3-3 的 seed 没在新库跑过；**profile 从来没种过** | `seed_agents` 重跑 + 新增 `scripts/seed_procurement_agent_profiles.py` |
| 3 | profile 绑定工具直接 `ValidationError`：`material_search` 不在只读白名单 / `input schema root type must be 'object'` | 编排只读白名单是**另一份**清单（`READ_ONLY_HANDLER_REFS`），M3-1 没登记；工具 seed 的 `input_schema` 全是 `{}` | 白名单 +5（逐个评审只读性）；seed 补 schema；**`_needsUpdate` 增加 schema 比较**（否则老行永远修不好，seed 还报「0 changed」） |

## 二、I3-a：`POST /api/v1/procurement/copilot/messages`

- **不新造执行链**：入口 = 校验 `PROCUREMENT_` 前缀 → `AgentOrchestrationRunService.createRun`
  （ACTIVE / profile / model_config / 工具快照 / 预算 / 语义钉住**全部原样复用**）。
- 两种返回：`mode=task` → **202** `{runId, status, stepsUrl}`；
  `mode=stream` → **SSE** `copilot_run` → `copilot_step`* → `copilot_done`。
- **创建先于流**：404/409/422 必须以 HTTP 状态码返回，不能变成 SSE 里的一条 error。
- 轮询用 `ownSession()` 自开会话（不复用请求会话）；`CopilotStreamOptions`
  是依赖注入点（测试/运维可调 `pollInterval` / `maxWaitSeconds`）。
- 开关：与 `/agent-runs` 同开同关（`ENABLE_AGENT_ORCHESTRATION`）⇒ 关掉时整个面 404。

## 三、I3-b：`contract_document_search`

- 复用 `searchProcurementDocuments`（RAG + 密级闸 + catalog 核验），
  `documentTypes=["CONTRACT"]` 收窄；密级闸 fail-closed。
- 新增场景 Agent `PROCUREMENT_CONTRACT_SEARCH_AGENT` + profile。
- **顺手修的真正 bug**：编排路径的 `AgentToolContext` 只有 `actor`（run.created_by），
  没有 `user` ⇒ 文档类工具永远「缺少用户身份」。新增
  `dependencies.buildCurrentUserForUserId()`，工具侧按 actor 还原身份；
  查不到仍 fail-closed。

## 四、顺手修的两个存量 bug

1. **`findSuppliersOfMaterial` 查已废弃 label `Material`**（Phase 4.4 起是
   `ItemMaster`，且 `Material` 已不在白名单）⇒ `supplier_search` 的
   materialKey 分支**每次都** `TOOL_EXECUTION_FAILED`。已改 label + 回归用例。
2. **`_needsUpdate` 不比较 `input_schema`** ⇒ 库里遗留 `{}` 永远不会被 seed 修复。

## 五、真实验证（dev 栈，真 PG/Neo4j/Milvus + 真 Worker + 本地 LLM）

| 用例 | 结果 |
|---|---|
| 物料检索 Copilot（task 模式） | `SUCCEEDED`，2 轮 / 1 工具 / 4337 tokens，回答 16 个真实物料（ERP 源） |
| 供应商检索（SSE） | `copilot_run → copilot_step×2 → copilot_done`；label 修复后不再 `TOOL_EXECUTION_FAILED`，如实回答「图中无登记供应商」 |
| 合同检索（SSE） | 工具真执行（不再「缺少用户身份」），如实回答「未命中 CONTRACT 文档」（语料未导入） |
| 目录 `GET /procurement/agents/catalog` | 4 个场景 Agent `runnable=true` |

**回归**：受影响家族 **123 passed**（route-auth 守卫 / 生产鉴权守卫 / agent-runs create /
orchestration acceptance / config-api / seed_agents / copilot API / 工具与白名单单测）。

## 六、仍未做（I3 剩余）

- `quotation_compare`（归一化比价矩阵）：需要币种/税率/单位归一规则 + THBI Oracle 实数据。
- 设计 §5.2 的其余场景模板（SOURCING / QUOTATION_ANALYST / FULFILLMENT_MONITOR）：
  等业务定稿。
- 前端 Copilot 页对接 SSE（M4 面）。

---

## 七、第二轮（同日）：SOURCING 场景 / quotation_compare / 前端 Copilot 页

### 7.1 `PROCUREMENT_SOURCING`（多工具场景）
- 注册 Agent + profile（6 工具：material_search / supplier_search / supplier_360 /
  supplier_risk / graph_traverse / purchase_history_query）。
- **策略必须取工具组并集**：`_policiesFor` 原来只按**主工具**派生 ⇒ 运行期 ACL 对
  DWD（purchase_history）与 FEATURE（supplier_360/risk）一律 PERMISSION_DENIED。
  新增 `_AGENT_EXTRA_BINDINGS` + `_unionPolicies`（同键去重、确定性排序）。
- 编排绑定前置：`supplier_360` / `supplier_risk` / `graph_traverse` 的
  `input_schema` 补成合法 object schema（原来全是 `{}`）。
- 实测：SOURCING 跑通「检索物料 → 多候选 ⇒ ASK_USER 追问」，答案里明确让用户选规格
  （不是失败，是设计要求）。

### 7.2 `quotation_compare`（确定性比价，零 LLM）
- 新模块 `services/procurement_quotation_compare`：CNY / 未税 / 同物料同年度横向比价；
  相对中位价 20%（异常低价）与 30%（偏高）阈值；全程 Decimal；非法价格**不猜成 0**
  （0 会变成「最便宜」）。
- 工具 handler + 只读 SQL builder（`IN` 列表去重、上限 20 个物料）+ 白名单/seed/schema。
- `PROCUREMENT_QUOTATION_ANALYST` 场景 Agent + profile（quotation_query + quotation_compare）。
- **仍待 Oracle**：真实数据跑通/验收证据；口径 5 项默认（币种 CNY / 未税 / 同物料同单位 /
  同年度横向 / 20%·30%）已在模块文档写明，业务改口径只改常量与用例。

### 7.3 前端 Copilot 页
- `POST /procurement/copilot/messages` 的 SSE 客户端：自研解析（EventSource 不支持 POST
  + Authorization）；跨 chunk 拼接、坏帧不炸整条流。
- 页面：场景下拉（只列 runnable）/ 模型下拉（必选，后端不改派）/ 输入区 / 对话卡
  （轨迹步骤 + 最终答案 + PAUSED 追问高亮 + 轨迹链接 + tokens）。
- 路由 `/procurement/copilot` + 菜单 seed（icon 用已注册的 `message`）+ zh/en 文案。
- 测试：SSE 解析 7 例、页面 3 例（含 PAUSED 渲染成追问、错误展示 detail）。

### 7.4 顺手修的存量红基线
- `test_seed_menu_config` 的计数与路径清单一度停在 53 行 / 45 项（实际已 64 项）⇒
  改为**从 `SECTIONS`/`ITEMS` 推导**并补齐 11 个早就在 seed 里却没登记的路径。

### 7.5 反馈修复：supplier_search 支持**物料名**（2026-11-21）
- 实测反馈：用户问「FE-5顶丝M8×1×15有哪些供应商」⇒ 工具按 SUPPLIER 检索 0 命中
  ⇒ 把缺口甩回用户（要物料编码），而该物料名可解析到编码 `504014710001`、
  图里有 4 家在册供应商。
- 修法：`q` 按供应商检索为空时，把关键词当**物料名**解析（MATERIAL）+ 走
  `SUPPLIES` 反查；`via=material_name`，并在数据里带 `materials[]`。
- 三种结论**互不混淆**：有供应商 / 物料在但无在册供应商 / 物料也搜不到。
- 测试：3 例（含用户原始问法）；后端+worker 重建后真机复现该问题已通。

