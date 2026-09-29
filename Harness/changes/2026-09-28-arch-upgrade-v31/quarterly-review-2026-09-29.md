# 变更：v3.1 蓝图季度回顾（2026-09-29）

- **日期**：2026-09-29
- **作者**：Claude / 启琳
- **触发**：蓝图 `docs/系统架构优化思路0928-v3.1.md` §26.4 第 3 条「每季度回顾 §22 现状表格」
- **基线**：`epic/v31-upgrade @ cf7c47e`（ahead of `main@da456f4` by 23 commits）
- **alembic head**：`0101`
- **关联**：[summary.md](./summary.md) + [plan-execution.md](./plan-execution.md) + [README.md](./README.md)

---

## 1. 范围

对照蓝图 §22 现状对接 + §23 MVP 落地建议 + §26 现实校准，核对：
1. 已完成能力（✅）真实可证
2. 部分落地能力（🟡）的实际缺口
3. 待建能力（⚪）/ 延后能力（📦）当前状态
4. 蓝图 §22.2 工时估算偏差
5. MB1/MB2/MB3/MB4 划分与实际执行对齐
6. 蓝图 §26 自陈妥协的当前真实性

---

## 2. MB 里程碑现状

| MB | 蓝图定义 | 状态 | 证据 |
|---|---|---|---|
| **MB1 Semantic Foundation** | M0 + M1 + M8 | ✅ 100% 完成 | commit `76ef6ea`（M0 合 main）/ `16bc629`（B1+B2 合 epic）/ `258bd72`（A5 合 epic） |
| **MB2 Agent Convergence** | M2 部分 + M3 + M6 部分 | ✅ 100% 完成（含 Phase B 提前） | `05d3412`（M2a Planner ≤5）/ `6f57882`（A7 通道 1）/ `cedd9b2`（B4 Confidence 4 级）/ `cf7c47e`（B5 Memory Phase A） |
| **MB3 Report MVP** | M4 + M7 | ⚪ **未启动** | 无 `report_template_service.py` / 无 `hypothesis_service.py` / 前端无 ReportsPage |
| **MB4 Governance** | M5 | ⚪ **部分启动**（字段已落但语义偏离蓝图，治理文档未建） | `wiki_models.py:151/278` `authority_level` L0-L5 |

---

## 3. 蓝图 §22.1「已落地 28 项 ✅」真实性核对

全部 28 项**仍真实**，无新增未声明回归。

| 已修回归风险 | 修复 commit |
|---|---|
| Token 计量逃逸（M4） | `dfd8932` |
| Milvus Strong 一致性 gotcha | release/load 兜底（2.4.6 per-request 不生效） |
| SQL Guard 侧信道黑名单 | `d6b9615` |
| FeatureCalc await（L1） | `31316c6` |
| H4 chat 断连兜底落库 | `750a513` |
| Milvus ontology_embedding 删除可见性残差 | uuid + 删对查不可见兜底 |
| routing_layer L3 真话化 | L3 路由已被前端移除 |
| chat LLM router keyless 500 | 显式 modelId 保 404 |
| RAG score NaN% | `1/(1+d)` 映射已修 |
| 文档卡死前端构建 | npm 源切 npmmirror（21× 提速） |

---

## 4. 蓝图 §22.2「部分落地 15 项 🟡」现实偏差

蓝图估算 20 周 → 实际已落 ~11 周（含 Phase B 白送），剩余 ~9 周集中 MB3+MB4。

