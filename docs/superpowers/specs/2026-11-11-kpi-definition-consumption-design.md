# 指标口径真正进入 AIChatService 查询链路 — 设计稿

> **状态**：🟢 APPROVED（2026-11-11 评审通过；先执行 P0 止血，后续批次按 §10 排期）
> **日期**：2026-11-11
> **基线**：`main` @ `55259d67`（原稿写于 `4e535289`，两者在本设计涉及的文件上无差异）
> **上游分析**：本次会话对 `AIChatService` 链路的根因分析（见 §2）
> **相关设计**：`docs/superpowers/specs/2026-10-28-kpi-metric-technical-form-design.md`

---

## 1. 目的

让业务在「本体指标（`ontology_metric`）」和「KPI 指标（`kpi_catalog`）」里定义的计算方法，真正成为 AIChatService 回答指标问题的依据，而不是 LLM 自己理解表结构后生成。

目标形态（已与用户确认）：

1. **分层路由**：识别到 KPI → 确定性执行（Tier A）；识别不到 → 现有 NL2SQL + 契约约束（Tier B）；技术形态缺失 → 明确降级（Tier C）。
2. **数据 + 代码混合对齐**：存量 12 条 KPI 规范为物理列；同时新增「业务词/别名 → 物理列」解析层，避免后续再漂移。
3. **执行体先近后远**：一期直接执行已有可执行 SQL（`execution_source=ESCAPE_HATCH` 的 `ontology_metric.formula`）；结构化 measure→SQL 编译器后置。
4. **匹配升级**：包含式判定 + 多命中澄清，替换当前数学上不可达的 Jaccard 0.75 路径。

---

## 2. 现状冻结（as of 2026-11-11，dev `qa_metadata` 实测）

| 事实 | 证据 |
|---|---|
| 契约注入开关已开 | `system_config.KPI_CONTRACT_INJECTION_ENABLED = true` |
| 12 条 KPI 全部 PUBLISHED，12 条 metric 全部 DRAFT | `kpi_catalog` / `ontology_metric` 实测 |
| L1 等值锚定命中 **0** 条 | `kpi.metric_id → metric.formula == feature_definition.feature_definition` join 实测 |
| 治理⇄技术对账 **12/12 mismatch** | 现场执行 `reconcileMetric(governanceSpecOf, techniqueOf)` |
| 技术形态可执行 8 条（ESCAPE_HATCH），4 条 NONE | `ontology_metric.execution_source` |
| 自然语言问句 Jaccard 约 0.09，阈值 0.75 | `kpi_semantic_match_service` + 12 条 `match_threshold` 实测 |

对账 mismatch 的主要原因不是单条配错，而是两套命名体系未对齐：

- 粒度：治理 `["供应商","月"]` vs 技术 `["SUPPLIER_CODE","FACILITY_CODE"]`
- 列名：治理 `ACTUAL_RECEIPT_DATE` vs 技术 measure.expr `RECEIPT_DATE`
- MEASURE 型：治理声明了分子列，技术侧 `numerator_measure` 为空

当前三条通路均不通：

1. 主 NL2SQL prompt 只渲染本体类/属性（`nl2sql_schema.py:251`），零 `ontology_metric` 消费者。
2. L1 直通依赖「问句出现 KPI code」+「formula 字符串等值命中 FeatureDefinition」，自然语言与存量数据均不满足。
3. Tier B 契约被对账门禁拒绝（`kpi_contract_builder.py:348`），且同样依赖 0.75 关键词匹配。

---

## 3. 范围与非范围

### 3.1 In scope

