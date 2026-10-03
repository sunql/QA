# 变更：feat-nl2sql-share-denominator-guard

- **日期**：2026-10-02
- **作者**：Claude / 启琳
- **Phase**：Phase 12 NL2SQL 引擎（业务正确性守卫）
- **状态**：done
- **关联变更**：[../fix-nl2sql-topn-share-denominator/summary.md](../fix-nl2sql-topn-share-denominator/)（方案 A prompt 负向约束——本变更是其确定性升级）；[../2026-10-01-nl2sql-structural-formula-guard/](../2026-10-01-nl2sql-structural-formula-guard/)
- **迁移版本**：无
- **提交**：（待提交时回填）
- **MEMORY**：qa-system-nl2sql-share-denominator-guard.md（更新既有 topn-share-denominator 记忆）

---

## 1. 需求

2026-10-02 真机复现：同一问题（三家供应商 4 月供货量最多三种物料的占比）两次生成，一次正确（独立
`sup_total` CTE 分母）、一次陷阱（Top-N 过滤后的行集上用窗口函数算分母 → 占比恒 100%）。此前方案 A
只在 **prompt** 加负向约束（4/4 采样通过），本次回归证明 prompt 约束是**概率性**的。用户要求：
**SQL 能执行 ≠ 业务正确**，需要机制确认答案是真正的问题答案。

验收标准：
1. 陷阱形态的 SQL 在生成阶段被**确定性拦截**并回灌原因重试（不再依赖 LLM 自觉）。
2. 陷阱变体若漏到执行后，数学不变量能**确定性判错**（占比 > 100% 不可能成立）。
3. 全组占比恒 100% 的**不可判错**场景，答案向用户显式示警而非静默返回。
4. 正确形态（本案例第二次的 SQL）零干扰。
5. 单步 / 多步 / 流式 / REFINE 全链路覆盖。

## 2. 设计评审

| # | 候选方案 | 取舍 | 决定 |
|---|---|---|---|
| A | L1 形态守卫（生成出口拦截 + 回灌重试，与 SQL Guard 同构） | 确定性、零额外 LLM 成本（通过时）；与既有 M2 反馈回灌模式一致 | ✅ 采用（承重层） |
| B | 引入 sqlglot 做语义级块分析 | 重依赖；L1 漏报由 L3 兜底，不值得 | ❌ 拒绝（改用轻量块扫描器） |
| C | L3 结果不变量 + 自动重生成一轮 | 多花一轮 LLM 且不保证对；L1 在前，到达 L3 的违规应极罕见 | ❌ 重生成拒绝；**判错采用**（直接失败明示原因） |
| D | all-1.0 场景跑验证查询（数每组明细数）消歧 | 需从 plan 重建 WHERE，构造脆弱 | ❌ 拒绝（用 L2 示警诚实交付） |
| E | 加 Settings 开关 | 确定性代码 + 完整测试，开关是多余配置面；出问题走 git 回滚 | ❌ 拒绝（决策点 4a：无开关） |

核心判据（L1）：SQL 求值顺序 WHERE 先于 SELECT，因此**同一块**里「排名列过滤 + 窗口函数占比分母」
必然是陷阱（分母=分子），不存在合法的同块形态。合法形态（内层窗口+外层过滤、独立 CTE 分母 JOIN）
都不同块或无窗口。

## 3. 数据模型变更

无。

## 4. 接口契约变更

无 API 契约变更。行为变更：
- `generateSql` 对陷阱形态 SQL 消耗重试预算（与安全校验失败同路径）；
- 占比不变量违规 → `Nl2SqlError`（单步触发既有「回退多步拆解」、多步触发步级隔离）；
- 全组恒 100% → 答案文本以 ⚠️ 示警开头（非流式：`ChatResponse.answer`；流式：首个 token）。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `app/services/nl2sql_semantic_guard.py` | **新建**：`findShareDenominatorIssues`（L1，块扫描器：子查询块划分 + 字面量剥离 + 排名过滤×窗口分母同块共现）、`checkShareInvariants`（L3：单行占比 > 100% 判错；组列可解析时按组求和判错；Decimal(str(v)) 归一化）、`shareAmbiguityWarning`（L2 薄封装） |
| `app/services/nl2sql_service.py` | generateSql 循环内 L1 挂点（`_assert_read_only` 之后，同构 `errors.append + continue`），反馈文案回灌 |
| `app/services/chat_usage.py` | `_runQueryWithRetry`：核验放 `try/else`（**不能**进 try 体——违规异常会被执行失败重试逻辑吞掉重生成，违反决策 3a）；抽 `_checkShareResult` 首次与重试成功路径复用 |
| `app/services/chat_service.py` | 单步非流式：`shareAmbiguityWarning` → `answerText` 前缀，落库与 `ChatResponse.answer` 同用 |
| `app/services/chat_stream.py` | 流式：persistState 注册**之后**追加 answerPieces + yield（同引用保证 H4 断连兜底可见） |
| `app/services/messages_zh.py` | `MSG_NL2SQL_SHARE_DENOMINATOR_FEEDBACK` / `_INVARIANT_FAILED` / `_AMBIGUOUS_WARNING` |

