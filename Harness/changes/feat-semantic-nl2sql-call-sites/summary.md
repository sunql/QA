# 变更：feat-semantic-nl2sql-call-sites

- **日期**：2026-11-16
- **作者**：Claude
- **Phase**：P3d 其余 NL2SQL 调用点（D29–D31）
- **状态**：in-review（D29/D30 已交付并实证；D31 回归+PR 中）⇒ **P3 全阶段完成**
- **关联变更**：[feat-semantic-agent-snapshot](../feat-semantic-agent-snapshot/summary.md)（P3c，run 钉住）·
  [feat-semantic-release-graph-mirror](../feat-semantic-release-graph-mirror/summary.md)（P3b，图谱面）·
  [feat-semantic-registry-nl2sql](../feat-semantic-registry-nl2sql/summary.md)（P2，schema 渲染面）·
  [feat-semantic-registry-rag](../feat-semantic-registry-rag/summary.md)（P3a，向量面）
- **迁移版本**：无（**零 DDL**）
- **MEMORY**：[qa-system-p3d-nl2sql-call-sites](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-p3d-nl2sql-call-sites.md)

---

## 1. 需求

G4 收口：NL2SQL / RAG / 图谱 / **Agent 工具与本体召回**四个消费面读发布版本。
P2/P3a/P3b/P3c 各自交付后，剩两个**未接**的本体消费面：

- **L4 Agent Loop 的本体读工具**（`list_tables` / `describe_table` / `list_joins`）——
  会话钉住了发布，工具仍读活本体 ⇒ 同一 run 内两套口径；
- **chat 召回链**（`classifyAndRecall`：全量类 / DIM 补拉 / join 扩边）——同上。

**验收标准（G4）**：四消费面均可在开关下读发布版本、随时可回落、无一个强制依赖 Registry。

## 2. 设计评审

**候选 A：只切 DIM 补拉（最小面）** vs **候选 B：召回整链切换（采纳）**

| 维度 | A 只切 DIM | B 整链切换 |
|---|---|---|
| 口径一致性 | ❌ 混合口径（DIM 快照、其它活表）——同一问句两半两个版本 | ✅ 同一问句一个版本 |
| 实现面 | 小 | 中（三个读点 + 穿参） |
| 结论 | ❌ 静默破坏「按发布读」合同 | ✅ **采纳** |

**候选 Ⅱ：召回视图可变 vs 复用 P2 frozen 视图**

| 维度 | frozen（P2 `SnapshotClass`） | 可变 `RecallClass`（采纳） |
|---|---|---|
| `_recall_score` 回填 | ❌ `cls._recall_score = ...` 对 frozen 当场炸 | ✅ |
| 字段契约 | 与 ORM 对齐 | **同样逐字对齐**（层判定/ODS 过滤/排序一视同仁） |
| 结论 | ❌ | ✅（双向单测钉住字段契约） |

**工具侧共享渲染管线**：`SnapshotClass/Join` 字段名与 ORM **逐字对齐** ⇒
`_describeColumns` / `_renderJoinEdges` 直读与快照**共用同一函数**
⇒ 两条路径的输出格式不可能漂移（单一事实源）。

## 3. 数据模型变更

**PG 侧：无**（零 DDL）。图/向量侧：无。本批只新增**路由分支与闸门**。

## 4. 接口契约变更

| 契约 | 内容 |
|---|---|
| `loadToolOntology(session, *, pinnedReleaseId, registry=None)` | 工具闸门 ⇒ `ToolOntologyView`（全量复原）；`None` = 直读 |
| `loadRecallOntology(session, *, pinnedReleaseId, registry=None)` | 召回闸门 ⇒ `RecallOntology`（全量类 + join，**可变**视图）；`None` = 直读 |
| `dispatch_tool_call(tool_call, *, session, pinnedReleaseId=None)` | 工具分发透传钉住 id |
| `AgentRuntimeService.run_agent_loop(..., pinnedReleaseId=None)` | L4 loop 接受钉住 id（state 穿透到每次工具调用） |
| 降级语义（全闸门统一） | 那一版被删 / id 非法 / 完整性失败 / 畸形 / 意外异常 ⇒ `None` + warning 留痕 ⇒ 直读治理表 |

## 5. 实现要点

