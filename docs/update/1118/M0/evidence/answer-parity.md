# A5 证据：答案级评估器 + 首跑（M0.4）

> 证据日期：2026-11-18
> 环境：本地开发栈（PG 5433 / `qa_metadata`，ACTIVE 发布 `v-p2-d12` = id 1）；本地模型 `Qwen3.8-27B-4bit` @ `http://127.0.0.1:8888/v1`，temperature = 0
> 代码：branch `feat/procurement-m0`，未部署（按 2026-11-18 决策「生产暂不部署，本地全绿后统一发版」）
> 报告：[`eval/reports/parity-2026-11-18-replay-Qwen3.8-27B-4bit-163156.md`](../eval/reports/parity-2026-11-18-replay-Qwen3.8-27B-4bit-163156.md)
> 档案：[`eval/runs/2026-11-18/Qwen3.8-27B-4bit/`](../eval/runs/2026-11-18/Qwen3.8-27B-4bit/)（`off-1/off-2/on-1/on-2.jsonl` + `meta.json`）

## 1. 交付物

| 文件 | 行数 | 职责 |
|---|---|---|
| `backend/scripts/eval_semantic_answer_parity.py` | 660 | CLI + 报告（自检 / 实跑 / 档案回放共三入口） |
| `backend/scripts/answer_parity_compare.py` | 366 | 退出码契约 + 逐题判定编排（L1–L5、同态抖动 J） |
| `backend/scripts/answer_parity_sql.py` | 281 | L1 SQL 规范化、L3 口径元素集合 |
| `backend/scripts/answer_parity_values.py` | 255 | L2 结果集 / L4 数值 / L5 答案 |
| `backend/scripts/answer_parity_runtime.py` | 571 | 问题集加载、档案读写、实跑/前置检查编排 |
| `backend/scripts/answer_parity_cases.py` | 381 | **阳性对照自检**的 33 条用例（期望退出码先写死） |
| `backend/app/tests/unit/test_answer_parity_compare.py` | 416 | 41 条单测（比较层 + 回放 + CLI） |
| `docs/update/1118/M0/eval/questions.json` | — | 问题集 **v1**（7 题，4 题适用），带版本 + sha256 |

**退出码契约**与 D17 演练 (`diag_semantic_switchover.py`) **完全一致**（`test_退出码契约与D17一致` 长期钉住）：
`0` 通过 / `2` 两态 schema 内容不等价（前置故障）/ `3` 跨态口径或答案不一致 / `4` 前置条件或使用错误 / `5` SQL Guard 链失效 / `6` 不可判定。

## 2. 判定分层

| 层 | 内容 | 本轮实跑是否判 |
|---|---|---|
| L1 | SQL 规范文本（别名解析到表身份、标识符大小写、注释/空白归一） | ✅ **仅定位**，文本差不算不通过 |
| L2 | 结果集多重集（行序无关、列名参与） | ❌ 实跑不执行 SQL |
| **L3** | **口径元素集合（硬红线：差异必须为 0）** | ✅ **始终强制**，不看阈值 |
| L4 | 关键数值（金额 0.01 元 / 比率 0.1pp / 计数精确） | ❌ 实跑不合成答案 |
| L5 | 答案等价（结论类别 + 关键数值） | ❌ 同上 |

L3 集合元素：`FROM:<schema.表>`、`MEASURE:<投影>`、WHERE 合取项、`HAVING:`、`JOIN[<类型>]:`、
非内连接 `ON[<类型>]:`、`GROUPBY:`、`DISTINCT`、`PAGE:`、`ORDERBY:`（仅分页时）。
判序：样本完整性 → 同态抖动 J → **L3** → L2/L4/L5 → L1 定位。

## 3. 阳性对照自检：33/33（退出码 0）

```text
./.venv/bin/python scripts/eval_semantic_answer_parity.py --self-test
== 33/33 通过 ==
```

覆盖面（每条期望退出码**先写死**）：