- KPI 识别器 v2：精确 code + 包含式关键词 + 歧义澄清
- Tier A 执行器：ESCAPE_HATCH formula 的直接执行、安全校验、结果归一、失败降级
- 别名解析层：业务词/别名 → 物理列/类；时间粒度 → 日期列
- 对账改造：治理层改为「解析后集合比较」，解析不了判 unknown
- 数据规范化：12 条 KPI 的结构化列、缺失 measure、4 条 NONE 的 Tier C 标记
- 时间范围语义：Tier A 一期只处理无显式时间限定的问题；参数化后置
- 可观测性：命中方式、执行层级、降级原因、命中率/覆盖率
- 结构化 measure→SQL 编译器（P4）
- 发布闸门与指标状态流转（P5）

### 3.2 Out of scope

- 本体缺失物理源的补齐（`DWD_INCOMING_INSPECTION` / `DWD_NCR` / `DWD_PAYMENT` / `DWD_SUPPLIER_CERTIFICATION` 等）——独立数据工作项，本设计只保证缺失时明确 Tier C。
- 多步/研究会话的指标编排——沿用现有 L1 多步策略，不在本期扩展。
- 自然语言指标的自动创建（`/metric`）行为变更。
- 向量检索/LLM 意图识别作为匹配器——本期用可解释的包含式。

---

## 4. 目标架构

```
用户问题
  │
  ├─ [K1] KPI 识别器 v2
  │     ├─ 唯一命中 ──→ [K2] Tier A 确定性执行
  │     │                  ├─ ESCAPE_HATCH → 执行 metric.formula
  │     │                  ├─ COMPILED     → P4 编译器；之前降级 Tier B
  │     │                  └─ NONE / 执行失败 → [K3] Tier B（LLM + 契约）
  │     ├─ 多个命中 ──→ [K1.5] 澄清（列候选，不猜）
  │     └─ 无命中 ────→ 现有 NL2SQL（Tier B 契约注入）
  │
  └─ 全程：match_method / execution_tier / degraded_reason 落 routing 指标
```

### 4.1 组件落点

| 组件 | 职责 | 落点 |
|---|---|---|
| K1 匹配器 v2 | 精确 code、包含式、歧义集 | `kpi_semantic_match_service.py` 扩展 |
| K1.5 澄清流 | 多命中返回候选 | `chat_stream_output.py` preamble |
| K2 执行器 | 路由、执行、归一、降级、计量 | 新增 `kpi_execution_service.py` |
| K3 契约注入 | 保留 Tier B；补时间粒度与执行来源 | `kpi_contract_builder.py` |
| K4 解析层 | 业务词/别名 → 物理列/类 | 新增 `kpi_alignment_resolver.py` |
| K5 对账 | 解析后集合比较；unknown 语义 | `kpi_metric_reconcile.py` / `kpi_metric_spec.py` |
| K6 数据规范 | 12 条 KPI 一次性规范化 + 校验报告 | 新增 `scripts/kpi_normalize_governance.py` |

### 4.2 不可变约定

所有解析/对账/执行入参均为 frozen dataclass 或 tuple，不原地修改 ORM 实体；执行器只构造新结果对象。

---

## 5. 匹配器 v2（K1）

### 5.1 判定顺序

1. **精确 code**：问句含 `KPI_XXX` 且缓存存在 → `confidence=1.0`，`match_method="code"`。
2. **完整包含**：问句归一化（小写、全角转半角、去标点）后，逐个 KPI 检查：
   - `kpi_name`、`semantic_keywords`、`metric_alias` 任一作为完整子串出现；
   - 命中短语要求：中文 ≥ 2 字，ASCII ≥ 3 字符；
   - 多个命中时按「命中短语长度」降序，最长者胜出。
3. **歧义**：若前两名属于不同 KPI 且命中短语等长 → 返回候选集合，进入澄清。
4. **无命中**：返回 None，走现有 NL2SQL。

### 5.2 关键规则

- **最长匹配优先**：解决「交付率」误命中「准时交付率」。
- **不猜 top1**：不同 KPI 等长并列时澄清，不静默选择。
- **Jaccard 退出运行时**：保留函数供诊断，不再作为路由判据（0.75 不可达）。
- **缓存口径不变**：只索引 `PUBLISHED` KPI；`ensureFresh` 失败降级空集，不阻断 chat。

