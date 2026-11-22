# 对《智能采购平台详细设计方案 V0.3（1109）》的完成度对照

> 核对时间：**2026-11-22** ｜ 基线 `main@9662aa6b`（#174）｜ 迁移 head **0165**
> 判定口径：✅ 完成 ／ ◐ 部分（列缺口）／ ✗ 未做 ／ ⤳ 走已批准偏差（D1–D4）
> 上一版：2026-11-21（head 0161）——本版按当日实跑结果更新

## 总览（§12 迭代）

| 迭代 | 设计内容 | 完成度 | 说明 |
|---|---|---|---|
| **I0** 基线与接口冻结 | 冻结 commit/head/compose、OpenAPI 快照 | ✅ **100%** | 生产 `c959b77f`+runbook；A1 本地栈同构；**319 路由**冒烟（采购域 15） |
| **I1** 本体语义发布 | source binding、release/check/manifest、registry | ✅ **100%**（⤳ D1/D2） | 2 张绑定表(0154) + check 落表(0155) + manifest v2(0156) + 域编排/回滚(0157) + 对象核对(0158) |
| **I2** 知识服务 | 统一 Evidence、图谱、RAG、SQL 只读 | ◐ **95%** | 四端点 ✅；Evidence 契约全字段 + **`relatedEntities` 回填**；文档**质量人工开关**(0165)；缺：**CONTRACT 类型语料 0 份**（内容侧，Q04 仍受阻） |
| **I3** Copilot/只读 Agent | 场景目录、采购工具、profile、轨迹 | ✅ **98%** | 目录 7 个场景（**6 可运行**）、**采购工具 8/8**、`copilot/messages`（task+SSE）、多轮会话、合规核对工具；缺：FULFILLMENT_MONITOR（需 4 个新工具） |
| **I4** 询价草稿/工作流 | 命令、状态机、任务、审批、Outbox、ERP 适配 | ◐ **75%**（⤳ D-4） | 10 态状态机 + 审批 + 幂等 + Outbox 消费者 + **发送端口 SENDING**（M6-1）；缺：RFQ 走 business_command/workflow_instance、**真实 ERP/SRM 适配器** |
| **I5** 扩展场景 | 比价、合同审查、履约监控 | ◐ **35%** | **比价：代码 + 真数据验收完成**（Oracle 2026 年度 50,599 行实跑）；合同审查：文档检索 ✅ + `policy_compliance_check` ✅，缺条款抽取/规则版本化；履约监控 ✗ |

**首期（I0–I4）加权：约 94%**；含 I5 的全量：约 78%。

---

## I3 明细（本批变化最大）

| 项 | 现状 | 证据 |
|---|---|---|
| 场景目录 API | ✅ | `GET /procurement/agents/catalog`：7 条，6 条 `runnable=true` |
| 场景 Agent | ✅ **6 个可运行**（+COPILOT 占位） | SOURCING / QUOTATION_ANALYST / CONTRACT_SEARCH / MATERIAL_SEARCH / SUPPLIER_SEARCH / PURCHASE_HISTORY |
| 采购工具 | ✅ **8 个已启用** | material_search、supplier_search、purchase_history_query、quotation_query、quotation_compare、procurement_policy_search、contract_document_search、policy_compliance_check |
| Copilot 入口 | ✅ | `POST /procurement/copilot/messages`（task 引用 + SSE）+ **多轮会话**（追问续跑 / 有界上下文 / 会话绑定） |
| 轨迹 | ✅ | 复用 `agent_runs` / `/steps`；前端 Copilot 页可看 |
| 比价真数据 | ✅ | `scripts/probe_quotation_compare.py`：2026 年度 50,599 行 / 13,154 物料 / 253 供应商；修正了**含税与不含税混比**、**完全重复行**两个口径错误 |
| 制度合规核对 | ✅ | `policy_compliance_check`：把结论拿去比制度，输出「数据 + 依据 + 是否一致」 |

## 设计稿之外新增的能力（本批实现，供评审确认是否纳入基线）

设计稿 V0.3 未描述「文档原文阅览」，但实测业务必需，已在 `analysis/wiki-document-sharing` 交付：