| 文件 | 职责 |
|---|---|
| `backend/app/services/agent_tools_semantic_gate.py` | 工具闸门（全量复原） |
| `backend/app/services/semantic_recall_gate.py` | 召回闸门（全量复原 + **可变**视图） |
| `backend/app/services/agent_tools_nl2sql.py` | 3 个 handler 快照分支 + 共享渲染管线（`_describeColumns` / `_renderJoinEdges`）+ dispatch 透传 |
| `backend/app/services/agent_runtime_service.py` | 钉住穿透链（run_agent_loop → state → dispatch）；agent_tools 函数内 import 提升到模块级 |
| `backend/app/services/chat_l4.py` | L4 入口读会话钉住并传进 loop；读取失败容错（不杀 L4）；拆三段回 <50 行 |
| `backend/app/services/chat_recall.py` | 召回整链切换（`classifyAndRecall` → `_selectRelevantClasses` / `_rankByLayer` / `_fallbackRecall` / `_expandByJoinNeighbors` 穿参） |

## 6. 测试

| 文件 | 用例 | 关键场景 |
|---|---|---|
| `test_agent_tools_semantic_gate.py` | 6 | 钉住/未钉住/被删/完整性/畸形/意外异常全部降级 |
| `test_agent_tools_nl2sql_semantic.py` | 16 | 视图分支渲染等价 + **穿透链**（dispatch→闸门、_dispatchSingleTool→dispatch） |
| `test_semantic_recall_gate.py` | 4 | 钉住/可变性/**字段契约**（层判定+ODS）/降级 |
| 既有 `test_classify_and_recall.py` | 176 | 全部 passed（新参数带默认 ⇒ 旧行为不变） |
| 既有 L4 套件 ×5 | — | 全绿（cold_start 先红后绿 ⇒ 钉住读取容错） |
| **合计** | **331 passed** | ruff 干净（agent_runtime_service 的 7 条为 HEAD 既有债，错误集与 base 相同） |

**TDD**：RED（模块 ImportError / 视图分支缺）→ GREEN → 重构（抽取共享渲染管线回 <50 行）。

## 7. 安全审查

本批不新增写路径、不新增端点、不引入凭据；风险面是「钉住读取失败把错误带进问答链路」——
已用统一降级口径封死（任何异常 ⇒ None ⇒ 直读 + warning 留痕；工具/召回绝不因语义面炸）。
自查结论：无 CRITICAL/HIGH 遗留。

**⚠️ 重构引入过 1 个真 bug 且单测没抓住**（R-23）：`_describeTableDirect` 抽取后位置
传参撞 keyword-only 签名——假会话不覆盖 handler→直读体接缝 ⇒ **真实数据对等验证**
（直读 vs 钉住逐字节比）当场抓出。教训：**抽取重构后必须跑真实数据对等验证**。

## 8. 部署验证

**零 DDL、不部署**：没钉住时所有行为与现状逐字一致（真实数据直读路径逐字节对等实证）；
钉住来源全部来自已有数据（`session_query_state.semantic_release_id`，P2 D16 已在建）。

**真实数据对等（dev 5433，发布 v-p2-d12）**

```
工具面（直读 vs 钉住，同一本体）：
  list_tables      逐字节一致（712 = 712 字节）
  describe_table   8/8 张表逐字节一致
  list_joins       内容 100% 一致（13/13 边集合相等）；顺序不同（L-23，如实登记）
召回面（钉住视图 vs 活表）：
  classes 类名集合 37 = 37；join 类名对 11 = 11；层判定 DIM 3 / DWD 14 / ODS 20
```

docker compose 栈（本地）：qa-postgres 5433（healthy）/ qa-pg-a1 5434（healthy）/ qa-neo4j-test 7688。

## 9. 关联

- **每日证据**：[`daily/D29.md`](daily/D29.md) · [`daily/D30.md`](daily/D30.md) · [`daily/D31.md`](daily/D31.md)
- **清单**：[`plan.md`](plan.md) §3 调用点清单（15 处逐一判定 + grep 复核）
- **Wiki**：[`Harness/wiki/architecture.md`](../../wiki/architecture.md)（语义发布章节）
- **Rules**：[`Harness/rules/变更记录强制规范.md`](../../rules/变更记录强制规范.md) · [`Harness/rules/开发流程规范.md`](../../rules/开发流程规范.md)
- **Memory**：`qa-system-p3d-nl2sql-call-sites`（+ `qa-system-graph-mirror-release` / `qa-system-p3a-rag-filter`）
- **关联变更（predecessor）**：feat-semantic-agent-snapshot（P3c）· feat-semantic-release-graph-mirror（P3b）· feat-semantic-registry-nl2sql（P2）· feat-semantic-registry-rag（P3a）

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全
- [x] 9 段都非空，无 TBD/TODO 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A/B × 2 组）
- [x] 第 3 段零 DDL（无迁移）
- [x] 第 7 段安全自查（无新写路径/端点；统一降级口径）
- [x] 第 8 段真实数据对等验证贴出
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] wiki 已更新；MEMORY 已添加
- [x] 真实数据验证脚本已跑通（工具逐字节 + 召回集合对等）
