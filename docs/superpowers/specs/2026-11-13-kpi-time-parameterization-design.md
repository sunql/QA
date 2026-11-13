# KPI P4a 时间参数化（`{time_scope}` 占位符）设计稿

> 日期：2026-11-13
> 上游设计：`docs/superpowers/specs/2026-11-11-kpi-definition-consumption-design.md`（P4 时间参数化部分）
> 前序落地：P0-P3 已合 main（P3 = PR #122 `e31367c1`，迁移 0143，未部署）
> 分支：`feat/kpi-p4a-time-parameterization`（worktree `/private/tmp/qa-kpi-p4a`）
> 约束：**不部署、不改生产库**；生产仍 alembic 0140 + 旧代码。

## 1. 背景与问题

P2/P3 落地后，KPI Tier A 执行器可以直接按 `ontology_metric.formula` 执行指标查询。但现状
`kpi_execution_service.py` 对**显式时间限定问句**（如「近 6 个月供应商准时交付率」）一律判
`REASON_EXPLICIT_TIME_SCOPE` 降级 Tier B（LLM NL2SQL 路径）——因为 formula 里的时间窗口
无法确定性地注入。

存量事实（dev 5433 只读实测，2026-11-12）：

- 8 条 ESCAPE_HATCH 公式（metric id 2-9）**全部没有时间过滤**（聚合全部历史）；
- 多数为 ratio 型（`COUNT(CASE WHEN ...)` / `NULLIF(COUNT(...))`）⇒ 运行时外层包一层
  时间谓词在语义上是**错的**（聚合后过滤 ≠ 行级过滤）；
- `time_column` 仅 metric 5（INVOICE_DATE）已写；4 条歧义按 P3 R3「不猜」未写。

⇒ 运行时改写 formula 行不通，唯一可行路径是**治理侧在 formula 里显式声明时间占位符，
服务端确定性渲染**。

## 2. 目标 / 非目标

### 目标

显式时间限定问句在「指标 formula 声明了 `{time_scope}` 占位符 + time_column 存在」时，
走 Tier A：服务端把解析出的时间窗口渲染成行级谓词注入 SQL，产出确定性结果。

### 非目标（本批明确不做）

- ❌ 具体日期区间解析（「2026-01-01 到 2026-03-31」）——后续批次
- ❌ P4b 编译器（COMPILED 技术形态，measure→SQL 编译）——独立项目（YAGNI：存量 0 条 COMPILED）
- ❌ PG/MySQL 的渲染实现（入口保留，未实现 ⇒ fail-closed degraded）
- ❌ week 粒度支持（跨年边界口径有歧义，与「不猜」冲突；`TimeGrain` 枚举不动，week 仅不参与渲染）

## 3. 已裁决的设计决策（用户确认）

| # | 决策 | 结论 |
|---|---|---|
| D1 | P4 批次拆分 | 拆两批：P4a 时间参数化先（本稿），P4b 编译器后立项 |
| D2 | 落地机制 | 占位符 + 治理改写（不搞运行时 formula 外包） |
| D3 | 时间形态覆盖 | 近 N 月/季/年 + 自然月/年（本月/上月/今年/去年）；具体日期区间不做 |
| D4 | 方言范围 | Oracle 先行；PG/MySQL fail-closed（degraded，不静默降级） |
| D5 | 存量改写交付 | 脚本辅助（dry-run 建议）+ admin 逐条确认写入（走既有审计） |

## 4. 路由设计

现状（`kpi_execution_service.py`）：

```
has_explicit_time_scope 为真 ⇒ REASON_EXPLICIT_TIME_SCOPE ⇒ Tier B
```

P4a 后：

```
has_explicit_time_scope 且 formula 含 {time_scope} 且 time_column 非空 且 解析成功
  ⇒ Tier A，渲染时间谓词进 SQL
否则 ⇒ 维持现状（REASON_EXPLICIT_TIME_SCOPE → Tier B，不猜）
```

