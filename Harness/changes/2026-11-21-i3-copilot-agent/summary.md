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
