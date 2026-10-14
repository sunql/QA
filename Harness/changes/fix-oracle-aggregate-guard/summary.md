# 变更：fix-oracle-aggregate-guard

- **日期**：2026-10-02
- **作者**：Claude / 启琳
- **Phase**：Phase 12 NL2SQL 引擎（方言规则）
- **状态**：done
- **关联变更**：[../2026-10-01-nl2sql-structural-formula-guard/summary.md](../2026-10-01-nl2sql-structural-formula-guard/summary.md)（同一条真机故障链的上一环）
- **迁移版本**：无
- **MEMORY**：[qa-system-tests-swallowed-by-midclass-function.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-tests-swallowed-by-midclass-function.md)

---

## 1. 需求

**背景**：上一轮修复（`2026-10-01-nl2sql-structural-formula-guard`）让生产问题
「5月份供货量最多的三家供应商所供货物总量占5月份总供货量的比例是多少」通过了**计划校验**，
模型也确实照着引导改写了 formula。但流程仍走不到结果——卡在**SQL 执行**：

```
ORA-00937: not a single-group group function
```

**根因**：Top-N 占比被写成

```sql
SELECT SUM(t.QTY) / (SELECT SUM(s.QTY) FROM supplier_qty s) FROM topn t
```

Oracle 禁止在**同一个查询块**的 SELECT 列表里把聚合函数与标量子查询**并列**；
PostgreSQL 允许该写法，所以只在 Oracle 数据源暴露（本系统生产库是 Oracle）。

**验收标准**：

1. 上述问题在生产 Oracle 数据源上端到端出结果（不再卡在 SQL 执行）；
2. 结果值与手工按 Oracle 合法写法算出的值一致（`0.4500555662151004` → 约 45.0%）；
3. 非 Oracle 数据源（PostgreSQL/MySQL）不受影响——两者的 prompt 里不出现该规则；
4. 方言 prompt 规则编号连续，无跳号。

## 2. 设计评审

| # | 候选方案 | 取舍 | 决定 |
|---|---|---|---|
| A | **执行期改写 SQL**：捕获 ORA-00937 后重写查询结构 | 结构改写（拆分子查询、改 SELECT 列表）的失败面远大于既有的 `_quote_digit_leading_identifiers` 就地加引号；改错会产出**语义不同但语法合法**的 SQL，比报错更危险 | ❌ 拒绝 |
| B | **方言 prompt 规则注入**：把 Oracle 的这条约束写进 System Prompt | 与 `identifierRule`（ORA-00923）/ `nullOrderingRule` / `timeBucketRule` 完全同款；只影响生成，不改执行语义；成本一次、可复用 | ✅ **采用** |
| C | **不修**，靠既有「回灌错误重试一轮」自愈 | 该机制确实存在，但上一轮真机已观察到它**未被触发**（流断开、H4 取消）；且是否自愈不可观测 | ❌ 拒绝 |

**最终决定**：方案 B。规则文本经 code review 后追加**适用范围限定**（见 §5）——初稿的
「分子分母都写成标量子查询、外层用 FROM DUAL」是无条件祈使句，与基础规则 8
（`formula` 用窗口函数 `SUM(x) / SUM(SUM(x)) OVER ()`）存在张力：逐组占比被推成
`FROM DUAL` 会把逐组行塌缩成单行。这属于 review 指出的 MEDIUM，部署前已收紧。

## 3. 数据模型变更

无。纯 prompt 文本 + 方言表字段，不涉及表 / 列 / 索引 / CheckConstraint，无 Alembic 迁移。

## 4. 接口契约变更

无对外 API / DTO 变更。`SqlDialect` 是**内部** frozen dataclass，新增字段
`aggregateRule: str = ""`（带默认值，追加在其余带默认字段之后，不破坏位置参数构造；
全仓 4 处构造点均为关键字传参）。

## 5. 实现要点

**关键文件**

| 文件 | 改动 |
|---|---|
| `backend/app/services/nl2sql_dialects.py` | `SqlDialect` 新增 `aggregateRule` 字段；新增常量 `_AGGREGATE_RULE_ORACLE`；在 `_SQL_DIALECTS_ORACLE_11G` 与 `_SQL_DIALECTS_ORACLE_12C` 上赋值 |
| `backend/app/services/nl2sql_prompts.py` | `_buildSystemPrompt` 的 `extraRules` 链追加 `dialect.aggregateRule` |
| `backend/app/tests/unit/test_nl2sql_service.py` | 新增 2 例；**结构性修复**（见下） |

