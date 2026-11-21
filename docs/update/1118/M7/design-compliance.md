# 对《智能采购平台详细设计方案 V0.3（1109）》的完成度对照

> 核对时间：2026-11-21 ｜ 基线 commit `0811ae43`（main）｜ 迁移 head **0161**
> 判定口径：✅ 完成 ／ ◐ 部分（列缺口）／ ✗ 未做 ／ ⤳ 走已批准偏差（D1–D4）

## 总览（§12 迭代）

| 迭代 | 设计内容 | 完成度 | 说明 |
|---|---|---|---|
| **I0** 基线与接口冻结 | 冻结 commit/head/compose、OpenAPI 快照 | ✅ **100%** | 生产 `c959b77f`+runbook；A1 本地栈同构；301 路由冒烟 |
| **I1** 本体语义发布 | source binding、release/check/manifest、registry | ✅ **100%**（⤳ D1/D2） | 2 张绑定表(0154) + check 落表(0155) + manifest v2(0156) + 域编排/回滚(0157) + 对象核对(0158)；9 绑定已确认、域首发布 ACTIVE |
| **I2** 知识服务 | 统一 Evidence、图谱、RAG、SQL 只读 | ◐ **90%** | 四端点全做（query/entities/graph/documents）+ Evidence 契约补全；缺：Q04 制度文档 0 份（内容侧） |
| **I3** Copilot/只读 Agent | 场景目录、采购工具、profile、轨迹 | ◐ **90%**（2026-11-21 更新） | 目录 API ✅、工具 **6/7**、**4 个场景 Agent 可运行**（含专业 profile）、`copilot/messages` ✅（task 引用 / SSE）、`contract_document_search` ✅、轨迹复用 agent_runs ✅；缺：`quotation_compare`（待业务归一规则 + Oracle）、§5.2 其余场景模板、前端 Copilot 页对接 |
| **I4** 询价草稿/工作流 | 命令、状态机、任务、审批、Outbox、ERP 适配 | ◐ **70%**（⤳ D-4） | 状态机 10 态 ✅、审批 ✅、幂等 ✅、Outbox+消费者 ✅、端口+模拟器 ✅；缺：RFQ 走 business_command/workflow_instance（现为 RFQ 原生决策表）、真实适配器、待办页 |
| **I5** 扩展场景 | 比价、合同审查、履约监控 | ✗ **~10%** | 比价：quotation_query 工具在（无评分规则）；合同审查/履约监控未做（依赖 D3 规则版本化 M6） |

**首期（I0–I4）加权：约 85%**；含 I5 的全量：约 70%。

## 逐章要点

### §2 目标模块与代码落点（12 项）
建议的 `services/procurement/*` 独立子包**未采用**：实际按**既有分层**落到
`services/{knowledge,procurement_*}`（D1 复用原则）。逐项映射：

| 设计模块 | 实际落点 | 状态 |
|---|---|---|
| 本体扩展 / 语义编译器 / 注册表 | `semantic_release_service` + `semantic_compiler_bindings` + `semantic_registry_service` | ⤳ 复用 |
| 知识门面 | `services/knowledge/facade.py` + `api/v1/procurement_knowledge.py` | ✅ |
| 图查询适配 | `graph_traversal_service` + `api/v1/procurement_knowledge_graph.py` | ✅ |
| 文档检索适配 | `knowledge/document_search.py` + `api/v1/procurement_knowledge_documents.py` | ✅ |
| 采购工具 | `services/agent_tools_procurement.py`（5 个工具） | ◐ 5/7 |
| Agent 场景注册 | `scripts/seed_agents.py` + `services/agent_catalog.py` + `api/v1/procurement_agents.py` | ✅ |
| 业务命令 / 工作流 | P5 `workflow_service` + `business_command`（复用） | ⤳ 复用（RFQ 未走） |
| 同步服务 | 未做（M6 真实对接后） | ✗ |
| 前端 | 4 页：询价/绑定/知识/场景目录 + 菜单 + 权限矩阵 | ✅ |

