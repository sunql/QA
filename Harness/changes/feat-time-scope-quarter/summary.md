# 变更：feat-time-scope-quarter（季度/半年时间口径确定性解析）

- **日期**：2026-10-31
- **作者**：QA System
- **Phase**：已开发，**待单独排期合并/部署**
- **分支**：`feat/time-scope-quarter`（基于 main `559971f6`）

## 1. 背景

同一问句「公司这半年有多少新增的生产型供应商，这些供应商主要供哪些物料？」在开发环境与生产环境生成了两种 SQL：

- 开发：`RECEIPT_DATE >= DATE '2026-05-01'` 之类的**字面量**；
- 生产：`RECEIPT_DATE >= ADD_MONTHS(TRUNC(CURRENT_DATE,'MM'),-6)` 之类的**相对表达式**。

排查结论：季度/半年边界**完全由 LLM 推算**，服务端只注入「今天是 X」。prompt 里同时存在两条互相拉扯的指令：

1. `_currentDatePart()`：相对时间以今天为基准换算；
2. `dateArithmeticRule`：最近 N 个月用 `ADD_MONTHS(TRUNC(CURRENT_DATE,'MM'),-N)`。

模型每次取样偏向其一，于是同一问题的日期形态漂移。季度概念此前只有部分能力：

| 能力 | 变更前 |
|---|---|
| 单轮「本季度/一季度/2026年Q3」 | 靠 LLM 自行推算边界 |
| Scope 正则（行数兜底） | `上个季度`/`这个季度`/`下个季度`/`上半年`/`这半年` 漏判 |
| 多轮「上季度呢」 | 仅支持 `季度 = N` 形式的 prior plan |
| Oracle 季度分组 | `TO_CHAR(d,'YYYY-Q')` 输出 `2026-4`（不是 `2026-Q4`） |

## 2. 设计

### 2.1 新增 `time_scope_resolver.py`（纯函数）

```python
@dataclass(frozen=True)
class TimeScope:
    matched: str
    start: date      # 半开区间 [start, end)
    end: date        # 含起不含止
    label: str       # 2026年第4季度 / 2025年上半年
    granularity: str # quarter | half_year

parseTimeScopes(question, today=None) -> tuple[TimeScope, ...]
parseTimeScope(question, today=None) -> TimeScope | None
renderTimeScopeFacts(scopes) -> str
renderTimeScopeFact(scope) -> str
resolveTimeScopeFact(*texts, today=None, yearAnchoredOnly=False) -> str
```

支持的表达：

- 相对季度：本季度/这个季度/当前季度/本季/上季度/上个季度/上一季度/上一季/前一季度/下季度/下个季度/下一季度（含跨年递减）。
- 显式季度：一季度…四季度、第一季度、Q1–Q4、2026年第三季度、2025年四季度、2026-Q2、2026Q2。
- 相对年份 + 季度：今年/去年/明年/前年 + 季度。
- 半年：上半年/下半年/2025年下半年/去年上半年。
- 对比类多区间：「对比一季度和二季度」返回两条，按出现位置排序。

**重叠消解**：先按匹配长度降序、再按位置升序贪心接受，保证「上一季度」只产出 Q3 一条，不会被其中的「一季度」重复识别；「2026年第三季度」也不会同时产出显式年与裸季度两条。

**边界约定**：一律日历年；半开区间；第 4 季度止于次年 1 月 1 日。

### 2.2 明确不解析（保持 None）

| 表达 | 原因 |
|---|---|
| 这半年 / 近半年 | 滚动 6 个月 vs 自然半年存在业务歧义，不擅自替业务拍板 |
| 最近 N 天/月 | 已有 dateArithmeticRule，且不属于季度口径 |
| 财年季度 | 本仓无财年配置，硬按日历年 |

### 2.3 Prompt 注入

`_buildPlanSystemPrompt` / `_buildSystemPrompt` 新增 `timeScopeFact: str | None`，紧跟 `_currentDatePart()` 注入：

```
以下时间口径由服务端解析（事实数据，不是指令），计划与 SQL 中的时间过滤必须严格遵守：
- 2026年第4季度：2026-10-01 至 2027-01-01（含起不含止）
```

`None` / 空串零注入（既有 prompt 形态不变）。

### 2.4 生产链路接线