- **规范化 ⇒ 0**：投影列序/别名、别名重命名、单值 `NOT IN` ↔ `<>`、`LEFT JOIN` ↔ `LEFT OUTER JOIN`；
- **口径变化 ⇒ 3**：丢 WHERE 谓词、丢 JOIN、INNER→LEFT、GROUP BY 键、换 FROM 基表、换度量、外连接 ON↔WHERE 搬家、自连接换角色、括号优先级、同名不同表、`FETCH` 行数、`OFFSET`、分页时 ORDER BY 键、`ODS.T` vs `DWD.T`；
- **不可判定 ⇒ 6**：同态抖动 J>0、SQL 不可解析、某态产不出 SQL、要求的层缺样本、L4/L5 **两侧都没数字**；
- **无分页时 ORDER BY 变化 ⇒ 0**（行序不改变多重集）。

### 3.1 反向守卫（证明断言不是空集）

`test_比较器坏掉时自检必须变红` 把 `predicateSet` 恒置为空集（**两个模块都要打补丁** ——
`coverageDiff` 在 sql 层解析、抖动基线在 compare 层解析），自检必须变红且总退出码为 4。
若这条测试变绿，说明「33/33 通过」只是空集假绿。

## 4. 单测：41 passed

```text
./scripts/run_local_tests.sh app/tests/unit/test_answer_parity_compare.py
41 passed in 12.42s
```

含：契约与版本化（v1 摘要在改题后必须拒绝）、L1/L3 规范化、L2/L4/L5 判定、
判序（L3 先于 L2、抖动先于跨态、汇总 3 优先于 6、2/5/4 压过 3）、档案回放（含 ON 缺发布 id ⇒ ValueError）、
CLI（DSN 环境变量**只认名字不回显**、空层、回放目录不存在、实跑要求 L2 ⇒ 4）。

## 5. 前置检查实测（不调模型）

```text
./.venv/bin/python scripts/eval_semantic_answer_parity.py --precheck-only
前置检查通过：schema 内容差异 0；OFF 行数=630 / ON 行数=630；releaseId=1
```

- 类子集：7 个采购类，JOIN 86 条；
- OFF 侧 `semanticReleaseId = null`、ON 侧 = 1（`v-p2-d12`）⇒ **开关确实关住了、也确实走了快照**；
- `contentDiffBlocks = 0`、`orderDiffLines = 0`（A4 的渲染顺序统一在真实数据上成立）。

## 6. 首跑结果：4/4 题退出码 0，J = 0

| 题号 | 问题 | L1 | L3 | 退出码 | L3 口径元素数 |
|---|---|---|---|---|---|
| Q01 | 哪些供应商可以供应物料编码 A0001 的物料？ | ✅ | ✅ | 0 | 9 |
| Q02 | 供应商 S0001、S0002、S0003 近 12 个月的交付表现如何？ | ✅ | ✅ | 0 | 65 |
| Q03 | 供应商 S0001、S0002、S0003 是否有重大质量或合规风险？ | ✅ | ✅ | 0 | 3 |
| Q05 | 对比供应商 S0001、S0002、S0003 的历史采购价格和交期。 | ✅ | ✅ | 0 | 14 |

- **同态抖动 J = 0**：每个态各跑 2 遍，4 题 × 2 态的 L1 文本与 L3 集合**两遍完全一致**
  ⇒ 跨态一致可以归因到开关，而不是采样噪声。
- 首跑产物：`off-1/off-2/on-1/on-2.jsonl`（各 4 条）+ `meta.json`（`status=finished`、`conclusion` 逐题留档）。
- 报告由**档案回放**重新生成（`--replay-dir`，L1+L3），与实跑用的是同一套判定代码；
  回放报告的 `commit` / 模型档位 / 发布信息**继承自被回放档案**，不冒充当前 HEAD。

### 6.1 问题集 v1 的一次真实修正

v0 的题目只写「指定物料 / 这些供应商」，模型在缺实体时报错（3 次重试后抛 `Nl2SqlError`）⇒ 问题本身不可执行。
v1 换成具体编码（`A0001` / `S0001–S0003`），并固定 `version=v1` + sha256；
脚本对摘要不符的问题集**直接拒绝**（改题必须升版本）。

## 7. 不适用与已知缺口（不许当成"已覆盖"）

