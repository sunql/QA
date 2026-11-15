# P3c Agent 快照钉住 —— 执行计划（D26–D28）

> 上位：[`docs/update/1109/智能采购平台升级计划_日级排程_1109.md`](../../../docs/update/1109/智能采购平台升级计划_日级排程_1109.md) §8。
> 上游：[feat-semantic-release-graph-mirror](../feat-semantic-release-graph-mirror/plan.md)（P3b，D22–D25 已完成）·
> [feat-semantic-registry-nl2sql](../feat-semantic-registry-nl2sql/plan.md)（P2，D12–D17 已完成）

| 项 | 值 |
|---|---|
| **变更** | P3c Agent 快照钉住（AgentOrchestrationRun 挂 semantic_release_id） |
| **分支** | `feat/semantic-agent-snapshot`（worktree `/private/tmp/qa-graph-mirror` 续用），base = 合并 D24/D25 后的 `origin/main` |
| **迁移版本** | **0145**（手写、只加法：`agent_orchestration_run` 三列可空） |
| **关联变更** | 同上行 |
| **状态** | D26 完成（迁移 + ORM + 审计/读视图 + 单测 + 往返实证）；D27 完成（创建时钉住 + 运行期变更不影响 + 集成实证）；D28 进行中（回归 + PR） |

---

## 1. 背景（为什么做）

P2 给 NL2SQL 的会话层做了发布钉住（`session_query_state.semantic_release_id`，迁移 0144），
但 **Agent 编排 run** 还没有任何发布维度：一个 run 跑十分钟，中途发布切换，
同一 run 前后半截用的语义不同 ⇒ **重放不一致**，且事后无法回答「这个 run 用的是哪版语义」。

本批交付：run **创建时**把当前激活的语义发布**冻结**进三个可空列 ⇒ 运行期发布变更不影响该 run。

## 2. 日次任务卡

| 日次 | 任务 | 产出 | 完成定义 |
|---|---|---|---|
| D26 | `AgentOrchestrationRun` 新增可空 `semantic_release_id` + 快照冗余；迁移 + 单测 | 迁移 0145 + ORM 三列 + 审计/读视图增量 + 单测 | 旧 run 兼容（NULL 语义不变）；新 run 可写入；往返 upgrade→downgrade→upgrade 实证 |
| D27 | run 创建时固定当前激活发布；运行期发布变更不影响该 run；重放一致测试 | `resolveSemanticPin` + createRun 接线 + 测试 | 两个 run 跨发布版本各自语义稳定；语义面故障不挡创建 |
| D28 | 回归 + PR | 变更记录、PR | 净新增失败 0 |

## 3. 关键裁决

| # | 裁决 | 理由 |
|---|---|---|
| 1 | 钉住 = **全局**快照（`domain_code IS NULL` 的 ACTIVE） | `SemanticRegistry.getActive(None)` 的既有语义就是只认全局发布；Agent run 现在没有域上下文 ⇒ 领域级 ACTIVE 不在钉住范围（L-21，run 要域上下文另立卡） |
| 2 | 三列冗余（id + version + hash） | id 是指针；version/hash 是**自证**快照——发布行将来被删/被改，run 仍可读、可核对 |
| 3 | 语义面故障 ⇒ 降级 NULL + 告警，**不挡 run 创建** | 问答可用性优先；缺钉住只影响「重放同一发布」，不影响本轮能跑 |
| 4 | 迁移**手写 + 只加法**，注释与 ORM 逐字一致 | 0144 的既定惯例（迁移冻结在时间里不 import 业务代码）；一致性由单测双向钉住 |
| 5 | 审计与读 DTO 都暴露三要素 | 审计要能回答「这个 run 用的是哪版发布」；读视图要能展示（运维排障） |

## 4. 遗留与风险

| ID | 风险 / 遗留 | 处置 |
|---|---|---|
| L-21 | run **没有域上下文** ⇒ 只钉全局发布；领域级 ACTIVE 不在钉住范围 | run 要域上下文时另立卡（DTO/表都要加列） |
| L-22 | run 的**执行期**消费（NL2SQL 工具用钉住的那版）在 **P3d/D29** 接线 | 本批只交付「钉住被记录 + 展示」；工具消费是 P3d |
| R-21 | 钉住的三列与发布行的 id/version/hash 漂移 | `manifest_hash` 冗余列专门为核对存在；对账（P3d 或运维脚本）可比对 |
| R-22 | 迁移在生产是 DDL（0145） | 随常规发版（P0–P4 不部署生产；迁移在手写链上，downgrade 可回滚） |