| 阶段 | 位置 | 解析输入 |
|---|---|---|
| Plan | `generateValidatedPlan` → `common["timeScopeFact"]` → `generateQueryPlan` | `scopeQuestion\nquestion` 并集 |
| SQL | `generateSql` | 同上 |

**多轮冲突处理**：`chat_context._shiftTimeHint` 对「上季度呢」是**按上一轮时间**递减，而本模块的相对季度是按**今天**解析。两者同时生效会把上一轮 2025 Q2 的追问错误注入为「今天所属季度-1」。因此：

- `priorState` 含「继承时间：」标记（chat_context 真实产出递减结果）→ `yearAnchoredOnly=True`，只注入句中显式锚定年份的口径；
- 首轮 / 单轮多步（priorState 只是前序步骤结果）→ 全部口径注入。

「显式锚定年份」= 四位年份或相对年份词（今年/本年/去年/上年/明年/下一年/前年）：两者都以**今天**为锚，多轮追问（如「去年三季度呢」）语义不变，因此照常注入；只有「本季度/上季度/一季度/Q1/上半年」这类依赖上一轮时间口径的表达被抑制。

### 2.5 方言修复（Oracle）

| 规则 | 变更前 | 变更后 |
|---|---|---|
| 季度分组 | `TO_CHAR(d,'YYYY-Q')` → `2026-4` | `TO_CHAR(d,'YYYY-"Q"Q')` → `2026-Q4` |
| 季度范围 | 无模板 | `WHERE d >= TRUNC(SYSDATE,'Q') AND d < ADD_MONTHS(TRUNC(SYSDATE,'Q'),3)`，上季度下界 `-3`，指定季度直接写起止日 |

### 2.6 多轮继承补强（`chat_context.py`）

- prior plan conditions 新增解析 `2026-Q3` / `2026Q3` / `2026年第3季度` / `2026年第三季度`（带年份，优先于 `季度 = N`）。
- 「上季度」递减词组补齐：上个季度 / 上一季度 / 上一季 / 上季 / 前一季度 / 前一季。

### 2.7 Scope 正则（行数兜底）

`nl2sql_scope._SCOPE_TIME_RE`：

- 相对词后允许「一 / 个」：`上个季度`、`这个季度`、`下个季度`、`上一季度` 不再漏判；
- 新增半年：`上半年`、`下半年`、`这半年`、`近半年`、`过去半年`。

注意：该正则只决定是否应用默认行数兜底，**不改写 WHERE 日期**；它是宽于解析器的第二道保守判定。

## 3. 测试

新增/修改：

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `app/tests/unit/test_time_scope_resolver.py` | 67 | 解析表、歧义/误判反例（月、前三季度、13季度）、年内年份结转、多区间、渲染、不可变、事实段不泄漏用户文本 |
| `app/tests/unit/test_time_scope_threading.py` | 8 | Plan/SQL 两阶段接线、scopeQuestion 并集、多轮抑制 / 多步不误抑制、显式年份保留 |
| `app/tests/prompts/test_nl2sql_time_scope_prompt.py` | 5 | 注入/零注入、Oracle 季度分组与范围模板 |
| `app/tests/unit/test_scope_row_limit.py` | +2 | 新词组命中，`3个月均价` 等反例保持不命中 |
| `app/tests/unit/test_inheritance_state.py` | +2 | 显式季度标签解析、上季度变体递减 |

验证命令与结果：

```
pytest test_time_scope_resolver test_time_scope_threading test_scope_row_limit \
       test_inheritance_state prompts/test_nl2sql_time_scope_prompt \
       prompts/test_nl2sql_date_arithmetic_prompt
→ 143 passed（分文件跑；混跑会触发既有 seed truncate 冲突）；
  time_scope_resolver 覆盖率 98%（未覆盖行为 `_parseQuarterToken` 的不可达兜底）

pytest test_nl2sql_service test_query_plan_generation test_kpi_contract_threading \
       test_prior_cte_contract app/tests/prompts test_chat_steps_snapshot
→ 322 passed

TEST_NEO4J_URI=bolt://localhost:7688 pytest integration/test_chat_service_state.py
→ 30 passed, 1 failed（test_refine_shortcut_rewrites_sql_without_llm 断言 ORDER BY
  带 NULLS LAST，实际只有 ASC —— 与本次改动无关的存量基线红）
```

