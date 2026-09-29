# 乙 · 实施计划（服务/链路）

> 负责：M1' Evidence 扩展 → SQL 自动 Evidence 接线 → M3 Confidence 4 级 → M6 Memory Phase A → M7 Hypothesis Hook
> 周期：W1-W2 打底，W5-W8 MB2 主力，W9-W10 M7
> 分支：`feat/evidence-sql-metric` → `feat/confidence-level` → `feat/memory-phase-a` → `feat/hypothesis-hook`

---

## 任务 B1 · M1' Evidence 表扩展（W1-W2，4 天）

**目标**：复用现有 `evidence` 表，新增 v3.1 MVP 的两种 Evidence 类型。

- `app/domain/wiki_models.py` 的 `Evidence` 扩展（alembic 0096）：
  ```python
  # source_type 新增两种取值：
  # "SQL_QUERY"   → payload_jsonb: {sql, params, result_hash, row_count, execution_time_ms, datasource_id}
  # "METRIC_RESULT" → payload_jsonb: {metric_code, period, value, calc_time}
  payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
  session_id: Mapped[str | None]  # 关联 chat session，前端「展开证据」入口用
  ```
- 现有 Document 型 5 元组字段保持不变（向后兼容）
- API：`/evidences` 查询端点（按 session_id / claim_id / source_type 过滤），路由注册注意 `/search` 须在 `/{id}` 前（wiki search 端点教训）
- **不要**动 claim_extractor / wiki 导入链路（Document 型写路径冻结）

**TDD**：integration 测试——写 SQL_QUERY 型 evidence → 按 session_id 查回 → payload 字段完整；Document 型旧测试全绿（回归）。
**验收**：两种类型可写入可查；旧 evidence 行为零变化。

## 任务 B2 · SQL 自动 Evidence 记录（W2，2 天）

**目标**：所有业务 SQL 执行自动落 Evidence，零额外 LLM 调用。

- 收口点：`app/infrastructure/business_db_pool.py` 的 `execute_read_only`（三 adapter：PG/MySQL/Oracle 统一在此层，**只加记录钩子，绝不动 SQL Guard 三集合与只读兜底逻辑**）
- 实现：执行成功 → 计算 `result_hash = sha256(json.dumps(rows, sort_keys=True, default=str))` → 异步落 evidence（source_type=SQL_QUERY）
  - 用 outbox_service 模式异步写，**不阻塞查询主链路**（查询延迟红线）
  - 失败 best-effort（evidence 落库失败只 warning，绝不让查询失败——Oracle ALTER SESSION best-effort 同款降级原则）
- 关联：chat session_id 通过 contextvar 透传（chat_stream 已有 session 上下文）

**TDD**：integration——发起真实 chat 查询 → evidence 表出现一条 SQL_QUERY 记录且 result_hash 与手工计算一致；adapter 断开时查询仍成功且日志有 warning。
**验收**：L1（KPI 命中）/ L2（单步 NL2SQL）/ 多步 / L4 四条路径全部落 evidence。

## 任务 B3 · M2c Evidence 接线收尾 + Chat 证据展示（W3，1.5 天，缓冲周任务）

- ChatPage 答案卡片加「证据」展开：显示 SQL + 行数 + 耗时 + result_hash 前 8 位
- 多步计划每步独立 evidence（step_index 入 payload）
- 前端 SSE 鉴权走 `authHeaders()`（裸 fetch 403 教训），空白 query 422

## 任务 B4 · M3 Confidence 4 级（W5-W6，4 天）

**目标**：按 v3.1 §12.2 实现离散 4 级，淘汰伪精确展示。

- 新建 `app/services/confidence_service.py`：
  ```python
  def calculateConfidence(claim, *, evidenceCount, hasConflict, dataStale) -> ConfidenceLevel
  # 前置（任一命中 → REFUSE）：无 evidence / 数据过期 / 多权威冲突
  # 计分：证据完整 +1 / 规则匹配 +1 / 有历史(min=10，冷启动豁免) +1 / 历史准确率>0.9 +1
  # 映射：3=HIGH 2=MEDIUM 1=LOW 0=REFUSE
  ```
- `knowledge_claim` 加派生列 `confidence_level`（alembic 0098，由 confidence Numeric 原始分 + 前置条件派生，**不删 Numeric 列**——原始分保留供未来校准）
- 冲突前置条件复用 `wiki_conflict_service`（甲不动这个文件，放心用）
- 数据过期前置 v1 只对 claim 级生效（`triple_stale` 字段已存在；表级 freshness 借 DQ 评分时效字段，不可得时跳过该前置并在响应里注明）
- API：claim 详情/列表返回 `confidence_level` + `refuse_reason`；前端 Claim 卡片展示 4 级徽标 + 用户话术（「高置信度，可作为决策依据」等）
- LLM 输出涉及 claim 时，prompt 注入 confidence 级别而非数值（避免假精确话术泄漏）

