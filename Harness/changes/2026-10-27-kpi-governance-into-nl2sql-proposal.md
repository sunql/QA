# 提案：KPI 治理信息注入 NL2SQL 技术链路（业务面 → 本体指标技术面的对账与利用）

- **日期**：2026-10-27
- **状态**：**已实施（B1–B5 全量）** —— PR #70 已于 2026-10-30 squash 合入 `main`（`607ad254`）；
  prod 已于 2026-10-30 部署，但治理契约注入开关 `KPI_CONTRACT_INJECTION_ENABLED=false`（先关后开），
  ⇒ 线上当前走关闭态，开关打开后的验收待补。本文档**保留为决策与风险存档**（候选方案对比、
  「B2 必须先于 B3」的硬依赖理由、「不做的风险」等分析未在实施记录中重复）。
- **来源**：2026-10-27 会话——讨论 `ontology_metric` 与 `kpi_catalog` 关系时发现的「治理信息只展示、不生成」断层
- **实施记录**：[`kpi-l1-metric-id/summary.md`](kpi-l1-metric-id/summary.md)（B1–B5 落地 SSOT；
  本文件路径被该记录以 `../2026-10-27-kpi-governance-into-nl2sql-proposal.md` 反向引用，**路径勿改**）
- **下游变更**：[`2026-10-28-kpi-metric-technical-form/summary.md`](2026-10-28-kpi-metric-technical-form/summary.md)
  （指标技术形态治理，Phase 6.7）
- **关联**：
  - `Harness/changes/2026-10-18-kpi-datasource-write-flow-spec/`（F2：KPI 数据源写入流，本提案在其下游）
  - `Harness/changes/2026-10-01-nl2sql-structural-formula-guard/`（占比守卫家族，建议④在其上扩展）
  - `Harness/wiki/nl2sql-engine.md`、`Harness/wiki/数据模型.md`（两表双视角定义）
  - SSOT docstring：`backend/app/domain/models.py:363-373`

---

## 问题

`kpi_catalog`（业务/治理面）与 `ontology_metric`（技术面）通过 nullable 弱外键 `metric_id` 挂靠，
设计意图是「业务定义先行，技术实现后置」。但实查代码，治理信息在**三条链路上都没有参与
技术侧的生成**，只有展示：

### 断点 1：L1 直连路径完全绕开本体指标（隐性脆弱）

L1 KPI 语义匹配命中后，执行的是 `kpi.formula` **字符串精确等值**匹配
`FeatureDefinition.feature_definition`：

```
backend/app/services/chat_service.py:1512-1518  _resolveCalculationFeature
    select(FeatureDefinition).where(
        FeatureDefinition.feature_definition == kpi.formula,  # 字符串等值
        ...
    )
```

- `metric_id` 外键存在却不用；`ontology_metric.formula / agg_function / target_class_id`
  在 L1 路径零参与。
- formula 改一个空格 ⇒ 匹配静默失败 ⇒ `_executeCalculationLogic` 返回 None ⇒
  降级走 LLM 流水线。**无告警、无日志区分**（只有通用 warning），用户感知是
  「KPI 直连时灵时不灵」。
- `kpi.formula` 的真实语义在此处是「FeatureDefinition 的名字」，与
  `ontology_metric.formula`（SQL 形态公式）是两个概念，双写极易漂移。

### 断点 2：NL2SQL Prompt 零注入（治理信息不参与生成）

```
backend/app/services/nl2sql_prompts.py   # grep kpi/Kpi/KPI/metric/Metric → 0 命中
```

治理五件套 `numerator / denominator / grain / unit / business_definition`
目前的全部去向：

| 字段 | 去向 | 性质 |
|---|---|---|
| `business_definition` | L1 答案尾巴（`chat_service.py:1595`） | 展示 |
| `grain / unit / numerator / denominator` | 报告卡片段渲染（`report_template_service.py:397-405`） | 展示 |
| `unit` | L1 答案包装（`chat_service.py:1474`） | 展示 |

⇒ LLM 在 plan 阶段**不知道**「及时率不能先 SUM 后除」「统计粒度是月+供应商」。
`numerator/denominator` 恰是聚合约束的声明，`grain` 恰是 GROUP BY 粒度约束，
现在全都白白留在治理表里。

### 断点 3：双 formula 无一致性对账（编译期单向刷新，不校验语义）