**注入点**：`nl2sql_prompts.py::_buildSystemPrompt`（服务层包装 `nl2sql_service.py:387`）按
`identifierRule → nullOrderingRule → timeBucketRule → aggregateRule` 顺序追加，序号
`10 + i` 动态编号。新增一条方言规则＝加字段 + 加常量 + 赋值，**不必改 prompt 拼装逻辑**。

**规则文案的限定语（review MEDIUM 的修复）**：改法**仅在分母来自另一个结果集**
（全局合计、Top-N 求和）时适用；逐组占比（每个供应商、每月各占多少）保持窗口函数
`SUM(x) / SUM(SUM(x)) OVER ()`，**不要为此加 FROM DUAL——那会把逐组行塌缩成单行**。

**顺带修复：测试文件的既有结构性损伤**（非本故障引入，仓库首提交 `d72bc12` 即已如此）

模块级 helper `def _buildJoinedClasses():` 被写在 `class TestSupplementJoinPath` 的**类方法之间**，
其后所有 4 空格缩进的 `async def test_*` 落进该函数体内、位于 `return` 之后成为**死代码**。
后果：pytest 报告 149 个用例，实际应为 168——**17 个既有用例从未被收集、从未运行**，
其中包括 `identifierRule` / `nullOrderingRule` / `timeBucketRule` 的**全部**方言规则测试。
修复＝helper 移到模块级末尾 + 为被吞用例补类头 `TestDialectRuleInjection`；
**17 个复活用例未改一字**，全部通过。检测脚本（AST 扫「函数体内嵌套 test_ 函数」）已沉淀进 MEMORY。

## 6. 测试

**新增用例**（`app/tests/unit/test_nl2sql_service.py::TestDialectRuleInjection`）

| 用例 | 断言 | 修复前 |
|---|---|---|
| `test_oracle_injects_aggregate_rule` | Oracle 系统 prompt 含 `ORA-00937`、`标量子查询`、`另一个结果集`（适用范围限定语） | 红 ✅ |
| `test_postgresql_omits_aggregate_rule` | PostgreSQL 系统 prompt **不含** `ORA-00937` | 绿（缺省即真，属**防泄漏回归守卫**，非红证据） |

**判别器说明**：正向用例不断言 `"FROM DUAL"`——基础规则 9 的示例本就含 `SELECT * FROM DUAL`，
该子串对每个方言恒真、无区分力（review LOW）。改断言限定语 `另一个结果集`，它同时是防止
过度触发的关键短语（对应 review MEDIUM）。

**回归**

| 范围 | 结果 |
|---|---|
| `test_nl2sql_service.py` 全量 | **168 passed**（修复前 149 collected —— 差的 19 = 17 复活 + 2 新增） |
| 9 个 NL2SQL 相关文件 | **283 passed / 1 failed**；失败项 `test_query_plan_generation.py::test_missing_fields_use_defaults` 已在 HEAD 基线 worktree 上复现同样失败 ⇒ **预先存在**，与本改动无关（假 LLM 预设响应耗尽） |
| 渲染实测 | Oracle 规则编号 **1–13 连续无跳号**；`ORA-00937` 在 Oracle prompt 出现 **1 次**、在 PG prompt **0 次** |

**覆盖率**（`--cov=app.services.nl2sql_dialects --cov=app.services.nl2sql_prompts`）

```
app/services/nl2sql_dialects.py      44      2    95%
app/services/nl2sql_prompts.py      128     15    88%
TOTAL                               172     17    90%
Required test coverage of 80.0% reached. Total coverage: 90.12%
```

## 7. 安全审查

**未触发 security-reviewer**。判定理由：本次改动不触及认证 / 授权 / 密钥 / 用户输入处理 /
SQL Guard / 文件操作 / 外部 API / 加解密 / 支付中的任何一项——它是**静态 prompt 文本**与
一个内部 dataclass 字段，且该文本经既有 `_sanitizeContext` 之外的常量路径进入 System Prompt
（不含任何用户可控内容的拼接）。

code-reviewer 结果：**APPROVE** —— CRITICAL 0 / HIGH 0 / MEDIUM 1 / LOW 2。
MEDIUM（规则文本过度触发，可能把逐组占比塌缩成单行）**已在部署前修复**（§5 限定语）；
LOW 2 项均已在部署前处理（类 docstring 计数 19→17；正向用例改断言限定语）。

## 8. 部署验证

**部署**（`./scripts/deploy_backend.sh`，无迁移）