| 能力 | 说明 |
|---|---|
| 原文阅览 | `GET /documents/{id}/source`（原文字节 + 密级闸 + 中文名 RFC 5987） |
| 通用预览 | `GET /documents/{id}/preview`：PDF 原件直出；Office 走 **LibreOffice→PDF**（保真）；md/html/txt 服务端渲染（消毒 + CSP） |
| 再次向量化 | `POST /documents/{id}/vectorize`：读回**已存源文件**再向量化（wiki 导入文档由此可被检索） |
| 文档关联 | 支持关联到**本体类/指标/属性**（0164 去 FK + 唯一索引）；可修改；证据回填 `relatedEntities` |
| AI 上下文开关 | `ai_context_enabled`（0165，**人工**）：关掉的文档不参与检索/注入 |
| AI 文档依据 | AIChat 答案阶段注入（`CHAT_DOC_CONTEXT`，默认关）+ SQL 计划阶段注入口径（`CHAT_DOC_CONTEXT_SQL`，独立开关） |

## 逐章要点（较上版的变化）

### §4 知识服务
4.1 Facade ✅（4 通道）；4.2 Evidence 契约 ✅ 全字段 + **`relatedEntities`**；
4.3 混合检索 ✅；**新增**：文档质量人工开关（决定是否参与检索）。

### §5 Agent 架构
5.1 复用边界 ✅；5.2 场景 ✅ **6 个可运行**（上版 3 个）；5.3 执行状态 ✅；
5.4 工具契约 ✅（8 个采购工具全部过只读白名单 + 合法 input_schema）；
5.5 Planner 约束 ✅；5.6 run↔版本绑定 ✗（`procurement_agent_run_context` 未建，⤳ D2）。

### §6 业务流程与受控写
6.1 ✅；6.2 ✅（10 态；SENDING 由 M6-1 打开）；6.3 ◐（RFQ 原生决策表，未走 business_command）；
6.4 ⤳ 复用 P5；6.5 ✅；6.6 ✅（四级权限，PROHIBITED_AUTO 代码层不可达）。

### §7 数据库
7.2 新增 **7 张 `procurement_*`**：source_binding / property_binding / rfq_draft / rfq_decision /
copilot_conversation / copilot_turn / object_reconciliation_journal；其余 ⤳ 复用。
7.4 约束 ✅（唯一索引、CHECK、幂等键、Outbox 至少一次）。

### §8 API 契约
8.3 知识 4 端点 ✅；8.4 目录 ✅ + **`copilot/messages` ✅**（本版补上）；
8.5 RFQ ✅（命名仍是 `/drafts/{id}/approve`、`/send`，与设计 `/commands/{id}/approve` 不同，⤳ 已在偏差表）。

### §11 非功能
**性能 P95：dev 站已测达标**（实体 0.362s / 图 1 跳 0.068s / 2 跳 0.094s / RAG 0.087s /
文档 0.060s，目标 1/2/3s）；⚠️ **生产量级未复测**，且实体查询走 35 万行全表扫
（生产有超 1s 风险，建议加 trigram 索引）。

## 刻意偏差汇总（均有决策背书，无新增）

| 偏差 | 决策 | 影响 |
|---|---|---|
| 新表按 D1 复用（release/workflow/rule） | D1 | 少建 8 张表 |
| 绑定表 2 张（砍 key_binding） | D2 | 统一键走 entity_mapping |
| 规则版本化扩 feature_rule | D3 | 未做 |
| 询价 DRAFT-only，不接 ERP 写 | D-4 | 发送走端口+模拟器 |
| Evidence 嵌套 source→sourceRef | 实现约束 | `source:str` 已占用 |
| 文档能力（原文/预览/向量化/AI 开关） | **新增，需评审** | 设计稿未覆盖，业务必需 |

## 未完成清单（按阻塞性质）

1. **外部输入**：真实 ERP/SRM 适配器（契约已冻结，等账号）；pilot 问题集 P-01…P-14；生产量级 P95 复测
2. **内容侧**：**制度/合同语料**（`document_catalog` 8 份，**CONTRACT 类型 0 份** ⇒ Q04 与合同审查受阻）
3. **工程（可做）**：
   - FULFILLMENT_MONITOR（需 po_status / receipt / quality / invoice 4 个工具）
   - 合同条款抽取（`contract_clause_extract`）+ 规则版本化（D3）
   - RFQ 改走 business_command / workflow_instance（现为原生决策表）
   - `procurement_agent_run_context`（run↔版本绑定）
   - 实体查询 trigram 索引（0166 候选）

## 本版取证方式

- 运行时事实：场景目录 API、`/openapi.json`（319 路由）、`agent_tool_config`（8 个采购工具启用）、
  `alembic_version`（0165）、`pg_tables`（7 张 `procurement_*`）
- 真数据：Oracle 比价 50,599 行；Copilot 合规核对实跑（引用《采购询价管理制度》第一条）
- 测试：后端定向回归（文档/关联/预览/向量化/P0–P3）全绿；前端 2518 passed