### §3 本体驱动机制
3.1 六层模型 ⤳ 复用现有 ontology_*（无独立采购层）；3.2 十对象 ✅（M1-1.4 核对）；
3.3 映射契约 ⤳ **2 张表**（D2 砍 key_binding）；3.4 发布链 ✅（validate/compile 合并进
createDraft，**无独立 /compile 端点**，偏差）；3.5 版本表 ⤳ 复用 semantic_release；
3.6 运行时驱动 ✅（manifest 注入 NL2SQL/Agent）。

### §4 知识服务
4.1 Facade ✅（4 通道 GRAPH/SQL/RAG/METRIC）；4.2 Evidence 契约 ✅ **全字段补齐**
（evidenceId 内容确定/confidence/security/sourceRef/observedAt/validAt/ruleVersion）；
4.3 混合检索策略 ✅（实体优先图、制度优先 RAG、指标优先 SQL）。

### §5 Agent 架构
5.1 复用边界 ✅；5.2 场景（设计 5 个）→ 实际 3 个可运行场景 Agent + COPILOT 占位
（**缺 SOURCING/QUOTATION_ANALYST/CONTRACT_REVIEW/FULFILLMENT_MONITOR 四个场景模板**）；
5.3 执行状态 ✅（复用 agent_runs）；5.4 工具契约 ✅ 三参 handler + DB 元数据分工；
5.5 Planner 约束 ✅（迭代/工具数/预算既有）；5.6 run↔版本绑定 ✗
（`procurement_agent_run_context` 未建）。

### §6 业务流程与受控写
6.1 职责边界 ✅；6.2 状态机 ✅（10 态，D-4 下 SENDING 由 M6-1 打开）；
6.3 命令模型 ◐（**RFQ 用原生决策表而非 business_command**；Outbox 已接、消费者已建）；
6.4 流程表 ⤳ 复用 P5 workflow_*；6.5 引擎选型 ✅（轻量状态机，未引 BPM）；
6.6 权限四级 ✅（后端角色闸 + 前端矩阵，PROHIBITED_AUTO 协议层不可达）。

### §7 数据库
7.1 复用清单 ✅；7.2 新增表 **5 张**（procurement_*）+ 复用 8 张；
7.3 DDL ⤳ 按 D1/D2 裁减（release→semantic_release、workflow→P5、key_binding 砍）；
7.4 约束 ✅（JSONB 校验、幂等键、Outbox 至少一次）。

### §8 API 契约
8.2 发布 6 端点 → 实际 5 + rollback/archive（**无 /compile**）⤳；8.3 知识 4 端点 ✅；
8.4 目录 ✅ / **copilot/messages ✗**；8.5 RFQ 3 端点 ✅ 但**命名与拆分不同**
（`/drafts/{id}/approve` vs `/commands/{id}/approve`；`/send` vs `confirm-execution`）；
8.6 DTO ✅ 字段对齐（camelCase/Idempotency-Key）。

### §9–§11 时序/错误/非功能
9.x 时序 ✅（发布/查询/Agent/询价四条全通）；§10 幂等与一致性 ✅（同键同 payload 重放、
异 payload 409、Outbox 重试）；§11 权限/审计/可追溯 ✅，**性能 P95 未测**。

### §13 开发前待确认（7 项）
✅ **全部闭合**（生产 commit/compose 已冻结、源系统只读实测、审批矩阵已回收、
合同系统=文档目录、试点范围=采购全域、询价=仅草稿）。

## 刻意偏差汇总（均有决策背书）
| 偏差 | 决策 | 影响 |
|---|---|---|
| 新表按 D1 复用（release/workflow/rule） | D1 | 少建 8 张表；语义等价 |
| 绑定表 2 张（砍 key_binding） | D2 | 统一键走 entity_mapping |
| 规则版本化扩 feature_rule | D3 | 未做（M6） |
| 询价 DRAFT-only，不接 ERP 写 | D-4 | 发送走端口+模拟器 |
| `services/procurement/` 子包 | D1 复用原则 | 落点按既有分层 |
| Evidence 嵌套 source→sourceRef | 实现约束 | `source:str` 已占用 |

## 未完成清单（按阻塞性质）
1. **外部输入**：真实 ERP/SRM 适配器、pilot 问题集、P95 压测
2. **内容侧**：制度/合同文档导入（Q04）
3. **工程（可做）**：`copilot/messages` 入口、SOURCING 等 4 个场景模板、
   `procurement_agent_run_context`、`quotation_compare`、RFQ 走 business_command
