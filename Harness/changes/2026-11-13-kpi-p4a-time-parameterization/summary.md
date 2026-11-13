# KPI P4a 时间参数化（`{time_scope}` 占位符）— 变更总结

- 分支：`feat/kpi-p4a-time-parameterization`（worktree `/private/tmp/qa-kpi-p4a`），base = main `e31367c1`（PR #122 P3 合入后）+ 计划 commit `12181e1c`
- 计划：`docs/superpowers/plans/2026-11-13-kpi-p4a-time-parameterization.md`（Task 1-7）；设计稿：`docs/superpowers/specs/2026-11-13-kpi-time-parameterization-design.md`
- 状态：**代码完成、定向回归全绿、未合并、未部署、零 DDL、零生产写**（生产仍 alembic 0140 + 旧代码）

## 背景

P3（别名解析层 + time_grain/time_column 治理字段）之后，显式时间限定问句一律
`REASON_EXPLICIT_TIME_SCOPE ⇒ Tier B`（LLM 自由发挥时间过滤，不确定）。P4a 交付
**时间参数化**：指标 formula 声明 `{time_scope}` 占位符 + `time_column` 就绪时，
服务端把问句解析出的时间窗口确定性渲染成 Oracle 行级谓词注入 SQL，走 Tier A
产出可复现结果。占位符是治理数据——formula 改回原文即等效关闭该指标的参数化。

## 设计决策（D1-D5，用户确认，spec §3）

| # | 决策 | 结论 |
|---|---|---|
| D1 | P4 批次拆分 | P4a 时间参数化先（本批），P4b 编译器（COMPILED 形态）后立项 |
| D2 | 落地机制 | 占位符 + 治理改写（不做运行时 formula 外包） |
| D3 | 时间形态覆盖 | 近 N 月/季/年（滚动，自然月/季/年对齐）+ 本月/上月/今年/去年（自然区间）；具体日期区间不做 |
| D4 | 方言范围 | Oracle 先行；PG/MySQL fail-closed（degraded，绝不静默降级让 LLM 猜时间） |
| D5 | 存量改写交付 | 脚本 dry-run 只产建议 + admin 逐条确认写入（走既有审计）；脚本零写库 |

关键性质（spec §4）：**占位符缺失 = 行为零变化**——未改写的存量公式在 P4a 前后
行为完全一致（显式时间问句仍 Tier B）。

## 逐 commit 清单（2026-11-13 实现，Task 1-6 + 收尾）

| commit | 内容 |
|---|---|
| `12181e1c` | 计划（TDD 实施计划 Task 1-7） |
| `0ea8a5a6` | Task 1：`time_scope_resolver` 扩展滚动窗口与自然月/年形态（D3；中文数字 `_cnNumberToInt`、`_addMonths`、新形态 collector；docstring 旧约束「滚动窗口一律不解析」改判；83 passed） |
| `1d8c861c` | Task 2：`time_scope_renderer.py` 新建——Oracle 渲染 `[start, end)` 半开区间谓词 + 非 Oracle `TimeScopeRenderUnsupportedError` fail-closed（D4）+ `prepareTimeParameterizedFormula` 组装（8 passed） |
| `921ae471` | Task 3：路由决策表加 `time_param_ready`——`planKpiExecution` 显式时间短路改 `has_explicit_time_scope and not time_param_ready`；`_planKpiTier` 计算 `占位符∈formula 且 time_column 非空`（53 passed） |
| `98e3d82d` | Task 3 fix：`_planKpiTier` 层反向守卫——占位符与 time_column 双就绪缺一不可（变异取证：单事实实现各被恰好 1 条守卫击中） |
| `fbd58caa` | Task 4：Tier A 执行层渲染——`chat_service._executeTierA` 三重防御（渲染失败/无 question/无 time_column ⇒ 一律 return，永不 execute 占位符原文）；渲染失败降级 Tier B；集成用例真实 PG fail-closed + fake adapter Oracle 断言 executed SQL 含 DATE 字面量谓词 |
| `71b53bf4` | Task 5：治理写入校验——formula 含 `{time_scope}` ⇒ `time_column` 必填（create/update 双入口，422 + 明确错误消息；反向不强制） |
| `d3d1025a` | Task 5 fix：错误消息断言 + 反向合法 + 显式清空 time_column 边界（11 passed） |
| `10373f4b` | Task 6：`kpi_time_scope_rewrite.py` 纯函数建议层 + `scripts/kpi_rewrite_time_scope.py` 只读 dry-run CLI（无 --apply，DSN 打码，`--output` JSON 报告） |
| `9d710a90` | Task 6 fix：存量 8 条 ESCAPE_HATCH 建议快照 + 幂等断言（7 passed） |

