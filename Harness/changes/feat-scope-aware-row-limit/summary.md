# 变更：feat-scope-aware-row-limit（问题范围感知的行数限制）

- **日期**：2026-08-17
- **作者**：QA System
- **Phase**：已落地
- **状态**：done

## 1. 需求

智能问答场景下：用户问题没有明确数据范围（那年/那月/那类）时，NL2SQL 会拉取全表 5000 行返回，超时且不必要；只有问题明确指出时间或过滤范围时，查询结果才会自然收敛。需要：

- **无范围明细查询** → 兜底返回 100 行（避免全表扫描）。
- **有范围查询**（时间 / `plan.conditions`） → 不限制条目数，交给 `fetchmany(queryRowLimit=5000)` + 30s 超时兜底。
- **用户已表达条数/最值意图**（前 N / top N / 限 N / 最高 / 排名） → 保留用户意图，策略不介入。
- **聚合/分组查询** → 不注入兜底（截断分组会产生错误结论，省不了扫描）。

## 2. 设计评审

### 候选方案

| 候选 | 判定 | 依据 |
|---|---|---|
| (A) 纯 prompt 引导 | ❌ 单独不可行 | `docs/20260813-NL2SQL.md:148` 已实测 LLM 偶尔漏写 top-N |
| (B) SQL 生成后改写 | ❌ 拒绝 | Oracle 11g 行数限制是 `ROWNUM` 子查询包裹，正则剥离不安全 |
| (C) 意图阶段抽 scope | ❌ v1 拒绝 | 要改 `ExtractedEntities` + 前端 + 意图 LLM；确定性 scope 信号在计划阶段已免费拿到 |
| **(D) 覆盖 `plan.rowLimit`** | ✅ 推荐 | 确定性、方言无关、frozen dataclass 天然不可变、纯函数可单测、单一接缝 |

### 与初步设计的 4 处差异

1. **注入点改到 `_finalizePlan` 出口**（不是 `_twoStageGenerate`）。计划对象有 4 个下游消费者（SQL 生成 / 执行错误回灌重试 / 状态持久化 / 前端卡片），统一出口覆盖 = 4 处天然一致。
2. **删除"类型词"正则**（供应商 / 客户 / 产品等名词），改用 `plan.conditions` 非空判定"类型/过滤范围"——"各供应商采购汇总"含"供应商"却是全表扫描，正则会误判。
3. **二分 → 四分决策表**。"前 10 条供应商采购量"无时间词 → 二分会覆盖成 100（真实回归）；`groupBy` 套 100 会静默截断分组产出错误结论。
4. **常量 → `config.py` 的 `nl2sqlNoScopeRowLimit`**（`NL2SQL_NO_SCOPE_ROW_LIMIT`，默认 100，`<=0` 关闭）。改环境变量即可回滚。

### 决策表（`_applyScopeRowLimit`，按序命中即返回）

| 序 | 条件 | 动作 |
|---|---|---|
| 0 | `plan.isUnanswerable` | 原样返回 |
| 1 | 问题含显式条数 / 最值（前 N / top N / 限 N / 最高 / 排名 / 最新） | 保留模型值 |
| 2 | 问题含时间范围 **或** `plan.conditions` 非空 | `rowLimit = None` |
| 3 | `plan.aggregations` 或 `plan.groupBy` 非空 | 保留模型值 |
| 4 | 其余（无范围明细全表） | `rowLimit = 100` |

**贯穿原则**：漏限的代价有上限（5000 行 + 30s 超时），误限的代价是答案错误且不可见 → 检测**偏保守**（摇摆时按"有范围 / 不限制"处理）。

## 3. 数据模型变更

无。前端 `QueryPlanCard.tsx:113` 已是 `plan.rowLimit != null` 条件渲染；`rowLimit` 字段类型不变。

## 4. 接口契约变更

无 API 契约变更（字段名/类型不变）。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `backend/app/services/nl2sql_service.py` | 加 `_SCOPE_TIME_RE` / `_EXPLICIT_ROW_INTENT_RE` 正则；`_coerceRowLimit` 脏值归一；`_hasTimeScope` / `_hasExplicitRowIntent` / `_hasQueryScope` / `_applyScopeRowLimit`（约 70 行）；常量 `_PLAN_ROW_LIMIT_RULE`；`_finalizePlan` 加 `scopeText` 形参；`generateValidatedPlan` 加 `scopeQuestion` 形参（主问题 ∪ 子问题）；两处 prompt 规则注入；**顺手修 P2 bug**：`supplementJoinPath` 改用 `dataclasses.replace(plan, joins=tuple(extra))` 修复 `interpretation` 字段丢失 |
| `backend/app/config.py` | 新增 `nl2sqlNoScopeRowLimit: int = 100`，env 别名 `NL2SQL_NO_SCOPE_ROW_LIMIT` |
| `backend/app/services/chat_service.py` | `_twoStageGenerate` 加 `originalQuestion` 形参；`_planAndGenerateSql` lambda 在 `sub_question is not None` 时透传 `dto.question` |

### REFINE 捷径（v1 不改）

