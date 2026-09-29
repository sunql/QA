# 数据与 AI 治理规范

## 本体（Ontology）版本管理

- 每个 `ontology_class` / `ontology_property` / `ontology_metric` 记录带 `version` 与 `valid_from` / `valid_to` 时间范围。
- 修改本体定义（如 `source_table`、`formula`）时，旧版本保留供历史会话查询，新会话用新版本。
- 本体变更需人工审查（HITL），避免破坏既有 NL2SQL 语义。

## 指标（Metric）审查

- 指标公式 `formula` 必须人工审查其业务正确性后才能启用。
- 聚合函数 `agg_function` 与目标类 `target_class_id` 必须一致。
- 禁止在公式中嵌入危险表达式（如子查询）；公式仅限聚合 + 属性引用。

## 模型路由策略

- 模型配置（`llm_config`）的 `cost_threshold`、`weight`、`is_active` 调整需记录原因。
- 会话累计成本超过 `SESSION_BUDGET` 时强制降级到最便宜模型，不可绕过。
- 会话前 N 轮（`SESSION_AFFINITY_TURNS`）沿用同一模型，避免上下文割裂。
- 模型切换决策记录在 Token 流水中（`purpose` 字段标注）。

## Prompt 治理

- NL2SQL 的 System Prompt 模板变更需在 `changes/` 记录并对比效果。
- System Prompt 必须注入本体 schema，强制模型仅查询已定义的 Class/Metric。
- 禁止将完整数据库 schema 或敏感数据放入 Prompt。

## 数据隔离

- 后续多租户通过 `tenant_id` 隔离不同团队的本体与模型配置（Phase 5+）。
- 当前 MVP 单租户，预留字段。

## 默认约束的反例规则（Default + Exception Pattern）

> 2026-09-21 两次踩坑（TCLCOD_0 生产型物料 / INTER_COM_CODE 外部供应商贸易），
> 同一根因反复出现，固化为规则。

### 规则

任何「**默认 X** 过滤 / 限定 / 排除」的业务口径（含 ontology_property.description、
term_dictionary.definition/formula_hint、prompt 模板）都必须**配双层例外**：

1. **声明类例外**（类语义）：用户**明确说明**某类别时不应套用默认
   - 例：「声明是内部交易」「声明是零星物料」「声明是外协服务」
2. **指定对象例外**（实例语义）：用户**明确指定**具体对象（编码、名称、ID）时不应套用默认
   - 例：「明确指定供应商 B019 / B125 / D1」→ 不限制内外部 / 不限制生产型物料

**禁止只写单层例外**。任意单一例外写法在用户场景里会漏处理具体对象指定类请求。

### 落地形式

#### ontology_property.description

- 末尾必须含 `【口径例外】` 段，分条编号说明两类例外
- 模板：
  ```
  【口径例外】不套用 <默认过滤条件> 的场景：
  (1) 用户明确说明「<类别 A>」「<类别 B>」；
  (2) 用户明确指定了具体 <对象类型> 编码或名称（已知对象时不限制）。
  ```

#### term_dictionary.definition + formula_hint

- definition 末尾含「【口径例外】」段，列双层
- formula_hint 含「【例外】」段，SQL 模板写出「不应用过滤」的触发条件
- 单独建条目，不合并到其他 term（LLM plan 阶段按 mapped_property_name 找规则）

#### 测试断言

- 词典条目级：断言「明确指定」字样在 prompt 中出现（防回归到单层）
- SQL 级：跑「明确指定场景」+「默认场景」两组 question，断言 SQL 过滤条件差异
  - 明确指定场景：SQL 不含默认过滤条件（INTER_COM_CODE=1 / TCLCOD_0 IN 等）
  - 默认场景：SQL 含默认过滤条件
- 稳定性冒烟：同问 3 次，确认不是 LLM 抖动

### 防御层级（depth=2）

| 层 | 触发 | 内容 |
|---|---|---|
| 1. 属性 description（PG） | LLM 类内聚焦 | 完整双层例外描述 |
| 2. 顶部 digest（nl2sql_service） | LLM 长 schema 注意力补强 | 同上摘要前置（feat-schema-digest） |
| 3. term dict | 计划阶段 system prompt `<term_dictionary>` 块 | 独立条目 + 双层例外 |

### 适用范围

- ontology_property.description（每个有「默认行为」的属性）
- term_dictionary.definition + formula_hint（每个映射到过滤列的条目）
- NL2SQL plan 阶段 prompt 模板（如有 hard-coded 业务规则）
- 不适用于纯展示类属性（无默认过滤语义）

### 失败案例（已发生两次）

- 2026-09-21 第一轮：DIM_IMATERIAL.TCLCOD_0 description 只写「声明零星物料」单层例外
  → 用户问「B019/B125/D1 供货」时仍套用 TCLCOD_0 IN A02-A05 过滤
- 2026-09-21 第二轮：DWD_GOODS_RECEIPT_DTL.INTER_COM_CODE description 只写「声明内部交易」单层例外
  → 用户问「B019/B125/D1 供货」时仍套用 INTER_COM_CODE=1 过滤

详见 `changes/feat-supplier-exception-rule/summary.md`。