| §22.2 项 | 蓝图估算 | 实际投入 | 偏差 |
|---|---|---|---|
| Knowledge Claim | 1.5 周 | 0099 + 0100 顺手 B5 内 | 超估算 |
| Evidence | 3.5 周 | 0096/0098/3c98971/31316c6/a52a9ed | 接近估算 |
| Authority | 1.5 周 | 字段在；语义偏离蓝图；文档未做 | 字段 0 周 / 完整 1.5 周未做 |
| Knowledge Compiler | 3 周 | A5 = 3 commit（1d7d542/92e4af9/9868af7） | 实际 ~2 周内，估算略高 |
| Semantic Agent 合并 | 0.5 周 | 通道 1 = 0.5 周；通道 2 需 +1 周 | 通道 2 被低估 |
| Planner ≤5 步 | 0.3 周 | 05d3412 = 1 commit | **吻合** |
| Result 合并 | 0.5 周 | 既有内联 | 已超出估算 |
| Hypothesis Hook | 1.5 周 | ⚪ 0/1.5 | 未启动 |
| SQL 自动 Evidence | 0.5 周 | B2 = 2 commit | **吻合** |
| Report 模板 | 2 周 | ⚪ 0/2 | 未启动 |
| Confidence 4 级 | 0.5 周 | B4 = 5 commit | 实际 ~1 周，略高 |
| Memory Phase A | 1 周 | B5 = 3 commit（含 R1 HIGH-1） | 实际 ~1.5 周 |
| Phase B | 1 周 | 随 Phase A 提前 | **白送** |
| 统一 ID 体系 | 3 周 | M0 全链 | **吻合** |
| Row/Col Security | 2 周 | ⚪ 0/2 | 未启动 |

---

## 5. 蓝图 §26.1「现实校准」当前真实性

| 蓝图自陈妥协 | 当前真实性 | 突破点 / 残留 |
|---|---|---|
| Agent 9→3-4 | ✅ 仍成立 | 落地后实际 3 个（Semantic / Query / Planner 嵌入 chat_multistep），Conversation 是路由层不计 |
| LLM 9-30→2-5 | ✅ 仍成立 | 集成测试断言 `count==0/2`（A7 dea440b）；多步 ≤3 由 token_usage 收口 |
| DSL 3→1 | ✅ 仍成立 | Analysis/Report DSL 已砍 |
| Confidence 4 维→4 级 | ✅ **已突破** | 4 级真实链路：DTO + 徽标 + REFUSE 原因 |
| Evidence 8→3 | 🟡 **部分突破** | 3 种中 2 种落库（Document/SQL_QUERY），METRIC_RESULT 自动落库未做 |
| Authority 完整→字段 | 🟡 **偏离** | `authority_level` L0-L5（v3 等级枚举）vs 蓝图 §4.13 部门枚举 |
| Phase 3→模板+人审 | ✅ 仍成立 | 蓝图声明未被突破，3a 模板未做 |
| 三层 Memory→Phase A/B/C | ✅ **提前突破** | A + B 同时落地（0101 inheritance_snapshot） |
| Knowledge Compiler 8→3 | ✅ **已突破** | 门面 + 每日对账 worker 上线 |

**新增突破（蓝图 §26 未列）**：
- 统一 ID 体系（P0 前置）：`b69706d` 全链合 main（0097 id_mapping + Neo4j unified_id + Milvus 3-collection external_id + 写路径）
- Planner ≤5 步：`05d3412` 1 commit 完整

---

## 6. 蓝图 §〇「v3→v3.1 变更摘要 14 条」逐条落地

