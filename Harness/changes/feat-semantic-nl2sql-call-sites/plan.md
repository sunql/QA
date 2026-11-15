# P3d 其余 NL2SQL 调用点 —— 执行计划（D29–D31）

> 上位：[`docs/update/1109/智能采购平台升级计划_日级排程_1109.md`](../../../docs/update/1109/智能采购平台升级计划_日级排程_1109.md) §9。
> 上游：[feat-semantic-agent-snapshot](../feat-semantic-agent-snapshot/plan.md)（P3c，D26–D28）·
> [feat-semantic-release-graph-mirror](../feat-semantic-release-graph-mirror/plan.md)（P3b）·
> [feat-semantic-registry-nl2sql](../feat-semantic-registry-nl2sql/plan.md)（P2）

| 项 | 值 |
|---|---|
| **变更** | P3d：`agent_tools_nl2sql` / `chat_recall`（及其余消费面调用点）接经 Registry |
| **分支** | `feat/semantic-nl2sql-call-sites`（worktree `/private/tmp/qa-graph-mirror` 续用），base = 合并 P3c 后的 `origin/main` |
| **迁移版本** | **无**（零 DDL：D29/D30 只加路由分支与闸门，不加列不加表） |
| **状态** | D29 完成（工具闸门 + 钉住穿透链 + 真实数据逐字节对等）；D30 完成（召回闸门整链切换 + 调用点清单 grep 复核）；D31 进行中（回归 + CI 对差 + PR，**G4**） |

---

## 1. G4 判据（本批的完成定义）

> **G4**：NL2SQL / RAG / 图谱 / Agent 四个消费面均可在**开关**下读发布版本，
> 且**随时可回落**；无一个消费面强制依赖 Registry。

| 消费面 | 开关（钉住来源） | 状态 |
|---|---|---|
| NL2SQL schema 渲染 | 会话钉住（P2 D16，`session_query_state.semantic_release_id`） | ✅ P2 已交（`renderSchemaWithRelease` 闸门） |
| RAG 向量召回过滤 | Milvus v2 集合 `release_id` 过滤（P3a） | ✅ P3a 已交 |
| 图谱按发布读 | `SEMANTIC_GRAPH_RELEASE_ID`（P3b D25） | ✅ P3b 已交 |
| **Agent 工具本体读** | 会话钉住（D29，L4 loop 透传）/ run 钉住（P3c D27 记录） | ✅ D29 本批 |
| **RAG 本体召回** | 会话钉住（D30，召回闸门） | ✅ D30 本批 |

**回落语义（全消费面统一）**：没钉住 / 钉不住 / 语义面故障 ⇒ 降级**直读治理表**
（与现状逐字一致），问答/工具/召回**绝不因语义面失败**。

## 2. 日次任务卡

| 日次 | 任务 | 产出 | 完成定义 |
|---|---|---|---|
| D29 | `agent_tools_nl2sql.py` 直读 `OntologyClass` 改经 Registry | `agent_tools_semantic_gate.py` + 3 个工具视图分支 + L4 钉住穿透链 + 测试 | 工具输出与切换前一致（真实数据逐字节对等：list_tables 712=712 字节、describe 8/8）；降级回落实测 |
| D30 | `chat_recall.py` 及其余调用点逐一改经 Registry（保留直读回落） | `semantic_recall_gate.py` + 召回整链切换 + **调用点清单**（grep 复核）+ 测试 | 清单内每处有判定与理由；召回视图与活表对等（真实数据 37 类 / 11 join 对全等） |
| D31 | 全 NL2SQL 定向回归 + CI 对差 + PR | 变更记录、PR | 净新增失败 0；**G4 通过** |

## 3. 调用点清单（D30，grep `select(OntologyClass|OntologyProperty|OntologyJoin)` 全量复核）

| # | 文件 | 判定 | 理由 |
|---|---|---|---|
| 1 | `agent_tools_nl2sql.py` | **已切**（D29） | Agent 消费面（G4 第 4 面）；钉住 = 会话（L4 loop 透传）/ run（P3c 记录） |
| 2 | `chat_recall.py` | **已切**（D30） | RAG 本体召回消费面（G4 第 2 面的本体侧）；钉住 = 会话；**整链切换**（全量类 + DIM + join 扩边同源 ⇒ 不出混合口径） |
| 3 | `ontology_service.py` | 保持直读 | 治理 CRUD + 图同步差分（**写侧**：写主图必须读主图） |
| 4 | `ontology_batch_service.py` | 保持直读 | 治理导入/批处理（写侧） |
| 5 | `semantic_compiler.py` | 保持直读 | 编译器 = 发布的**源头**（发布时读活本体 ⇒ 快照才有内容） |
| 6 | `kpi_contract_builder.py` / `kpi_sql_compiler.py` | 保持直读 | KPI 编译/合同（治理发布链路，与 5 同理） |
| 7 | `data_quality_rule_llm_service.py` / `data_quality_rule_generate_service.py` | 保持直读 | DQ 规则生成（治理面） |
| 8 | `wiki_link_service.py` | 保持直读 | admin 弹窗选择器（治理界面必须看活数据） |
| 9 | `ontology_dimension_service.py` | 保持直读 | 维度配置（治理面） |
| 10 | `ontology_join_health_service.py` | 保持直读 | join 健康检查（治理面） |
| 11 | `lineage_extractor.py` | 保持直读 | 血缘提取（治理面） |
| 12 | `field_display_resolver.py` | 保持直读 | 图表列显示名（不在 G4 四消费面内；登记 L-25） |
| 13 | `api/v1/ontology.py` | 保持直读 | 治理 REST（CRUD） |
| 14 | `api/v1/research.py` | 保持直读（L-24） | research 臂的检索源 `searchByKeyword` 无发布维度（与向量侧 R-6 同根：检索/向量物化另立卡） |
| 15 | `nl2sql_service.py` 及 nl2sql_* 家族 | **P2 已切** | `renderSchemaWithRelease` 闸门 = 唯一出口（P2 D12–D17） |

**grep 复核**：`grep -rn "select(OntologyClass)" app/`（2026-11-16 实测）除测试外全部命中上表，
无遗漏。

## 4. 遗留与风险

| ID | 风险 / 遗留 | 处置 |
|---|---|---|
| L-23 | **工具 list_joins 顺序差异**：快照分支按 manifest 顺序（合成 id），直读按 DB 自增 id——内容 100% 一致（13/13 边对等）、顺序不同。manifest 刻意不带自增 id（字节确定性）⇒ 顺序对齐**不可能**（要么改 manifest schema，要么改直读排序 = 破坏「切换前一致」） | 与 P2 D14「内容等价/顺序等价」判定同款：**如实登记，不改代码**；顺序对齐需 manifest 加 join 身份（另立卡） |
| L-24 | research 臂的检索源（`searchByKeyword` / 向量）无发布维度 | 与 P3a R-6 同根；检索/向量侧发布物化另立卡 |
| L-25 | 图表列显示名（field_display_resolver）读活本体 | 不在 G4 四消费面；图表显示名钉住另立卡 |
| R-23 | D29 重构中曾引入 `_describeTableDirect` 位置传参 bug（keyword-only 签名撞位置调用）——**单测没抓住**（假会话不覆盖 handler→直读体的接缝），**真实数据对等验证抓住了** | 真实数据验证是这类接缝 bug 的唯一防线（教训已登记 D29.md） |
| R-24 | 召回视图与 P2 渲染视图是两套对象（可变 vs frozen）——字段名逐字对齐是唯一契约 | 双向单测钉住（召回视图过 `_getClassLayer`/ODS 判定；渲染视图探 `__dict__`） |