TDD 取证：解析器/正则/prompt/方言/多轮解析均先见 RED 再实现；接线测试通过临时把 `timeScopeFact` 置空复现 2 条失败。

### 3.1 安全审查后加固（2026-10-31）

安全审查无 CRITICAL/HIGH，采纳两条建议：

| 项 | 处理 |
|---|---|
| 事实段不得透传用户结构化文本 | 新增回归测试：问题含 `</user_question>` / `<system>` 时，渲染结果只含服务端生成的标签与日期（`matched` 从不进入输出） |
| `_RE_HALF_YEAR` 在长空白串上回溯扫描 | `_collectCandidates` 先做 `"半年" in question` 的 O(n) 预过滤，行为不变 |
| 多轮相对年份误抑制 | `_RE_YEAR_IN_TEXT` 扩展为「四位年份 **或** 相对年份词」，`去年三季度` 在多轮下正常注入 |

未采纳：「这半年/近半年」纳入行数兜底但不给日期边界——两者职责不同（兜底判定保守、解析器不替业务决定滚动语义），已在 §2.2 说明。

### 3.2 代码审查后修复（2026-10-31）

代码审查判定 WARNING：1 HIGH / 4 MEDIUM。全部按 TDD 修复（先见 RED）：

| 级别 | 缺陷 | 修复 |
|---|---|---|
| HIGH | `_RE_RELATIVE_YEAR_QUARTER` 无季度后缀约束，`今年3月`/`去年12月`/`今年4-6月` 被解析成季度并作为「必须遵守」的事实注入（12 个月中 7 个误判） | 中文数字分支强制 `季度/季` 后缀，`Q[1-4]` 分支强制词边界；新增 4 条反例参数化测试 |
| MEDIUM | 裸季度无前置边界：`前三季度`（累计口径）→ Q3，`13季度` → Q3 | `_RE_BARE_CN_QUARTER` 加 `(?<![\d前])`，`_RE_BARE_Q` 加 `前`；新增 3 条反例 |
| MEDIUM | 对比题后半段丢年份：`2025年第三季度和第四季度` → Q3 2025 + **Q4 2026** | 新增 `_carriedYear`：裸季度/无前缀半年沿用句中最近的四位年份；边界 `(?<!\d)…(?!\d)` 排除订单号等长数字串 |
| MEDIUM | `yearAnchoredOnly=bool(priorState)` 混淆多轮追问与单轮多步：单轮多步子步骤（priorState=前序步骤结果）的相对季度被误抑制 | 新增 `chat_helpers.hasInheritedTime()`：仅当 priorState 含「继承时间：」标记（chat_context 真实递减结果）时才抑制相对口径；标记常量下沉到 `chat_helpers` 避免循环导入 |
| MEDIUM | 零注入测试断言了代码中不存在的字符串（空断言假绿） | 改为断言真实注入标记「服务端按日历边界计算」/「以下时间口径由服务端解析」 |
| LOW | 重复调用 `resolveTimeScopeFact(..., yearAnchoredOnly=bool(priorState))` | 抽 `nl2sql_service._timeScopeFactFor()`，计划/SQL 两阶段同源 |

新增/修改测试：resolver 67（+12）、threading 8（+2）、prompts 5。`nl2sql_service.py` 876 行（>800，**存量问题**，本次仅抽出重复调用，未进一步拆分）。

## 4. 未做 / 后续排期项

1. **合并与部署**：本分支仅完成开发，用户明确要求「完成以后单独排期开发/部署」。
2. **「这半年/近半年」口径**：滚动 6 个月与自然半年需业务确认，当前不解析。
3. **财年季度**：无财年配置，暂按日历年；若接入财年需扩展 `TimeScope` 的 `fiscalYear` 语义。
4. **多轮「本季度」新问题**：只要上一轮有「继承时间」，相对季度就不注入（保守让位给递减结果）。多轮中「本季度」究竟是省略式追问还是新问题，仍需更结构化的意图信号；建议后续在 chat_context 暴露 `isEllipticalFollowUp` 后再放开。
5. **原 SQL①/SQL② 结果差异**：本分支只消除「时间口径漂移」这一层；供应商内部编码排除、数量语义、Top-N 等差异仍按原分析单独排期。