| # | 维度 | 状态 | 落地证据 | 残留 |
|---|---|---|---|---|
| 1 | Agent 9→3-4 | 🟡 | `2ba1ae5`/`6f57882` A7 通道 1 收口 | 通道 2 未接；Hypothesis 未落 |
| 2 | LLM 9-30→2-5 | ✅ | `dea440b` 集成断言 | 多步 ≤3 计数精度未与 token_usage 闭环 |
| 3 | DSL 3→1 | ✅ | `multi_step_plan` 是 Semantic Query 序列容器 | 无 |
| 4 | Confidence 4 维→4 级 | ✅ | `cedd9b2` 合 epic | 表级 freshness 未做 |
| 5 | Authority 完整→字段 | 🟡 | `wiki_models.py:151/278` 字段在 | 语义偏离；`governance-authority.md` 未建 |
| 6 | Evidence 8→3 MVP | 🟡 | `16bc629` B1 合 epic | METRIC_RESULT 自动落库未做 |
| 7 | Knowledge Compiler 8→3 | ✅ | `258bd72` 合 epic | 6 运维约束中缓存失效强制未全 |
| 8 | Planner 10+→≤5 | 🟡 | `05d3412` 硬限落地 | §5.3 分级路由未做 |
| 9 | Phase 3 20-50→≤10+人审 | ⚪ | 无 | A8 任务未启动 |
| 10 | Phase 1 80%→Top 20≥90% | ✅ | 文档级口径声明 | 量化跟踪未建 |
| 11 | 三层 Memory→Phase A/B/C | ✅ | `cf7c47e` B5 合 epic | C（Wiki 接入）v3.x |
| 12 | 8 中心→区分用户/内部 | ✅ | 蓝图 §6.1/§6.2 对齐 | U2「我的报告」未做 |
| 13 | 三图合一→P0 统一 ID | ✅ | `b69706d` M0 全链 | 无 |
| 14 | §22 现状对接增强 | ✅ | 变更记录 README/plan-execution/summary 三套 | 无 |

---

## 7. 蓝图 §5.9 Agent 收敛 8 个

| v3 Agent | v3.1 去向 | 状态 |
|---|---|---|
| Conversation Agent | ✅ 保留 | ✅ `_resolveInheritedState` 单入口（B5 cf7cfcfcfcfcf） |
| Intent Agent | 🔀 合并 Semantic | 🟡 通道 1 已收口；**通道 2 未接**（接入位预留 `test_classify_and_recall.py:277-282`） |
| Semantic Agent | ✅ 增强 | ✅ `chat_recall.py` 合并意图 |
| Analysis Planner | ⚡ ≤5 步 | 🟡 硬上限已落；**§5.3 分级路由未做** |
| Query Agent | ✅ + Result 合并 | ✅ |
| Result Agent | 🔀 合并 Query | ✅（Correlation/Contribution 按蓝图砍掉） |
| Hypothesis Agent | ⚡ 可选后处理 | ⚪ 无 `hypothesis_service.py` |
| Evidence Agent | ⚡ SQL 自动记录 | 🟡 SQL_QUERY 已落；**METRIC_RESULT 未自动落** |
| Report Agent | ⚡ 模板渲染 | ⚪ 无 `report_template_service.py` / 无 ReportsPage |

**8 个中 5 全落 + 1 部分（Intent 通道 1）+ 2 未开始（Hypothesis / Report）**。

---

## 8. 关键差异与遗留工作（按优先级）

### 🔴 必须决策的偏离

1. **`authority_level` 字段语义偏离**（v3 等级枚举 L0-L5 vs 蓝图 §4.13 部门枚举 SALES_MGMT/...）—— **负责人决策 (2026-09-29)：选项 (b) 改造 authority_level 字段含义并做迁移**。

### 🟡 MB2 顺手未清

2. **Intent 通道 2（LLM Semantic 兜底）**：蓝图 §5.2 终态要求，A7 标 MB3 占位。
3. **Planner §5.3 分级路由**（2-3 步模板/4-5 步 LLM 校验）：蓝图 §5.3 要求。
4. **Evidence METRIC_RESULT 自动落库**（B1 后半项）：纳入 MB3 任务包（负责人 2026-09-29 确认）。
5. **前端 Chat 证据展开块缺失**：后端 SQL_QUERY 已落，前端无 EvidenceCard。
6. **Confidence 表级 freshness**：B4 v1 仅 claim 级生效。

### ⚪ MB3+MB4 未启动（蓝图 §22.4/§26.1 已显式标 v3.x 候选的除外）

7. **Hypothesis Hook**（§5.6 / §22.2，B6 任务）—— 1.5 周
8. **Report 模板**（§5.8 / §21 Phase 3a，A8 任务）—— 2 周
9. **Authority 治理文档** `docs/governance-authority.md`（§4.13 + §10）—— 1.5 周
10. **Row/Col Security**（§22.2）—— 2 周