## dev 库 dry-run 实测（2026-11-13，只读，dev 5433 `qa_metadata`）

```
8 条 ESCAPE_HATCH 指标（metric id 2-9）：
  7 条 BLOCKED（ids 2/3/4/6/7/8/9，time_column 缺失，无建议文本）
  metric 5 KPI_INVOICE_MATCH_RATE ⇒ SUGGEST（time_column=INVOICE_DATE 就绪，
    formula 无 WHERE ⇒ 末尾追加 " WHERE ({time_scope})"）
汇总: {'blocked': 7, 'suggest': 1}；EXIT=0；幂等（快照单测钉死）；零写入
```

后续动作（发版后）：metric 5 由 admin 在指标编辑表单逐条确认改写（走既有审计）；
其余 7 条需先经 admin API 指定 time_column（P3 已登记的多候选/零候选数据工作项）。

## 验收对照（spec §8 六条）

| # | 条款 | 结果 |
|---|---|---|
| 1 | Oracle：「近 6 个月 + 已改写指标名」⇒ Tier A，executed SQL 含时间上下界谓词 | ✅ 代码级（集成用例 fake adapter 断言 DATE 字面量谓词整段；**真实 Oracle 端到端人工冒烟待发版后执行**——本环境无 Oracle 实例） |
| 2 | ratio 型指标行级过滤语义 | ✅（谓词进 formula 占位符位置，聚合前过滤；metric 5 建议已标注 ratio 语义安全） |
| 3 | PG/MySQL + 占位符 ⇒ degraded（reason 明确）不产出 SQL | ✅（真实 PG 集成用例：`executionTier=B` + `degradedReason=time_scope_render_unsupported_dialect`） |
| 4 | 未改写存量 + 显式时间问句 ⇒ 与 P3 完全一致（Tier B） | ✅（`time_param_ready` 默认 False + 占位符缺失短路回归；邻近 chat 套件抽查 47 passed） |
| 5 | 改写脚本 dry-run 幂等、只读、8 条建议齐全 | ✅（dev 实跑 + 快照/幂等单测；CLI 全文仅一条 SELECT） |
| 6 | 新增/改动测试净增红为 0 | ✅（Task 7 收尾定向回归 223 passed 全绿 + 邻近套件 47 passed；无基线红） |

## 明确不做（本批边界，spec §2 非目标）

- ❌ 具体日期区间解析（「2026-01-01 到 2026-03-31」）——后续批次
- ❌ P4b 编译器（COMPILED 技术形态）——独立项目（存量 0 条 COMPILED）
- ❌ PG/MySQL 渲染实现——入口保留，fail-closed degraded
- ❌ week 粒度——跨年边界口径歧义，与「不猜」冲突（`TimeGrain` 枚举不动，week 不参与渲染）
- ❌ 任何 Alembic 迁移 / DB DDL——本批纯代码 + 治理校验

## 执行中对 brief 的已记录偏差（均经任务评审确认）

1. Task 1：`_RE_BARE_YEAR` 加负向先行（避免「今年3月」误吞「今年」破坏存量用例）；
   collector 7 加「前N季度（无『个』）」守卫（「前三季度」= 累计口径，2026-10-31 裁决保持 None）。
2. Task 4：集成 Oracle 用例 patch 目标改 `_kpiExecutionService._adapterProvider`（构造期绑定，
   patch `kpi_execution_service.get_adapter` 不生效）；fake provider 改同步；fixture 需
   `commit()`（跨连接可见性）并把 time_column 补为 source property（否则对账 unknown 拦在路由层）。
3. Task 6：CLI 守卫按 `kpi_backfill_time_grain.py` 先例以 `allowProdWrite=True` 调
   `assertSafeTarget`（read-only dry-run 允许 `qa_metadata` 目标；守卫模块一字未改）。

## 测试与评审证据

- 每任务 TDD（RED→GREEN）+ 评审；fix round（98e3d82d / d3d1025a / 9d710a90）均为评审发现收口。
- Task 7 收尾定向回归（2026-11-13）：brief 指定 10 文件一次命令 **223 passed, 30 warnings in 53.46s**；
  邻近 chat 套件抽查（tier_a_probe / metric_tier_a_switch / tier_parity）**47 passed in 17.18s**。
- 快照测试 `_DEV_STOCK_ROWS` 为 dev-5433 存量 formula 镜像（characterization 语义）：
  存量演进导致失败时须先跑 live dry-run 对账再更新快照。

## 回滚

- 代码：逐 commit `git revert`；**无迁移无 DDL**，无库结构风险。
- 占位符是治理数据：某指标改写有误 ⇒ admin 表单把 formula 改回原文即等效关闭其时间参数化（可逆）。
- 脚本零写入，无数据回滚需求。