### 5.3 验收

- 50 条真实问句回归集：期望 KPI 命中率 ≥ 90%，误命中 ≤ 2 条。
- 多候选样例必须返回澄清，不得直接执行。

---

## 6. Tier A 执行器（K2）

### 6.1 路由

按 `ontology_metric.execution_source` 显式路由，不嗅探 formula 内容：

| execution_source | 行为 |
|---|---|
| `ESCAPE_HATCH` | 执行 `metric.formula`（本期主路径） |
| `COMPILED` | P4 前 → Tier B，`degraded_reason="compiled_not_supported"` |
| `NONE` / 空 | Tier C，返回口径说明 + 「暂不可确定性计算」 |
| 未知值 | Tier C，写 warning |

### 6.2 执行安全

- 复用 `business_db_pool._assert_read_only`（`SqlSafetyError`），拒绝非 SELECT/DDL/DML。
- 数据源：`kpi.datasource_id`；为空则不执行（Tier C），不回退到猜测。
- 行数上限：KPI 结果默认 ≤ 1000 行；超出截断并标注。
- 超时：沿用 adapter 现有超时；超时按执行失败降级。
- 结果归一化：单值取首个非 None；比率保留 Decimal 精度，单位来自 `kpi.unit`。
- 计量：0 LLM token；记录查询耗时、数据源、行数；沿用 `METRIC_RESULT` evidence（best-effort）。

### 6.3 失败与降级

- 执行异常 / 空结果 / 超时 → **不静默**：
  - 日志 warning 带 `kpi_code`、`datasource_id`、异常 cause 链；
  - 返回 Tier B（LLM + 契约），响应附 `degraded_reason`；
  - routing 指标记录 `tier=A→B`。
- 只有 `execution_source=NONE` 或数据源缺失才返回 Tier C 文案，不尝试生成 SQL。

### 6.4 时间范围语义（一期）

- 问句含显式时间限定（近 N 月/年/季度/具体年月）且 formula 无参数占位 → **不路由 Tier A**，直接 Tier B，并在响应中标注「未按指标定义口径直接计算」。
- 理由：避免「时间段不对但数字看起来合理」的静默错误。
- 参数化能力在 P4 提供；P4 引入 `{time_scope}` 占位约定与方言渲染。

---

## 7. 别名解析层与对账改造（K4/K5）

### 7.1 解析规则

输入治理侧 token，输出物理列/类：

1. 若 token 已是物理列（在 metric 源类中命中 `source_column`）→ 原样。
2. 否则在 metric 的源类中查 `property_name` / `property_alias` / `business_aliases`。
3. 时间粒度：`月/季度/年/周/日` 映射为 `time_grain` 枚举，并解析到源类的日期列（`time_column`）。
4. 解析唯一 → 返回；多个候选 → unknown（不猜）；无候选 → mismatch。

### 7.2 数据模型增量

- `kpi_catalog.time_grain`：VARCHAR(20)，枚举 `DAY/WEEK/MONTH/QUARTER/YEAR`，nullable。
- `ontology_metric.time_grain`：同上。
- `ontology_metric.time_column`：VARCHAR(100)，物理列名，nullable。
- `grain_keys` 规范：只放物理维度列（不再混入「月」这类时间词）。
- 迁移：新增 alembic revision，幂等回填脚本。

### 7.3 对账语义调整

- 治理层比较：`resolver(治理列集合)` vs `技术 measure 列集合`。
- 解析失败：
  - 治理声明了但技术完全缺失 → mismatch（保持阻断）。
  - 无法判定（多候选/无源类）→ **unknown 放行并标注 degraded**。
- 粒度比较：`time_grain` 枚举相等 + 维度列集合相等；不再直接比较中文字符串。
- 保留三层判定（pass/mismatch/unknown），层 2 仍决定注入门禁，但 unknown 不阻断。