**TDD**：unit 覆盖 4 级全部分支（含冷启动豁免）；integration 一条真实 claim 链路返回 HIGH 且字段可解释。
**验收**：前端可见 4 级徽标；REFUSE 时给出具体原因（不是笼统「无法判定」）。

## 任务 B5 · M6 Memory Phase A 补全（W6-W7，2.5 天）

**目标**：补全多轮字段继承规则，收敛追问级联入场点。

- `chat_context.py` 扩展 JSONB 快照（v3.1 §15.3 Phase B 字段提前落地，不引入新 store）：
  ```json
  { "inherited_metric": "SA", "inherited_time": {"year": 2026},
    "inherited_filters": {"region": "华东"}, "inheritance_confidence": 0.85 }
  ```
- 字段继承规则补全：上一轮解析出的 metric / 时间 / 维度 / 过滤，本轮省略式追问（「去年呢」「华东呢」）时按规则继承
- **收敛入场点**：追问级联现有 4 处入口 → 抽取为单一 `_resolveInheritedState()`（follow-up cascade A/B/C 三修教训——入口多导致漏 global_filters，这次直接收敛到 1）
- 甲的 Intent 合并（A7）会改 classify 出口，约定：classify 返回结构加 `semanticState` 字段，继承逻辑只读该字段（接口先行，W5 与甲对一次接口）

**TDD**：integration——「今年销售额」→「去年呢」→「华东呢」三轮，每轮 SQL 断言 metric 继承、time 递减、filter 叠加；省略式追问 global_filters 不丢失（multistep global filter 教训回归）。
**验收**：三轮追问全部正确继承；state 快照入库可查。

## 任务 B6 · M7 Hypothesis Hook（W9-W10，5 天）

**目标**：根因归因可选后处理，≤3 假设 + 每条附验证 SQL。

- 触发：Intent==RootCause 或消息含「为什么/原因/归因」（intent 枚举甲已冻结，乙只做消费方）
- 新建 `app/services/hypothesis_service.py`：
  - driver 强制：从 schema digest / 已执行步骤的聚合维度自动发现候选 driver（order_count / avg_price / customer_count 类），prompt 必须基于 driver 列表生成，禁止自由发挥
  - ≤3 假设（prompt 硬约束 + 输出解析截断）
  - 每条假设附验证 SQL（经 SQL Guard 校验后展示，不自动执行——人审原则）
  - 输出标注：「相关性 ≠ 因果，需后续 Evidence 验证」
- 声明类型对齐：假设产物可写为 `claim_type='INFERENCE'` 的 claim（甲确认保留 4 类型 + 新增映射，INFERENCE 即 v3.1 的第三类）
- LLM 调用：+1 次，走 token_usage 收口；失败降级为不输出假设段（答案主体不受影响——multistep 失败隔离同款模式）
- 前端：答案卡片「可能原因」区块，假设可展开看验证 SQL

**TDD**：unit——4 假设输出截断为 3、无 driver 时拒绝生成；integration——真实 RootCause 问题走通，假设挂 INFERENCE claim + evidence。
**验收**：假设段不出现因果断言话术（「因为 X 所以 Y」），AST/断言守卫生效。

---

## 周计划汇总

| 周 | 任务 | 交付 |
|---|---|---|
| W1 | B1 前半 | evidence 表扩展（alembic 0096）+ API |
| W2 | B1 后半 + B2 | Evidence 3 类型齐 + 自动落库钩子 |
| W3 | B3 | Chat 证据展示（缓冲，可提前进 B4 设计） |
| W4 | 与甲共建 Compiler（Vector 编译器收编）+ B4 接口对齐 | wiki_compile 门面化 |
| W5 | B4 前半 | confidence_service + 派生列 |
| W6 | B4 后半 + B5 启动 | 4 级 API/前端 + state 快照 |
| W7 | B5 完成 | 继承规则 + 入场点收敛 |
| W8 | MB2 联合验收 + bugfix | 验收报告 |
| W9-W10 | B6 | Hypothesis Hook |
| W11-W12 | MB4 穿插：governance-authority.md + 冲突裁定落库闭环 | 治理文档 + 裁定链路 |

## 协调事项

- W2 动 `business_db_pool.py` 前，与甲打招呼（甲 W1-W3 主要在 ontology/graph 层，不冲突，但执行层是全局收口点，改动需 review）
- W4-W5 与甲两次接口对齐：①classify 返回结构（A7 依赖 B5 的继承逻辑）；②evidence payload 结构（A8 报告模板引用）
- `chat_service.py` 乙主甲辅：乙的改动集中在中段执行/evidence/confidence，甲集中在入口 planner/intent；**同一天内不要两人都提交该文件**
- 所有 alembic：乙占 0096/0098 号段，甲占 0095/0097，撞车时后合并 renumber
- 测试红线：真实 PG（qa_metadata_test 恒空，别往里面写）；integration 禁与 unit 混跑（TRUNCATE 抹 ontology_class 教训）