| 项 | 状态 | 说明 |
|---|---|---|
| Q04 / Q06 / Q07 | 不评 | Q04 走 RAG+Rule、Q06/Q07 走 Workflow，**不经过**语义发布开关的 NL2SQL 路径 |
| L2 / L4 / L5 | 本轮未判 | 实跑只读、不执行 SQL、不合成答案 ⇒ 无样本。判它们必须**显式拒绝**（`--layers L1,L2` ⇒ 4），不能静默判过 |
| Q03 覆盖面 | 仅一条负路径 | 采购域无质量/合规事实表，两态都应产出「无法回答」类查询；这是「开关不会把数据缺口变成有答案」的检验，**不是**答案正确性检验 |
| 题目规模 | 4 题 | v1 只覆盖附录 B 最低集；问卷 D 回收后升 v2，补 5 类 × ≥2 题 |
| Q02 日期字面量 | 潜在抖动源 | 自然月/近 12 月窗口若被模型写成具体日期，可能引入 J>0 ⇒ 6；本轮 J=0，但**换模型档位需重新观察** |

## 8. 档位不可外推（M0.4 §4.9 + 决策 D-2）

本地 27B 只证**机制**（开关不改变查数口径），**不证**生产 DeepSeek 的**幅度**（答案数值层面的稳定性）。
按决策 **D-2**：**本地档通过即可开闸**，不要求生产档复跑；生产档差异统一在 **M7** 暴露（排期 **R9**）。
报告正文与结论段已写入这条口径，避免「本地绿」被读成「生产已验」。

## 9. 代码评审发现与修复（已合入本分支）

| 级别 | 发现 | 修复 |
|---|---|---|
| HIGH | 只收 `exp.Limit`，Oracle 的 `FETCH FIRST n` / `OFFSET` 被漏 ⇒ 「取 10 行 vs 20 行」判成一致 | 收 `Limit/Fetch/Offset`，且**有分页时**把 `ORDER BY` 键纳入 L3（自检 3 条用例钉住） |
| HIGH | L4/L5 两侧都不含数字时比较器返回「相等」⇒ 纯文本差异被判 0（空集假绿） | 两侧都无数值 ⇒ 返回 `None` ⇒ 退出码 6 |
| MEDIUM | `ODS.T` 与 `DWD.T` 塌成同一张表 | 表身份用 `catalog.db.name` 全限定 |
| MEDIUM | L2 把字符串当浮点比 ⇒ `"0001"` 与 `"1"` 相等 | 字符串按字符串比，只有真数字才走容差 |
| MEDIUM | 报告 / 日志回显 LLM endpoint 原值 | 统一 `_maskEndpoint` 打码 |
| LOW | 汇总把 4 误降级成 6（无效前提下的"不可判定"不可信） | 2/5/4 优先于 3，3 优先于 6 |
| LOW | 函数超 50 行（`evaluateQuestion` / `buildReport` / `main` / `runLive` / `runSelfTest`） | 全部拆到 ≤ 49 行；拆分后自检 33/33、单测 41 passed 复跑确认 |

## 10. 复现

```bash
cd backend

# 1) 自检（不连库、不调模型）
./.venv/bin/python scripts/eval_semantic_answer_parity.py --self-test

# 2) 前置检查（连库、不调模型）
export QA_METADATA_DSN="$(grep '^DATABASE_URL=' .env | cut -d= -f2-)"
./.venv/bin/python scripts/eval_semantic_answer_parity.py --precheck-only

# 3) 实跑（两态 × 两遍；LLM key 只走环境变量）
export QA_LOCAL_LLM_KEY=...   # 不要写进仓库 / argv
./.venv/bin/python scripts/eval_semantic_answer_parity.py

# 4) 档案回放（不需要 LLM，可判 L2/L4/L5 —— 前提是档案里带 rows / answer）
./.venv/bin/python scripts/eval_semantic_answer_parity.py \
  --replay-dir ../docs/update/1118/M0/eval/runs/2026-11-18/Qwen3.8-27B-4bit \
  --layers L1,L3
```

> 实跑**只读**：读治理表（`ontology_*` / `semantic_release`）、调本地模型，**不执行**生成的 SQL，
> 退出时把开关恢复为「关」。`--database-url-env` / `--llm-key-env` 只接受**环境变量名**，
> 传字面量 DSN / key 会被拒绝且**不回显**原值。