关键性质：**占位符缺失 = 行为零变化**。未改写的存量公式在 P4a 上线前后行为完全一致
（显式时间问句仍 Tier B）——与 P3 回填前窗口哲学相同，改写是治理动作而非上线动作。

## 5. 组件设计

### 5.1 `time_scope_resolver.py` 扩展

- 复用 PR #90 架构（纯确定性解析、无 LLM、`TimeScope` 值对象）。
- 现有：季度/半年（`granularity: quarter | half_year`）。
- 新增形态（D3）：
  - 滚动窗口：近 N 月、近 N 季度、近 N 年（N 为正整数）；
  - 自然区间：本月、上月、今年、去年。
- `TimeScope` 扩展为携带窗口类型（滚动近 N vs 自然区间）与确定性 `[start, end)`。
  渲染层只消费 `[start, end)` 与标注，不重复解析。
- 已知知识约束：Oracle「近 N 月」必须带上下界（`-(N-1)` 起算），见
  `Harness/wiki` 与记忆 [[qa-system-natural-month-window]]；渲染层负责，解析层只产窗口。

### 5.2 `time_scope_renderer.py`（新文件，纯函数）

- 输入：`(TimeScope, time_column: str, dialect: str)`；输出：SQL 谓词片段字符串 +
  标注（label），或抛 `TimeScopeRenderUnsupportedError`。
- Oracle 实现：半开区间 `[start, end)`；
  - 近 N 月：`col >= ADD_MONTHS(TRUNC(SYSDATE,'MM'), -(N-1)) AND col < TRUNC(SYSDATE,'MM') + INTERVAL '1' MONTH`（或等价确定性表达式，实现时以测试钉死整段子句）;
  - 自然月/年：对应 `TRUNC(col,'MM')/'YYYY'` 边界表达式。
- PG/MySQL：抛 `TimeScopeRenderUnsupportedError` ⇒ 上层转 degraded reason
  `time_scope_render_unsupported_dialect`。**绝不静默降级到 Tier B 让 LLM 猜时间**。
- 谓词为行级过滤，在 Tier A 组装时进入 formula 的 WHERE 位置（见 5.3）。

### 5.3 `kpi_execution_service.py` 路由与组装

- 路由条件见 §4。判定所需的三个事实（formula 含占位符、time_column 非空、方言支持）
  任一不满足 ⇒ 维持 `REASON_EXPLICIT_TIME_SCOPE` Tier B。
- 解析失败（问句时间形态不在 D3 集合内）⇒ Tier B（不猜）。
- 渲染产物作为**行级谓词**进入 formula 组装：ratio 型指标的 CASE/NULLIF 天然在聚合前
  接收过滤（这正是 D2 选占位符而非外层包裹的原因）。
- 新增 degraded reason 值：
  - `time_scope_render_unsupported_dialect`（PG/MySQL + 占位符指标）
  - `time_scope_parse_failed`（问句时间形态未识别）
  两者走既有 unknown+degraded 通道与计数。
- 渲染后的 SQL 仍走 SQL Guard（只读校验），不新增旁路。

### 5.4 治理侧约束与校验

- 指标 formula 编辑校验：formula 含 `{time_scope}` ⇔ `time_column` 必填；
  反向不强制（无占位符 + 有 time_column 合法，供后续批次使用）。
- 写入口 admin 已由 P3 收口（本体指标 REST 六路由），本批不新增写入口。

### 5.5 存量改写脚本 `scripts/kpi_time_scope_rewrite.py`

- **只读 dry-run**：扫描 formula 缺 `{time_scope}` 的 ESCAPE_HATCH 指标（当前 8 条），
  逐条产出：占位符建议插入位置、依据（time_column 现状 / 公式形态）、风险标注
  （如 time_column 缺失 ⇒ 不可改写）。
- 输出报告文件，**不直接写库**。管理员按建议在指标编辑表单逐条确认改写（走既有审计）。
- 幂等：重复运行输出一致；无写副作用。

## 6. 数据流（Tier A 命中路径）