```
backend/app/services/knowledge_compiler_service.py:481-510  _compileSqlMetadata
    metric 编译 → 按 metric_id 找挂靠 KPI → 刷 KpiMatchCache 条目
```

只刷缓存，**不校验**挂靠 KPI 的 `formula/numerator/denominator` 与
`metric.formula/agg_function/dimension_defaults` 是否语义一致。两份自由文本，
谁先改都会漂移，且没有任何告警面。

### 断点 4（次要）：`/metric` 创建的指标是治理孤儿

chat 里 `/metric` 定义指标（`chat_domain.py:554-560`）只建 `ontology_metric`，
不回写 `kpi_catalog` —— 产生「有技术形态、无治理户头」的孤儿指标，这类指标
永远不会出现在 KPI 直连、报告卡片、L1 匹配里。

---

## 候选方案总览

| # | 建议 | 断点 | 改动面 | 优先级 |
|---|---|---|---|---|
| B1 | L1 改走 `metric_id` 外键，废 formula 字符串等值 | 1 | 小（1 个查询 + 兼容回退） | **P0，可独立先做** |
| B2 | 编译期双向一致性对账（reconcile 规则） | 3 | 中（knowledge compiler + audit） | **P1，B3 的可信前提** |
| B3 | 挂靠 KPI 治理元数据注入 NL2SQL plan 上下文 | 2 | 中（prompts + 上下文装配） | P1（依赖 B2） |
| B4 | `denominator` 接入既有占比守卫 | 2 | 小-中（nl2sql_semantic_guard 扩展） | P2（依赖 B3） |
| B5 | 创建侧双向引导（孤儿指标回写 + grain 校验） | 4 | 小 | P2，顺手 |

依赖关系：`B2 → B3 → B4` 主链；`B1`、`B5` 独立。

---

## B1：L1 路径改走 metric_id 外键

### 方案

`_resolveCalculationFeature` 解析顺序改为：

1. `kpi.metric_id` 非空 → `ontology_metric` → FeatureDefinition（按 metric 新增显式关联，
   或 metric.formula → FeatureDefinition.feature_definition 等值）；
2. `metric_id` 为空的**存量 KPI** → 保留现行 formula 等值回退，但命中回退路径时
   `logger.warning("L1 fallback formula-match for %s")`，让存量依赖可见。

### 需要先核对的点（实现前）

- [ ] `FeatureDefinition` 是否适合加 `metric_id` 列（涉及迁移）；若不加，则
  `ontology_metric.formula → FeatureDefinition.feature_definition` 仍是字符串等值，
  只是把漂移面从两份缩到一份——**需要评估是否值得**。最小版本可以只做「metric_id
  非空时走 metric，为空走回退 + warning」，不加列、无迁移。
- [ ] 存量数据里有多少 KPI 是 `metric_id` 为空但 formula 等值命中的（生产
  `SELECT count(*) FROM kpi_catalog WHERE metric_id IS NULL AND formula IS NOT NULL`）。

### 验收

- L1 命中且 `metric_id` 非空 → 走外键链路；formula 漂移不再影响该路径；
- 存量回退路径有独立日志可查；
- 既有 L1 集成用例全绿（含 `METRIC_RESULT` evidence 落库断言，`chat_service.py:1555-1564`）。

---

## B2：编译期双向一致性对账

### 方案

knowledge compiler 的 metric 编译钩子（`knowledge_compiler_service.py:481`）扩一条
reconcile 检查（只读、落 audit_history，风格同既有 reconcile 三库对账）：

对每个 `metric_id` 挂靠的 KPI 检查：

| 检查项 | KPI 侧 | metric 侧 | 判定 |
|---|---|---|---|
| 分子分母 vs 公式 | `numerator` / `denominator` | `formula`（含 `agg_function`） | 语义比对：结构解析（列名集合包含）优先，解析不了 → LLM 判等，仍判不了 → 标 `unknown`（**不谎称一致**，同 cost_source 口径原则） |
| 粒度 | `grain` | `dimension_defaults` | 键集合一致性（弱校验） |
| formula 概念混用 | `kpi.formula` 实际是 FeatureDefinition 名 | `metric.formula` 是 SQL 公式 | 若两者文本相等 → 警告「概念混用嫌疑」（B1 落地后此检查可撤） |

不一致 → audit_history 落条目 + admin 可见（具体出口待定：审计页 or 现有 reconcile 巡检报告）。

