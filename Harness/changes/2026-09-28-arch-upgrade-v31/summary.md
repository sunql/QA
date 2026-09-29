# 变更：arch-upgrade-v31（架构升级 v3.1 实施工程）

- **日期**：2026-09-28
- **作者**：Claude / 启琳
- **Phase**：跨 Phase（顶层架构蓝图落地）
- **状态**：draft
- **关联变更**：`../2026-09-26-plan-scope-gate-proposal.md`（PlanDrop 闸门，MB2 复用其模式）
- **迁移版本**：0095_id_mapping, 0096_evidence_payload, 0097_claim_source_version, 0098_claim_confidence_level（编号段：甲 0095/0097，乙 0096/0098，撞车后合并者 renumber）
- **蓝图**：`docs/系统架构优化思路0928-v3.1.md`（§22 现状对接 + §23 MVP 落地建议）

---

## 1. 需求

按 v3.1 蓝图对现有系统做架构升级。代码核实结论：实际系统**领先文档 §22 状态标注**（Claim/Evidence 表、wiki_conflict_service、wiki_compile_service 已存在），升级以改造扩展为主。

**已确认决策**：
| 决策点 | 结论 |
|---|---|
| M0 统一 ID | 全量改造（P0.1-P0.4），非映射表渐进 |
| Claim 类型 | 保留现有 4 类型（FACT/DEFINITION/RULE/STATISTIC），新增 INFERENCE 映射 |
| 首期优先级 | MB2 Agent 收敛 + 可信性 先于 MB3 报告模板 |
| Evidence | 复用现有 evidence 表扩展 SQL_QUERY / METRIC_RESULT 两种 source_type |

**验收标准**：
- MB1（W5）：三库统一 ID 对账一致；Evidence 3 类型齐（Document 已有）；Compiler 门面 + 每日对账任务上线
- MB2（W8）：单步任务 ≤1 次 LLM、多步 ≤3 次；Confidence 4 级真实链路可见；三轮追问字段继承正确
- MB3（W12）：supplier-360-v1 + monthly-ops-v1 模板上线；Hypothesis Hook 可用
- 全局：覆盖率 ≥ 80%，真实 PG 测试，新增 LLM 调用全部走 token_usage 收口

## 2. 设计评审

候选方案：
| 方案 | 取舍 | 结论 |
|---|---|---|
| A. 按文档从零建 Claim/Evidence/Compiler | 干净但废弃已有 4 表 + 编译服务，浪费 5+ 周 | 否 |
| B. 改造扩展（选定） | 复用 knowledge_claim / evidence / wiki_compile_service，补缺口 | **是** |
| C. 映射表渐进 ID（M0 降级） | 风险低但三库对账永远隔着映射层 | 否（负责人明确选全量改造） |

MB2 关键取舍：Intent/Semantic 合并**只合并 LLM 调用层**，IntentType 业务路由枚举（SUPPLIER_360/AGENT_RUN/CHITCHAT 等）冻结——它是路由器不是 Agent，合并前后 50 条历史问题对拍 diff 必须为空。

## 3. 数据模型变更

| 迁移 | 内容 | 负责 |
|---|---|---|
| 0095_id_mapping | id_mapping 表：unified_id PK + bo/ext_id 唯一索引 + 三库 ID 列 | 甲 |
| 0096_evidence_payload | evidence 表：payload JSONB + session_id | 乙 |
| 0097_claim_source_version | knowledge_claim：source_version | 甲 |
| 0098_claim_confidence_level | knowledge_claim：confidence_level 派生列（保留 Numeric 原始分） | 乙 |

Neo4j：节点加 unified_id 属性（P0.2 分批，保留旧 ID 一个版本周期）。Milvus：三 collection 重建加 external_id（P0.3，不支持加列，双写迁移 + release/load）。

## 4. 接口契约变更

- `GET /evidences`（按 session_id/claim_id/source_type 过滤；`/search` 在 `/{id}` 前）
- claim 详情/列表：+ `confidence_level` + `refuse_reason`
- chat 答案卡片：证据展开（SQL + row_count + 耗时 + result_hash 前 8 位）、4 级置信徽标、「可能原因」区块（Hypothesis）
- classify 合并出口：+ `semanticState` 字段（甲 A7 与乙 B5 的接口契约）
- 报告模板：模板列表/渲染/导出端点（MB3）

## 5. 实现要点

分工与代码边界详见：
- **总览/分支策略/协调规则**：[README.md](./README.md)
- **甲（数据/基建）**：[plan-person-a.md](./plan-person-a.md) — M0 全量改造 → M8 Compiler 收口 → Planner ≤5 步 + Intent 合并 → 报告模板
- **乙（服务/链路）**：[plan-person-b.md](./plan-person-b.md) — Evidence 扩展 + 自动落库 → Confidence 4 级 → Memory Phase A → Hypothesis Hook

关键收口点：
- SQL 自动 Evidence 挂在 `business_db_pool.execute_read_only`（三 adapter 统一层，outbox 异步写，失败 best-effort）
- Confidence REFUSE 三前置复用 wiki_conflict_service；数据过期前置 v1 只对 claim 级生效
- 追问级联 4 入口收敛为单一 `_resolveInheritedState()`

## 6. 测试

每任务 TDD（测试清单见两份个人计划）。全局红线：真实 PG + 完整 API 链路；unit/services/integration 不同进程（TRUNCATE 陷阱）；M0 每批迁移跑 count(*) 三库对账 + 图遍历回归。

## 7. 安全审查

触发项：business_db_pool 改动（SQL Guard 收口点）、新查询端点、chat 链路。要求：乙改 execute_read_only 只加钩子不动三集合口径；security-reviewer 在 B2 / A7 两任务合入前必跑。

## 8. 部署验证

- M0 每步前备份 `backups/<日期>_m0/`（pg_dump + neo4j dump + Milvus 备份）
- 里程碑验收脚本：三库对账 diff 为空；LLM 调用计数断言；Top 20 高频场景抽查 ≥90%
- 后端更新走 `scripts/deploy_backend.sh`（容器跑旧代码教训）；前端新页面必须同步 seed_menu_config.py + i18n

## 9. 关联

- 蓝图：`docs/系统架构优化思路0928-v3.1.md`
- Roadmap：`Harness/wiki/ai-roadmap.md` §8（本工程接续 Phase 6.4 之后）
- 关联变更：`../2026-09-26-plan-scope-gate-proposal.md`（PlanDrop 模式复用）、`../2026-09-26-sql-guard-db-side-readonly-proposal.md`（best-effort 降级原则复用）
- 历史教训参考：Milvus 本体向量漂移、两库结构漂移、追问级联 A/B/C、导入台账僵尸任务