### 7.4 防假绿

- 守卫必须用变异取证：改坏解析规则 / 对账比较，测试必须变红。
- 禁止只断言「有输出」；必须断言具体 verdict 与原因码。

---

## 8. 数据规范化（K6）

一次性脚本 `scripts/kpi_normalize_governance.py`：

1. 读 12 条 KPI + 挂靠 metric + 源类属性。
2. 将 `numerator_columns` / `denominator_columns` / `grain_keys` 规范为物理列。
3. 为 MEASURE 型补齐 `numerator_measure`（以现有 `formula` 列引用为准）。
4. 写回前输出对照报告：每条 KPI 的 before/after 与解析依据。
5. 默认 dry-run；`--apply` 才写库；禁止生产库（白名单 `^qa_metadata(_test.*)?$`）。
6. 4 条 NONE 保持 NONE，标记 Tier C，并在报告中列业务待补项。

---

## 9. 可观测性

- `ChatResponse` / routing 指标新增：`kpi_code`、`match_method`、`execution_tier`（A/B/C）、`degraded_reason`。
- 管理端诊断接口 `GET /api/v1/admin/kpi/diagnose?question=...`：
  - 命中 KPI、匹配方式、对账 verdict 与 reasons、注入块是否为空、Tier A 可执行性。
- 日志：每次 Tier A 执行记录耗时/行数/数据源；每次降级记录原因。
- 看板（P5）：命中率、Tier A/B/C 分布、执行失败率、对账 mismatch 趋势。

---

## 10. 阶段计划与排期

单位：人日（熟悉本仓库的 1 名后端；含 20% 缓冲后总工期约 32–47 人日）。

| 阶段 | 内容 | 工作量 | 依赖 | 交付物 |
|---|---|---|---|---|
| **P0 止血** | 12 条 KPI 数据规范化；补 measure；对账跑通；诊断接口/脚本 | **2–3 d** | 业务确认 4 条 NONE | 契约块非空；「为什么没走口径」一键可查 |
| **P1 匹配升级** | 包含式匹配 + 歧义集 + 澄清流 + 50 条回归集 | **4–6 d** | P0 | 自然语言能命中 KPI |
| **P2 Tier A 执行** | formula 执行器（只读/Guard/上限/超时/计量）+ 归一 + 降级 | **5–7 d** | P1 | 8/12 KPI 确定性回答，0 LLM |
| **P3 别名解析层** | resolver + 对账改造 + 时间粒度迁移 + 数据回填 | **5–7 d** | P0；可与 P1/P2 并行 | 治理侧可填中文；新增指标不再手工对齐 |
| **P4 参数化 + 编译器** | 时间范围参数化；measure→SQL 编译器；三方言 | **8–12 d** | P2 + P3 | 时间限定问题可确定性回答；COMPILED 落地 |
| **P5 治理收口** | 发布闸门开启、状态流转、看板、runbook | **3–4 d** | P2/P4 | 可运营、可回滚、可审计 |

### 10.1 里程碑

- **M1 = P0 + P1（6–9 d）**：口径进入 prompt，自然语言能命中；SQL 仍由 LLM 生成但受契约约束。
- **M2 = M1 + P2（11–16 d）**：命中即确定性执行，数字与定义一致。
- **M3 = M2 + P3 + P4（24–35 d）**：通用、可参数化、不再靠人工对齐。
- **M4 = M3 + P5（27–39 d）**：治理闭环。

### 10.2 PR 批次建议

| PR | 内容 | 可独立上线 |
|---|---|---|
| PR1 | P0 数据规范化脚本 + 诊断接口 + 对账报告 | 是（数据为主） |
| PR2 | P1 匹配器 v2 + 澄清流 + 回归集 | 是（开关控制） |
| PR3 | P2 Tier A 执行器 | 是（`KPI_METRIC_TIER_A_ENABLED`） |
| PR4 | P3 resolver + 对账改造 + alembic | 是（解析失败回落旧比较） |
| PR5 | P4 参数化 + 编译器 | 是（按 execution_source 灰度） |
| PR6 | P5 闸门/看板/runbook | 是 |