```
问句（含显式时间限定）
  → 指标匹配（P1 匹配器 v2）→ 对账（P3 resolution）
  → has_explicit_time_scope 检测
      ├─ formula 无 {time_scope} / time_column 空 ⇒ Tier B（现状不变）
      └─ 占位符就绪
          → time_scope_resolver 解析窗口（确定性）
              ├─ 解析失败 ⇒ Tier B + reason=time_scope_parse_failed
              └─ 成功
                  → time_scope_renderer 渲染谓词（按方言）
                      ├─ 方言不支持 ⇒ degraded=time_scope_render_unsupported_dialect
                      └─ 成功
                          → formula 占位符替换为谓词 → SQL Guard → 执行 → 证据/计量照旧
```

## 7. 测试策略

- 解析器/渲染器：纯单测（无 DB），表驱动覆盖 D3 全形态 × Oracle；PG/MySQL 抛错用例。
- 路由：Tier A/Tier B 分派单测——占位符三事实逐一缺失均落 Tier B（反向守卫防假绿，
  断言 reason 值本身，不只断言状态码）。
- 谓词断言**打整段子句**（防 ASC/DESC 式漂移假绿，见 [[qa-system-natural-month-window]]）。
- 组装：ratio 型 formula（CASE WHEN/NULLIF）+ 占位符 ⇒ 谓词在聚合前的结构断言；真实 PG
  集成用例验证 executed SQL 含上下界。
- 改写脚本：dry-run 输出稳定性单测 + 对 8 条存量建议的快照断言。
- 治理校验：含占位符无 time_column ⇒ 写入被拒（API 级用例）。
- 测试环境约束照旧：真实 PG（5434）、不跑 Milvus/Neo4j 危险套件、定向 pytest 不跑全量门禁。

## 8. 验收标准

1. Oracle：「近 6 个月 + 已改写指标名」⇒ Tier A，executed SQL 含时间上下界谓词；
2. ratio 型指标：结果与手写等价 SQL 对照一致（行级过滤语义正确）；
3. PG/MySQL + 占位符指标 ⇒ degraded（reason 明确），不产出 SQL；
4. 未改写存量公式 + 显式时间问句 ⇒ 行为与 P3 完全一致（Tier B）；
5. 改写脚本 dry-run 幂等、只读、8 条建议齐全；
6. 全部新增/改动测试净增红为 0（merge-gate 失败集合对差判据）。

## 9. 风险与回滚

| 风险 | 处置 |
|---|---|
| 渲染表达式写错（边界差一天/差一月） | 表驱动测试钉死整段子句 + 真实 PG/Oracle 对照；Oracle 端到端人工验收 1 条 |
| 占位符被 LLM 路径意外消费 | 占位符仅 Tier A 组装消费；Tier B prompt 不含 formula 原文占位符（实现时验证） |
| 8 条改写错插位置 | 脚本只建议不写入；admin 逐条确认；改错可再编辑（治理动作可逆） |
| PG/MySQL 用户问显式时间 | degraded 明确报因（非故障静默）；后续批次补渲染 |
| 回滚 | 代码级 revert 即可；占位符是治理数据，formula 改回原文即等效关闭该指标的时间参数化 |

## 10. 交付物清单

| 交付物 | 形态 |
|---|---|
| `app/services/time_scope_resolver.py` 扩展 | 新形态解析（近 N 月/季/年 + 自然月/年） |
| `app/services/time_scope_renderer.py` | 新文件，Oracle 渲染 + fail-closed |
| `app/services/kpi_execution_service.py` | 路由 + 组装 + degraded reasons |
| `app/domain/models.py` / 治理 service | formula 含占位符 ⇔ time_column 校验 |
| `scripts/kpi_time_scope_rewrite.py` | dry-run 建议（只读） |
| 单测 + 集成测试 | §7 全清单 |
| `Harness/changes/2026-11-13-kpi-p4a-time-parameterization/summary.md` | 变更记录（收尾时） |