```
[deploy] 快照容器内现状 → backups/container/20261002_060407
[deploy] cp app/. → qa-backend:/app/app/
[deploy] cp scripts/. → qa-backend:/app/scripts/
[deploy] cp alembic/. → qa-backend:/app/alembic/
[deploy] 重启 qa-backend ...
[deploy] 等待启动（最多 90s）...
[deploy] ✅ 启动成功
```

**代码一致性**（按 hash 排序逐文件比对，宿主 = 容器）

```
0b9e42baab035159 test_nl2sql_service.py
8e4ad1121a9cc90e nl2sql_dialects.py
cf62a1c4d12e5c78 nl2sql_prompts.py
```

容器内断言：`_AGGREGATE_RULE_ORACLE` 存在；Oracle 实例 `aggregateRule` 非空；PostgreSQL 为空。

**真实数据验证**（生产 Oracle 数据源 `datasourceId=1`「THBI」，`POST /api/v1/chat/stream`）

- 请求：`5月份供货量最多的三家供应商所供货物总量占5月份总供货量的比例是多少`
- 事件序列（完整走通，**无 error 事件**）：
  `meta → class_recall → multi_step_plan → step_plan → plan → sql → chart → data_quality → token×N → step_result → done`
- 耗时 / 用量：`latency_ms=10252`，`tokensUsed=21550`，`cost=0.012831`，`modelName=deepseek-chat`
- 生成的 SQL（**已无聚合与标量子查询混排**）：

```sql
WITH sup_qty AS (... GROUP BY d.SUPPLIER_CODE),
ranked AS (SELECT SUPPLIER_CODE, QTY,
                  ROW_NUMBER() OVER (ORDER BY QTY DESC NULLS LAST) AS RN FROM sup_qty)
SELECT SUM(CASE WHEN RN <= 3 THEN QTY ELSE 0 END) / NULLIF(SUM(QTY), 0) AS TOP3_SHARE
FROM ranked
```

- 结果：`top3_share = 0.4500555662151004`（约 **45.01%**）
- **交叉验证**：上一轮我手工按 Oracle 合法写法（两侧标量子查询 + `FROM DUAL`）在同一数据源
  算出的是**同一个数** `0.4500555662151004`。两条独立路径同值。

> **一个值得记下的观察**：模型**没有**采用规则里给的 `FROM DUAL` 正例，而是选了第三种合法形态
> （窗口函数 `ROW_NUMBER()` + `SUM(CASE WHEN RN<=3 …)` 单块聚合）。生效的是规则里的**禁止**
> （不得并列混排），不是它给的正例。这印证了「禁令比范例更可迁移」，也说明规则文案里的
> 正例只是兜底，不必追求被逐字采纳。

## 9. 关联

- **Wiki**：[Harness/wiki/nl2sql-engine.md §多数据源](../../wiki/nl2sql-engine.md) —— 新增
  「方言规则的注入机制」小节 + 四字段（identifierRule / nullOrderingRule / timeBucketRule /
  aggregateRule）对照表
- **Rules**：[Harness/rules/变更记录强制规范.md](../../rules/变更记录强制规范.md)（本文件即按其 9 段填写）
- **Memory**：`qa-system-tests-swallowed-by-midclass-function.md`（测试被类中函数吞掉的检测法）
- **关联变更（predecessor）**：[../2026-10-01-nl2sql-structural-formula-guard/summary.md](../2026-10-01-nl2sql-structural-formula-guard/summary.md)
  —— 同一条故障链的上一环（计划校验：语句形态 formula 被误当属性校验）
- **提交**：`385c95a`

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比
- [x] 第 3 段迁移文件名 ≤ 32 字符（本次无迁移）
- [x] 第 7 段未触发 security-reviewer，已写明判定理由；reviewer 结果已录
- [x] 第 8 段部署命令 + 启动输出 + 真实数据验证输出已贴出
- [x] 第 9 段 ≥ 3 个跨文件链接（Wiki / Rules / Memory / 关联变更 / 提交）
- [x] 相关 wiki 文档已更新（`Harness/wiki/nl2sql-engine.md`）
- [x] MEMORY 索引已添加（`MEMORY.md` 一行）
- [x] 涉及真实 SQL/DB 改动 → 已有真实数据验证（§8 真机 SSE，非脚本；`scripts/<feature>_realdata.py` 本次未单独编写，理由：本改动不新增可复用的数据库对象或数据变换，验证对象是**既有生产问题**本身）