---

## 11. 测试策略

按项目规则：TDD（RED→GREEN→REFACTOR），覆盖率 ≥ 80%。

- **单元**：匹配器表驱动（含最长匹配、等长歧义、误命中）；resolver；对账三态；执行路由。
- **集成**：真实 PostgreSQL 元数据库 + 真实业务数据源（Oracle/MySQL demo）+ 完整 API 链路；禁止 sqlite 内存库或直调 service。
- **安全**：Tier A 执行 INSERT/UPDATE/DELETE/DDL 必须被 Guard 拒绝；数据源越权必须拒绝。
- **防假绿**：对账与解析守卫做变异测试（改坏规则必须变红）；断言具体 verdict/reason，不只断言状态码。
- **回归**：50 条问句语料；8 条可执行 KPI 的结果与手工 SQL 对比（数值容差 0.01）。
- **CI**：失败集合对差，净新增失败必须为 0。

---

## 12. 风险与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| 4 条 KPI 物理源不在本体 | Tier A 只能覆盖 8/12 | Tier C 明示；本体补齐独立排期 |
| 包含式误命中 | 答错指标 | 最长匹配 + 等长澄清 + 回归集 |
| formula 执行安全 | 注入/慢查询 | 只读 Guard + 行数上限 + 超时 + 已绑数据源 |
| 对账放松后放过坏定义 | 错口径上线 | unknown 标注 + 发布闸门 + 变异测试 |
| 时间范围语义错配 | 数字对但时段错 | P2 显式降级，P4 才参数化 |
| 缓存脏数据 | 命中旧 KPI | 写时失效 + TTL + 冷缓存重建（沿用现有机制） |

**回滚**：三个开关独立控制——`KPI_MATCH_CONTAINMENT_ENABLED`、`KPI_METRIC_TIER_A_ENABLED`、`KPI_CONTRACT_INJECTION_ENABLED`。任一开关关闭即回到旧行为，不需回滚代码。

---

## 13. 验收标准

### M1

- 50 条问句回归集命中率 ≥ 90%，误命中 ≤ 2。
- 12 条 KPI 对账 0 mismatch（允许 unknown）。
- 输入 KPI code 时契约块非空（非码问句命中后同样非空）。

### M2

- 8 条 ESCAPE_HATCH KPI 确定性回答，tokensUsed=0。
- 结果与手工 SQL 对比容差 0.01。
- 执行失败 100% 带 `degraded_reason`，无静默空答。
- 4 条 NONE 明确返回 Tier C 文案。

### M3

- 新增一条只用中文治理词的 KPI，无需改代码即可通过对账并注入/执行。
- 带「近 6 个月」的指标问题走参数化确定性路径（P4 后）。

### M4

- 发布闸门开启后，未达标指标无法发布（409）。
- 看板可看到命中率、Tier 分布、失败原因。
- runbook 覆盖开关、回滚、数据修复。

---

## 14. 开放决策

| # | 决策 | 当前建议 | 状态 |
|---|---|---|---|
| 1 | Tier A 一期时间范围策略 | 显式时间限定一律降级 Tier B | 已定（用户认可分层方案） |
| 2 | 4 条 NONE 的处理 | Tier C 明示 + 业务补源 | 已定 |
| 3 | 新字段 `time_grain` / `time_column` | 新增（显式优于约定） | **已定（2026-11-11）** |
| 4 | 澄清流前端渲染 | 复用现有 clarify intent | **已定（2026-11-11）** |
| 5 | 是否保留 Jaccard 代码 | 退出运行时，仅保留诊断 | **已定（2026-11-11）** |