- 带时间范围的追问（"只看 2025 年的"）命中不了 `_REFINE_CMP_RE`（要求"列 运算符 值"结构）→ 自然退回两阶段拿到正确行为。
- 唯一残留缺口（"日期 BETWEEN a AND b"）用单测**固化**。
- 正向副作用：本策略让"无范围"查询的 SQL 一定带行数子句，REFINE 捷径改写命中率反而上升（零 LLM 成本）。

### 多步流水线

scope 取**主问题 ∪ 子问题**（`scopeQuestion` 透传）。`rule_based_split` 按「第X步」切句会切掉主问题年份；多步结果进 `StepAggregator` 汇总，某步被截断会污染最终结论。

### 顺手修的 P2 bug

`supplementJoinPath`（nl2sql_service.py:1141-1210）原用手工重建 `QueryPlan` 时漏了 `interpretation` 字段，凡是补充过 JOIN 的计划都会丢失"理解"字段（前端计划卡片 + 下游 prompt 都受影响）。改为 `replace(plan, joins=tuple(extra))` 后，新测试 `test_preserves_interpretation_when_join_supplemented` 守约。

## 6. 测试

### 新增（47 条用例）

- `tests/unit/test_scope_row_limit.py`：**新建**，39 条纯函数用例
  - `TestHasTimeScope`：13 条（10 正例 + 5 反例防误判）
  - `TestHasExplicitRowIntent`：3 条
  - `TestCoerceRowLimit`：6 条（脏值归一）
  - `TestApplyScopeRowLimit*`：17 条（4 个分支 + 不可变性 + 脏值场景）
- `tests/unit/test_query_plan_generation.py`：`TestGenerateValidatedPlan` +4 条端到端（cases 14-17）
- `tests/unit/test_nl2sql_service.py`：`TestGenerateSql` +3 条 prompt 注入（cases 18-20）；case 21 = 既有的"FETCH FIRST N ROWS ONLY" 不出现回归守约
- `tests/unit/test_chat_service.py`：`TestChatService` +2 条多步 `scopeQuestion` 透传（cases 22-23）
- `tests/unit/test_refine_shortcuts.py`：+1 条 REFINE 缺口固化（case 24）
- `tests/unit/test_nl2sql_service.py::TestSupplementJoinPath`：**新建**，1 条 P2 bug 防回归

### 覆盖率

`nl2sql_service.py`：**85%**（与本次改动相关函数 100% 覆盖：`_applyScopeRowLimit` / `_coerceRowLimit` / `_hasTimeScope` / `_hasExplicitRowIntent` / `_hasQueryScope`）。
`config.py`：**88%**。

### 回归

- `test_nl2sql_service.py::TestSqlDialect`（MySQL / PostgreSQL dialect）全部通过——既有 `"FETCH FIRST N ROWS ONLY" not in system` 断言保持绿（`_PLAN_ROW_LIMIT_RULE` 刻意不含任何方言关键字）。
- `integration/test_term_dictionary_inject.py`：真实链路回归（含 `_DEFAULT_PLAN` 带 conditions 的端到端链路）。

### 已知非本次引入的失败（与本次工作无关）

- `tests/unit/test_seed_ontology_sync.py::testSeedJoinsMaterializesEdgesAndIsIdempotent`：期望 `BPCARRIER/BCRNUM` 边，但 `seed_ontology.py` 与全代码库均无该实体/本体类（前后无关）。
- `tests/integration/test_token_usage_service.py` 等：在本环境无 PostgreSQL，所有 223 条 ERROR。

## 7. 安全审查

- `_coerceRowLimit` 强制 int → **无新增 SQL 注入面**。
- 正则均为线性、无嵌套量词 → **无 ReDoS**。
- `_applyScopeRowLimit` 返回新 plan（frozen dataclass `replace`）→ **无 mutation**。
- `rowLimit` 只进 prompt 文本与 `fetchmany` 行数参数，**不进入 SQL WHERE 子句**（由 LLM 按 `dialect.limitRule` 写正确语法）。

## 8. 部署验证

### 回滚开关

```bash
NL2SQL_NO_SCOPE_ROW_LIMIT=0   # 关闭兜底注入（规则 2 "有范围不限制" 仍生效）
```

完全回滚：revert 单个提交。

### 手动冒烟

| 问题 | 期望 `plan.rowLimit` |
|---|---|
| 列出所有收货记录 | 100 |
| 2025 年的收货记录 | null |
| 前 10 条收货记录 | 10（保持） |
| 采购额最高的供应商 | 模型值（保持） |
| 按月统计采购趋势 | 模型值（保持，不注入） |

## 9. 关联

- 设计稿：`docs/20260813-NL2SQL.md` §2（历史回归）+ 本次复审
- Wiki：`Harness/wiki/nl2sql-engine.md`（新增"范围感知行数限制"小节 + REFINE 已知缺口）
- Wiki：`Harness/wiki/config-reference.md`（新增 `NL2SQL_NO_SCOPE_ROW_LIMIT`）
- 规则：`Harness/rules/开发流程规范.md`（TDD 10 阶段）