---

## 9. MB3 启动顺序（负责人 2026-09-29 拍板）

**优先级**：先 Authority 治理文档（把组织问题显式化），然后按 1→2→4 顺走（Hypothesis → Report 模板 → METRIC_RESULT）。

| 序 | 任务 | 工期 | 蓝图条款 | 备注 |
|---|---|---|---|---|
| **0** | Authority 治理文档 + `authority_level` 字段语义对齐迁移（决策 b） | 1.5 周 | §4.13 + §10 | 文档 + 数据迁移；先把组织问题显式化 |
| **1** | B6 Hypothesis Hook | 1.5 周 | §5.6 + §22.2 | LLM 调用 +1（已显式限定） |
| **2** | A8 Report 模板（monthly-ops-v1 + supplier-360-v1） | 2 周 | §5.8 + §21 Phase 3a | 前端菜单 seed 同步（已立规矩） |
| **3** | METRIC_RESULT 自动落库（补 B1 后半） | 0.5 周 | §4.12 MVP 第 3 种 | metric_promotion_service 接线 |

**总工期 ~5.5 周**（约 1.5 个月），完成后 MB3 = 100%。

### 不推荐现期碰
- **Row-Col Security**（2 周 P2，与蓝图 v3.1 核心降级无直接耦合）—— 排 MB4 末或 Q1 2027
- **Intent 通道 2**（1 周）—— MB3 末或 MB4 单独立卡
- **Planner 分级路由**（0.3 周）—— 可与 B6 同批做或单独立卡

---

## 10. 与蓝图 §26.4「文档维护约定」对齐

| §26.4 条 | 行动 |
|---|---|
| 1. 任何能力降级必须更新蓝图 | 本回顾已对齐（MB3 启动顺序待补 §23.3 划分） |
| 2. v3.x 激活必须先评估 ROI | 未触发 |
| 3. **每季度回顾 §22** | **本回顾即为该周期记录** |
| 4. 不写「未来会做」只写「满足条件 Y 后激活」 | §26.1/§26.2 表保持 |

---

## 11. 变更点摘要（commit 级事实清单）

### MB1 完成
- `76ef6ea`：feat/m0-unified-id → epic/v31-upgrade 合 main（M0 全链）
- `16bc629`：B2 → epic/v31-upgrade（Evidence 扩展 + 归属守卫）
- `258bd72`：A5 → epic/v31-upgrade（Knowledge Compiler 三角色）

### MB2 完成
- `05d3412`：M2a Planner ≤5 步硬限
- `6f57882`：A7 Intent/Semantic 通道 1 收口
- `cedd9b2`：B4 Confidence 4 级
- `cf7c47e`：B5 Memory Phase A + Phase B 提前落地
- alembic：0098→0099→0100→0101 单线

### alembic 链
- `0097_id_mapping`：M0（M0 P0.1）
- `0098_claim_id_nullable`：B2（Evidence SQL 自动落库）
- `0099_knowledge_claim_source_version`：A5（可追溯）
- `0100_knowledge_claim_confidence_level`：B4（派生列）
- `0101_session_query_state_inheritance_snapshot`：B5（Memory Phase A+B 快照）

### 顺手修复（跨批次）
- `55149ee`：createMetric/updateMetric auto-sync
- `7d16a08`：vectors endpoint dim bug
- `350cfe0`：PATCH `/wiki/compile/claims/{claimId}` 补 getCurrentUser
- `842f178`：B5 R1 HIGH-1 multi-step inheritance snapshot + stream wiring

---

## 12. 关联文档

- 蓝图：`docs/系统架构优化思路0928-v3.1.md`
- 现状对接：蓝图 §22（2026-09-29 更新版以本回顾为准）
- 里程碑划分：蓝图 §23（MB3 任务包以本回顾 §9 为准）
- 现实校准：蓝图 §26（已逐项核对，见 §5）
- 历史回顾：本变更即 2026-Q3 周期