L1 已知漏报边界（诚实记录）：排名列别名不在词表（rn/rnk/rank/row_number/rownum/row_num/seq）内时漏检
→ L3 数学判错兜底；全局 LIMIT 式 Top-N（无排名列过滤）+ 窗口分母漏检 → L3 兜底；UNION 两侧视为同块
（本仓生成风格不出现，接受）。

## 6. 测试

| 用例 | 断言 |
|---|---|
| `test_nl2sql_semantic_guard.py`（新，29 例） | **判别器 = 真机两条 SQL 原文 fixture**：RUN1（陷阱）必拦 / RUN2（正确）必放；四则合法形态放行（内层窗口+外层过滤 / CTE 分母 / 普通比率 / 纯 ROW_NUMBER）；变体（NULLIF 包裹、无空格、大小写、别名前缀、`<` 等价式、子查询内陷阱）按预期；字面量/子查询 WHERE 不误报；L3：>100% 判错、全 1.0 示警、Decimal/str 归一化、组求和、无 Top-N/无别名/空数据不误报 |
| `test_nl2sql_service.py::TestGenerateSqlShareDenominatorGuard`（3 例） | 陷阱→回灌反馈（user prompt 含文案）→第二次干净 SQL；耗尽→`Nl2SqlError` detail 带原因；干净 SQL 零干扰 |
| `test_nl2sql_semantic_guard.py::TestRunQueryShareCheckWiring`（3 例） | 违规→`Nl2SqlError`（UsageMixin 直调桩）；全 1.0→正常返回；非占比查询不受影响 |
| `test_chat_share_denominator_warning.py`（新集成） | 全链路：计划（Top-N 占比）→ SQL（守卫放行）→ 执行恒 1.0 → `answer` 以 ⚠️ 开头且正常回答在后 |

回归（2026-10-02 实跑，真实 PG 串行）：
- 新增测试 **35/35 绿**（guard 29 + generateSql 挂点 3 + runQuery 接线 3）；守卫模块覆盖率 **99%**（164 行仅 2 未覆盖）。
- unit 全量 **3483 passed / 48 failed** —— 48 与基线快照逐条同族（45 chat 套件陈旧假替身 + 3 既有红：`test_execute_read_only_quotes_digit_leading_alias`、`test_execute_read_only_injects_nulls_last`、`test_stub_disabled_raises_permission_denied`，均在本轮失败清单中在场），**零新增红**。
- chat 集成回归 **55 passed**（test_chat_api + test_chat_multi_step + 本特性集成 + test_l2_cte_avg_of_ratios）。

## 7. 安全审查

未触发 security-reviewer：本变更只新增**只读**代码路径（纯函数分析 + 异常 + 文案），不触及认证、
加密、SQL 执行方式；守卫反馈不回显被拒 SQL 原文（沿 M2 口径），日志中 SQL 截断 500/`_RETRY_SQL_LOG_LIMIT`。

## 8. 部署验证

（待部署后回填：真机多次采样复问原问题 ≥5 次，观察恒 100% 是否绝迹、守卫拦截日志是否出现。）

## 9. 已知边界与后续

- L1 是词法判据，覆盖「已知陷阱类」；新问法仍可能踩新坑——机制价值在每类新坑可从「人眼发现」
  沉淀为守卫规则，错误类别单调收敛。
- 日期范围 / Top-N 的 N 与问题原文一致性比对（L1 规则槽位已留）本次不做：本案例未踩，不为推测场景预设计。
- prompt 方案 A 约束保留为第 0 道软防线（降低重试消耗概率）。