### 验收

- 构造不一致 fixture（改 KPI.denominator 不改 metric.formula）→ 对账出条目；
- 一致 fixture → 无条目；`unknown` 判定有独立标签，不与「一致」混淆；
- 对账本身只读，不阻断编译。

---

## B3：治理元数据注入 NL2SQL plan 上下文（主链收益）

### 方案

plan 系统提示（`nl2sql_prompts.py:_buildPlanSystemPrompt`）新增「KPI 契约」段：

```
[KPI 契约] 以下 KPI 与本次查询涉及的本体类挂靠，SQL 必须遵守其口径：
- 供货及时率 (KPI_003, v2.1, 单位%)
  分子: 按期到货行数 | 分母: 总到货行数 | 粒度: 月+供应商
  技术公式: SUM(received_qty)/SUM(order_qty)  ← 比率型，禁止先聚合分母分子再相除以外的写法
```

装配路径（注入点二选一，实现时定）：

- **A. 按 plan 引用的类反查**：plan 选中的 `ontology_class` ← `ontology_metric.target_class_id`
  ← `KpiCatalog.metric_id`（PUBLISHED 才注入）。精准但 plan 之后才有 ⇒ 只能注 user prompt / SQL 生成阶段。
- **B. 按问句先匹配**：复用 `KpiMatchCache`（补一个 `target_class_id → KPIs` 索引），
  L1 miss 后把候选 KPI 契约注进 plan system prompt。早注入，可影响 plan 本身。

倾向 **B 为主、A 为兜底**：治理约束最大的价值是影响「选哪些类、怎么聚合」的计划阶段，
而不是事后修正 SQL。数据源现成（缓存已常驻全量 PUBLISHED KPI）。

### 需要先核对的点（实现前）

- [ ] 注入后 plan prompt 的 token 预算影响（2048 计划预算是否够；候选 KPI 上限截断策略）；
- [ ] 多步 / REFINE 路径的注入一致性（`_buildPlanUserPrompt` 的 `priorState` 文本注入不冲突）；
- [ ] 命中多 KPI（同义问句挂多个 KPI）时的取舍规则——全部注入 or 置信度 Top-1。

### 验收

- 问句命中 PUBLISHED KPI → plan prompt 含契约段；未命中/无挂靠 → 不含（零回归）；
- 契约中的公式与 B2 对账通过的版本一致（**不注入对账未通过的条目**，宁可少注入不注入错的）；
- 契约注入有独立日志开关，可灰度。

---

## B4：denominator 接入既有占比守卫

在 `nl2sql_semantic_guard.py`（Top-N 分母守卫家族）上扩展：问句命中 KPI 且其
`denominator` 非空时，对生成的 SQL 做弱校验——分母表达式的列集合须与 `denominator`
声明的列**兼容**（包含即可，不做等价证明）。不符 → 复用 L2 歧义示警出口，
不硬拒（治理声明可能有历史误差，先示警积累数据）。

### 验收

- 构造「分母列与声明不符」的 SQL → L2 示警出现；
- 无 KPI 命中 / denominator 为空 → 守卫行为与现状完全一致。

---

## B5：创建侧双向引导

- `/metric` 创建指标后自动建 DRAFT `KpiCatalog` 并挂 `metric_id`
  （`chat_domain.py:554-560`；名称用 metric_name，status=DRAFT 不进 L1 缓存，
  符合「业务定义后补」的设计方向）；
- KPI 目录保存/更新时，若 `grain` 与挂靠 metric 的 `dimension_defaults` 粒度键不一致
  → 表单提示（不阻断保存）。

---

## 建议排期

```
B1（独立小修，1 天级）─── 可立即做
B2（对账，2-3 天级）→ B3（注入，3-5 天级）→ B4（守卫扩展，1-2 天级）
B5（顺手，0.5 天级）─── 可搭任意批次
```

B2 先于 B3 是硬依赖：**注入未经对账的治理声明等于把漂移喂给 LLM**，
比不注入更危险。

## 不做的风险

- 维持现状 = 治理五件套永远只是报告卡片文案；KPI 直连的准确性继续被
  formula 字符串等值的漂移面随机击穿；
- 若先做 B3 跳过 B2，错误口径会以高置信度（"系统契约"的姿态）进入 prompt，
  比现状更糟——这也是本提案把 B2 排在 B3 前的原因。
