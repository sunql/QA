# 变更：修复 plan 校验误杀「表.列」限定名分组与「月份」时间粒度词

- **日期**：2026-08-15
- **作者**：AI 助手
- **Phase**：bugfix（ReAct NL2SQL 计划校验）
- **状态**：done

## 1. 需求

用户报障：复合采购分析问题（"先找 2025 年采购金额最多的 20 种物料，分析其 2025 年采购数量每月变化趋势，再对比 2026/2025 年含税价格 T10"）持续 plan 校验失败，报"分组属性 `PORDERQ.ITMREF_0` 不属于选定的任何类"、"分组属性 `月份` 不属于选定的任何类"。

验收标准：
- `groupBy` 写 `表.列` / `类.列` 完整限定名不再被误杀；仍拒绝真正不存在的表/列。
- 时间粒度（按月/按年/按季度）需求能正确落到日期属性分组，而非裸粒度词；校验失败时报错可操作、重试能自愈。
- 单元测试全量通过、无回归。

## 2. 设计评审

### 根因 1（限定名误杀）
`_propertyRefNames` 只收纳 `property_name` / `source_column` / `business_aliases` 三类**未限定名**。`buildSchemaText` 以 `### 类名 (别名): table=PORDERQ` + `物料编号: STRING (column=ITMREF_0)` 呈现，LLM 自然拼出 `PORDERQ.ITMREF_0` 写进 `groupBy`。与补二（物理列名 `BPSNUM_0` 误杀）同源，但补二只补了物理列、未补 `表.列` 限定形式。
**决策**：`_classRefNames` 改为收 `OntologyClass` 整体，在未限定名之外生成 `表.列`/`类.列`/`表.属性`/`类.属性` 四类限定名；`_propertyRefNames` 顺带补收 `property_alias`。

### 根因 2（时间粒度无归属）
计划 schema 无"时间粒度"概念，`groupBy` 只能填属性名，LLM 遂把"月份"粒度词直接写进 `groupBy`。
**决策（三层，不改 QueryPlan schema）**：① 计划 prompt 规则 5 明确 groupBy 填日期属性、严禁粒度词；② 校验对时间粒度词给可操作提示（列日期候选，重试自愈）；③ `SqlDialect` 新增 `timeBucketRule` 按方言注入时间截断写法。不引入新的计划字段，沿用既有"prompt 引导 + 校验放宽 + 方言规则"模式。

## 3. 数据模型变更

- 无。`QueryPlan.groupBy` 仍为 `tuple[str, ...]`，语义不变。

## 4. 接口契约变更

- 无对外 API/DTO 变更。`SqlDialect` dataclass 新增 `timeBucketRule: str = ""` 字段（内部 prompt 构造用）。

## 5. 实现要点

- `app/services/nl2sql_service.py`：
  - `_propertyRefNames`：补收 `prop.property_alias`。
  - `_classRefNames(cls)`：签名从 `list[OntologyProperty]` 改为 `OntologyClass`，生成未限定名 ∪ `表.列`/`类.列`/`表.属性`/`类.属性`。
  - `validatePlan`：`propsByClass` 改传类对象；`groupBy` 校验对时间粒度词附 `_timeBucketGroupHint` 提示。
  - 新增 `_TIME_BUCKET_TOKENS` / `_DATE_TYPES` / `_datePropertyNames` / `_timeBucketGroupHint` 辅助。
  - `_buildPlanSystemPrompt`：新增规则 5（时间粒度 groupBy 填日期属性），interpretation 顺延为规则 6。
  - `SqlDialect` 新增 `timeBucketRule`；`_TIME_BUCKET_RULE_ORACLE/POSTGRESQL/MYSQL` 三套方言文案；`_buildSystemPrompt` 注入该规则。

## 6. 测试

- `test_query_plan_validation.py` 新增 5 例：`表.列` 限定名通过、`类.列` 限定名通过、不存在的限定名仍拒绝、时间粒度词报可操作提示（含日期候选 + TO_CHAR）、无日期属性时仍给提示。
- `test_nl2sql_service.py` 新增 3 例：Oracle/PG/MySQL 各自注入 `timeBucketRule`。
- `test_query_plan_generation.py` 新增 1 例：计划 prompt 含时间粒度规则。
- 全量单元测试 **691 passed**，无回归。（集成测试需真实 PG，本环境未运行。）

## 7. 安全审查

- 无新增安全面：仅放宽校验集合（纯代码，不调 LLM）与 prompt 规则文本；时间粒度提示只回显本体日期属性名（非外部输入）；无 SQL 注入面变化（SQL 仍经 `_assert_read_only` 校验）。

## 8. 部署验证

- 单元测试全量通过即验证（本变更为纯函数校验 + prompt 文本，无数据/运维动作）。

## 9. 关联

- 同源问题：`docs/20260813-NL2SQL.md` 补二（物理列名误杀）、补四（NULL 排序）、补六/补七（派生别名误杀）。
- 本文档：`docs/20260813-NL2SQL.md`「二·补八」。
- Wiki：`Harness/wiki/nl2sql-engine.md`
