# 变更：feat-semantic-agent-snapshot

- **日期**：2026-11-16
- **Phase**：P3c Agent 快照钉住（D26–D28）
- **状态**：**已合入 main**（PR #134，squash `0cb73dff`）⇒ 不部署（迁移 0145 随常规发版）
- **关联变更**：[feat-semantic-registry-nl2sql](../feat-semantic-registry-nl2sql/summary.md)（P2，会话层钉住）·
  [feat-semantic-nl2sql-call-sites](../feat-semantic-nl2sql-call-sites/summary.md)（P3d，**消费** run 的钉住）·
  [feat-semantic-release-graph-mirror](../feat-semantic-release-graph-mirror/summary.md)（P3b，图谱面）
- **迁移版本**：**0145**（手写、只加法：`agent_orchestration_run` 三列可空）
- **MEMORY**：消费侧见 [qa-system-p3d-nl2sql-call-sites](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-p3d-nl2sql-call-sites.md)（本批本身无独立记忆）

---

## 1. 需求

P2 给 **NL2SQL 会话层**钉住了发布（`session_query_state.semantic_release_id`，迁移 0144），
但 **Agent 编排 run** 没有任何发布维度：一个 run 跑十分钟，中途发布切换，
同一 run 前半截与后半截用的语义不同 ⇒ **重放不一致**，且事后无法回答
「这个 run 用的是哪版语义」。

交付：run **创建时**把当前激活的语义发布冻结进三列（id + version + hash）⇒
运行期发布变更**不影响**该 run，事后可核对。

## 2. 关键裁决

| # | 裁决 | 理由 |
|---|---|---|
| 1 | 钉住 = **全局**快照（`domain_code IS NULL` 的 ACTIVE） | `SemanticRegistry.getActive(None)` 的既有语义就是只认全局发布；run 无域上下文（L-21） |
| 2 | **三列冗余**（id + version + manifest_hash） | id 是指针；version/hash 是**自证**快照——发布行将来被删/被改，run 仍可读、可核对 |
| 3 | 语义面故障 ⇒ **降级 NULL + WARNING，不挡 run 创建** | 问答可用性优先；缺钉住只影响「重放同一发布」 |
| 4 | 迁移**手写 + 只加法**，注释与 ORM **逐字一致** | 0144 惯例（迁移冻结在时间里，不 import 业务代码）；一致性由单测**双向**钉住 |
| 5 | 审计 DTO 与读 DTO 都暴露三要素 | 审计要能回答「用的哪版」；读视图供运维排障 |

## 3. 交付

| 面 | 内容 |
|---|---|
| 迁移 | `0145_agent_run_semantic_release.py`：`semantic_release_id` INTEGER / `semantic_release_version` VARCHAR(64) / `semantic_manifest_hash` VARCHAR(64)，全可空 ⇒ 老 run 语义不变、零回填 |
| ORM / DTO | `agent_orchestration_models.py` 三列（注释同迁移字面量）；`AgentRunRead` 增 `semanticReleaseId` / `semanticReleaseVersion` / `semanticManifestHash` |
| 读视图 / 审计 | `agent_orchestration_run_views.py` 的 `runToRead` 与 `runToAuditDict` 都映射三要素 |
| 钉住 | `agent_orchestration_run_service.py`：`SemanticReleasePin`（frozen）+ `resolveSemanticPin`（有 ACTIVE ⇒ 钉；无 ⇒ `None`；异常 ⇒ 降级 `None` + WARNING）+ `createRun` 接线；`semanticRegistry` 构造注入（生产走延迟 import 单例，防环） |

## 4. 测试（RED → GREEN，全部实跑）

- **新增 unit 文件** `test_agent_run_semantic_pin.py`（9 例）：三列赋值 / 不钉住全 NULL（旧语义兼容）/
  **ORM 注释与迁移字面量逐字一致（双向）** / 审计含三要素 / 有 ACTIVE 解析 / 无发布 `None` /
  语义面故障降级不挡创建 / **重放一致**（跨发布版本两个 run 各自钉住）。
- **集成**（加在**已有**文件 `test_agent_runs_api_create.py`，不新建集成文件——不动 CI 分片轮转）：
  有 ACTIVE ⇒ 创建的 run 三列冻结且与发布行一致；发布改 ARCHIVED 后 detail 的钉住快照**原样**；
  既有用例补「无发布 ⇒ 三列 NULL」。
- **迁移往返实证**（真实 PG，`qa_metadata_test`@5434）：0144 → upgrade（三列 + 注释逐字）→ downgrade（三列全删）
  → upgrade（逐列一致）= `ROUNDTRIP OK`。
- 集成用例两个真实坑（已修，登记在 `daily/D27.md`）：JSONB 列必须传 dict（否则二次编码撞 CHECK）；
  `manifest_hash` 必须按 `canonicalManifestJson` 口径算；测试造的发布行必须 commit（createRun 走独立连接）。

## 5. 影响面与遗留

- **零 DDL 运行时**（0145 只在迁移链上）、**零新增端点**、老 `agent_orchestration_run` 行不需要回填。
- 迁移链：0144 → **0145** → 0146/0147/0148（A1 发现底座建在 0145 之上，已合）。
- **L-21**：run 无域上下文 ⇒ 只钉全局发布；domain 级 ACTIVE 不在范围（另立卡）。
- **L-22（**未完全闭合，如实登记**）**：P3d/D29 把工具本体读接进了闸门
  （`agent_tools_semantic_gate.loadToolOntology`，`55fa8188`），但**透传的是会话钉住**
  （`session_query_state.semantic_release_id` ⇒ `agent_runtime_service` 的 `pinnedReleaseId`），
  **不是** run 的 `semantic_release_id`。run 三列目前只「被记录 + 被展示」，**执行期未被消费**：
  同一会话内若跨 run 换过发布，工具读的仍是会话那版。⇒ 要把 run 钉住接进 loop
  需要 run → loop 的 id 透传（另立卡；`agent_runtime_service.py` 的 `pinnedReleaseId` 注释里
  现在写的是「会话钉住」）。
- **R-21**：三列与发布行漂移 ⇒ 靠 `manifest_hash` 冗余列对账（未做自动对账，运维脚本级）。

## 6. 回滚

迁移 0145 `downgrade` 删三列（已验证往返）；代码回滚 = revert `0cb73dff`
⇒ run 创建不再写三列、读 DTO 不再暴露（列若已删，ORM 回退一致）。
P3d 的消费侧（会话钉住）对 `pinnedReleaseId is None` 回落直读 ⇒ 与 0145 无关，
迁移回滚不影响工具读路径。
