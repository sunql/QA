# AIChat Service 收尾与补债计划

- 日期：2026-10-14
- 基线：`origin/main` = `b81bf1e0`（含 #8 `b3889ef2` 与 #11 `4e5213e9`）
- 上一计划：`docs/superpowers/plans/2026-10-13-aichat-service-priority.md`（12 项 + N1–N5）
- 协调骨架沿用：`docs/superpowers/plans/2026-10-13-aichat-rollout.md`

## 本计划要解决的问题

上一计划的 12 项已全部有代码落地，但**落地 ≠ 达成**：

1. P0 四项（#11）已合入 main，审计查出 **4 处生产缺陷**（含 1 个跨数据源串 state + 500 的真 bug）
2. P1/P2 六项（#12–#17）分支上写了代码，但**多数是死代码或半成品**，原样合入会把 6 个未接线模块带进主干
3. 原计划未识别的欠账：52 条 unit 测试失败、行数红线逼近、测试库 alembic 污染

**本计划不是"继续执行上一计划"，而是"逐项核实后重新定级并修复"。**

---

## 一、审计结论：12 项 + N1–N5 重新定级

定级口径：
- ✅ **DONE** — 目标达成且无残留问题
- ⚠️ **BROKEN** — 代码在主干/分支上，但行为或质量有实证缺陷
- ❌ **NOT STARTED** — 计划要求了，代码里没有
- 🔀 **REWORK** — 分支上有实现，但不能原样合并，须先改

| # | 报告问题 | 原计划判定 | **实测定级** | 关键证据 |
|---|---|---|---|---|
| 1 | 普通 vs 流式入口不对齐 | P0 | ⚠️ **BROKEN** | `_runL1AndL4Preamble` 已共用（`chat_stream_output.py:165`），但 `_preamble_l1_response` / `_preamble_l4_response` 是模块级单例 `ChatService`（`chat.py:39`）上的**可变实例状态**，且不在 `__init__` 初始化 ⇒ 并发下 A 请求可返回 B 的响应 |
| 2 | 短追问/问候误分类 | P0 | ⚠️ **BROKEN** | CHITCHAT 已移到 `hasPriorState` 之后；但 `intent_service.py:580` 用 `<=9`、`:636` 用 `<=10`，注释都写 10（`79d7a5e7` 只改一半）；实测 `'汇总一下今年采购'`（8 字无历史）→ CLARIFY |
| 3 | SessionQueryState 不绑定数据源/用户 | P0 | ⚠️ **BROKEN（最高）** | ORM/迁移/前端已到位，但 `chat_stream.py:802 / 1058 / 1323` 三处写 + `:255` 一处读**漏传 scope**（已实测确认）。流式是默认路径 ⇒ 仍写 NULL scope 行、跨数据源串 state、`.scalar_one_or_none()` 撞 `MultipleResultsFound` → 500 |
| 4 | 会话归属守卫 | P3 | ✅ **DONE** | `3759d85`，`chat.py:54,74,99,193` 全调 `assertSessionOwnership`。**本计划关闭此项** |
| 5 | 快捷 SQL 改写 quote-unaware | P1 | 🔀 **REWORK**（#12） | sqlglot AST 化已做；但无条件 `set limit` 推翻了 LOW-2 安全决策（无行数子句不追加）且无 dialect 参数 ⇒ Oracle 产非法 SQL；`_CLAUSE_FETCH_FIRST` 成死代码 |
| 6 | KPI 用固化 FeatureDefinition | P0 | ❌ **NOT STARTED** | `_executeCalculationLogic`（`chat_service.py:1500,1522`）仍用 `feat.datasource_id` + 固化 `calculation_logic`，**忽略 `dto.datasourceId` 与本轮时间/过滤**。P0-1 验收本应覆盖但未覆盖 |
| 7 | 模型能力路由 + 流式 usage | P1 | 🔀 **REWORK**（#13） | 流式 usage 双收 ✅；能力路由是**哑雷**：`required_capabilities` 全仓 0 调用点，且 `LlmConfig` 无 `capability` 列，一旦传参候选被全滤光 → `NoAvailableModelError`；`cost_source="measured"` 错标流式近似值 |
| 8 | JSON/Decimal/date 序列化 | P0 | ⚠️ **BROKEN** | P0-4 已做 `_jsonSafe` + `allow_nan=False`，但兜底是 `return value` 而 `toSse` 已无 `default=str` ⇒ **`set` / `bytes` 直接 TypeError**，流式中途抛 = 500。`_jsonSafe` 现有 **4 处同名、3 种语义** |
| 9 | 流式绑定会话/轮次 | P1 | 🔀 **REWORK**（#14） | CRLF/EOF ✅；`currentAssistantMessageId` 流式 `onDone` **不清**（注释却称会清）⇒ 下轮把已完成消息误标 `aborted`；`isCurrent()` 在 id 为 null 时放行一切（为迁就 mock 削弱守卫）；`Last-Event-ID` 后端零读取点 |
| 10 | 多步依赖不显式 | P1 | 🔀 **REWORK**（#15） | 无任何生产者填 `depends_on`/`on_failure` ⇒ 依赖图恒空、策略恒默认，全 no-op；`skip` 与 `isolate` 实现逐字相同（3 策略只有 2 行为）；`skipped` 不进 SSE ⇒ 前端 `error=None` 渲染成 **success** |
| 11 | 假设自动验证 / 证据回放 | P1 | 🔀 **REWORK**（#16） | 安全约束守住（不自动执行）✅；但与主干 `research_hypothesis_test.HypothesisVerdict` 四值全同、判据更弱、无 enum 关联；`TurnResultRepository` 全仓孤岛 ⇒ **"证据回放"半边 0 实现**；i18n 6 key 全缺且新测试断言 key 原文 = **假绿** |
| 12 | 预算 / 错误分类不闭环 | P2 | 🔀 **REWORK**（#17） | ErrorType 扩 7 值但只接 2 处、3 处仍硬编码；前端 `case "error"` 丢弃 `d.errorType` ⇒ 零消费；`classifyError` 与 SSOT `isRetryableLlmError` **双写**（两套 status 表、两套 hint 表、cause 深度不同）；`RequestDeadline` 零调用点 |

### 新增发现（原计划未覆盖）

| # | 发现 | 定级 | 证据 |
|---|---|---|---|
| N1b | `chat_multistep.py` 853 → **966** 行（阈值 1000，余 34）；`chat_stream.py` **1332** 行（超出计划 N1 视野，且远超 800 规范）；`chat_service.py` 1987（距 2000 上限余 13） | ⚠️ 未处理 | `wc -l` |
| D1 | **52 条 unit 测试失败**：生产侧自 2026-10-02 起 24 commit，测试侧 **0 commit**。`test_rate_limit.py` 3 条根因已坐实（`limiter._auto_check=False` 全局关停 + 探针 app 未挂 `rateLimitDep`）；`test_chat_service.py` 35 条**断言未坐实** | ⚠️ 未处理 | 各文件最后修改日期对比 |
| D2 | `services/` 下 **122 处** `raise XxxError("中文…")` 硬编码，绕过 `error_messages.py` 的 263 个 SSOT 常量 | ⚠️ 未处理 | grep 计数 |
| D3 | `chat_multistep.py:436-437` **原地给异常对象赋属性**（`exc.tokens_used = …`）后 `raise`，违反不可变约束 | ⚠️ 未处理 | 源码 |
| D4 | `chat_service.py:956` 与 `:1650` 重复同一段 `_featureQueryService` 懒加载 | ⚠️ 未处理 | 源码 |
| D5 | 仓库现存 **4 个陈旧 stash**（`fix/audit-2026-10-12` / `feat/evidence-extension-v31` / `feat/wiki-provenance-p0` / `main`），违反「禁用裸 stash/pop」 | ⚠️ 未处理 | `git stash list` |
| D6 | 测试库 `qa_metadata_test` alembic 停在 `0114`，代码 head 已到 `0124` ⇒ 88 条 unit 测试 setup error | ⚠️ 未处理 | P0 审计实测 |
| N2 | 三份 1012 报告零交叉引用；**且 `QA-1012-01` 的 11 项已由 `90403a24`（PR #9）全部结清但从未回写进 AIChat 计划** —— 这才是 N2 的实际代价。5 份文档全部 untracked | ❌ 未处理 | grep 零命中 + 抽查代码已改 |
| N3 | 数据源切换副作用：前端半边 DONE（`chatStore.ts:489`），后端半边 = **项 3 的漏传** | 合并入项 3 | — |
| N4 | qwen-uplift 回滚干净，`QueryPatternRouter` 已彻底消失，`pc.selected` frozen 机制在用且合规 | ✅ **可关闭** | `query_pattern_router.py` 不存在；`chat_helpers.py:369-374` |
| N5 | `stream_events.py` 的 `default=str` 已被 P0-4 处理 | 合并入**项 8** | — |

**统计：DONE 2 项 / BROKEN 4 项 / NOT STARTED 1 项 / REWORK 6 项（分支上，不可原样合）。**

---

## 二、Phase 0：前置解阻（无此阶段，后续全部无法验收）

### Task 0.1 — 修测试库 alembic 污染
测试库停在 `0114`，代码 head `0124`。**必须真跑 migration，不许 `UPDATE alembic_version` 伪造**（记忆 `qa-system-never-fake-alembic-version-number`：伪造会产生整片假 error）。

```bash
psql ... -d qa_metadata_test -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO qa_user;"
cd backend && DATABASE_URL="postgresql+asyncpg://qa_user:<pw>@localhost:5434/qa_metadata_test" uv run python -m alembic upgrade head
```
- 验收：`SELECT version_num FROM alembic_version` = `0124`；`pytest -m unit --tb=line -q` 无 setup error
- ⚠️ **端口 5433 与 5434 是两个不同的库**，先用 `SELECT inet_server_port(), current_database()` 确认

### Task 0.2 — 取 52 条失败的**真实** traceback
原计划与两轮静态审计都没坐实 `test_chat_service.py` 35 条的失败断言（静态分析子代理产出过虚构结论，已被复核否掉）。

```bash
cd backend && uv run pytest app/tests/unit/test_chat_service.py -q --tb=line > /tmp/tcs.txt 2>&1; tail -50 /tmp/tcs.txt
```
- 验收：一份按「过期替身 / 真实缺陷 / 跨测试污染」三分类的清单，每条附 traceback 行
- **这是 Phase 2 的唯一输入来源。不得再派静态分析代理代替。**

### Task 0.3 — 清理 4 个陈旧 stash
先 `git stash list -p <ref> --stat` 逐个确认内容已被别处覆盖，再 `git stash drop`。**不盲目 apply**（跨分支 apply 风险，见 `qa-system-git-stash-cross-branch-hazard`）。
- 验收：`git stash list` 为空；或每个保留项都写明保留理由

---

## 三、Phase 1：主干生产缺陷修复（项 3 / 6 / 8 / 1 / 2）

顺序按「数据正确性 → 功能正确性 → 崩溃面 → 竞态 → 分类口径」。每项独立分支、独立 TDD。

### Task 1.1（P0）项 3 — 流式漏传 scope ★最高优先
**RED 测试**：`test_chat_stream_query_state_scope.py`
- 走流式主查询成功后，库里该 session 的行 `user_id`/`datasource_id` 非 NULL
- 跨数据源同 sessionId 第二次请求，`classifyAndRecall` 拿不到上轮 state（返 None）
- **反向删守卫自检**：注释掉 scope 参数后上述用例必须变红

**GREEN**：4 处补参数 —— `chat_stream.py:255`（读）、`:802`、`:1058`、`:1323`（写）。
- 同时把 `chat_context.py:525` 的 `.scalar_one_or_none()` 加固：NULL scope 行与 scoped 行并存时不能抛 `MultipleResultsFound`

**验收**：`git grep -c "userId=" backend/app/services/chat_stream.py` 的每个 `_saveQueryState`/`classifyAndRecall` 调用点都带 scope（逐点核对，不看计数）。

### Task 1.2（P0）项 6 — KPI 忽略 `datasourceId` 与本轮时间
`_executeCalculationLogic`（`chat_service.py:1500,1522`）改用 `dto.datasourceId`；固化 `calculation_logic` 需按本轮时间/过滤条件重算或显式声明不支持并降级。

**RED**：`test_kpi_datasource_binding.py` — 用户选 datasource_B 而 KPI 固化在 datasource_A 时，必须走 L2 NL2SQL 而非 L1 直答。
- **连带修正**：`chat_stream_output.py:197` 的 mismatch 判定用 `feat.data_source`(Text, `"采购域"` 这类描述文本) 与 `datasourceId` 做 `str()` 比较，而真正执行用的是 `FeatureDefinition.datasource_id`(int) ⇒ **判定字段 ≠ 执行字段**，判定恒不成立。改为比对 int 字段。

### Task 1.3（P0）项 8 — `toSse` 新崩溃面
`_jsonSafe` 兜底改回 `str(value)`（与 `chat_chart_persist._jsonSafe` 的 doctrine 对齐），并**递归** tuple/set/bytes。

**RED**：`toSse({"k": {1,2}})`、`toSse({"k": b"x"})` 不得抛 `TypeError`。
**顺带**：收敛 4 处同名 `_jsonSafe` → 统一到 `app/utils/json_safe.py` 的 SSOT；修正 `stream_events.py:26` 那句已失效的 docstring。

### Task 1.4（P1）项 1 — preamble 竞态
`_preamble_l1_response` / `_preamble_l4_response` 从实例属性改为**局部变量**：`_runL1AndL4Preamble` 直接返回 `PreambleResult`，调用方就地构造响应，不经实例字段。

**RED**：并发两个请求打同一 `ChatService` 单例，各自拿到自己的响应（构造两个不同答案的假 preamble，断言不串）。

### Task 1.5（P1）项 2 — 意图阈值口径统一
`intent_service.py:580` 与 `:636` 统一到同一常量（`intent_service.py:50` 附近声明，禁内联数字）。
**RED**：`'汇总一下今年采购'`（无历史）必须判 QUERY 而非 CLARIFY。

---

## 四、Phase 2：债务基线（Task 0.2 的产物驱动）

### Task 2.1 — 按三分类逐批修 52 条失败
每批 ≤ 15 条，单批一个 commit，附分类理由。
- **过期替身**：改测试（补 `rateLimitDep` 等真实装配），不改生产
- **真实缺陷**：回到 Phase 1 加固
- **跨测试污染**：加 teardown / 独立进程（记忆 `qa-system-mixed-suite-truncate-hazard`：unit + services + integration 不得同进程）
- 禁止为了让测试变绿而放宽断言；**反向删守卫自检**（`qa-system-same-status-code-false-green`）

### Task 2.2 — 行数红线
- `chat_stream.py` 1332 行：**本计划新增拆分**（原计划完全没提）。按既有 mixin 模式抽 `StreamGuardMixin`（SSE 帧守卫 + 断连兜底）
- `chat_multistep.py` 966 行：新增代码一律进 `multi_step_persist_hooks.py` 等既有模块，不得再涨
- `chat_service.py` 1987 行：硬上限 2000，任何 Task 不得使其增长

### Task 2.3 — 规范欠账
- D2：`services/` 122 处硬编码错误消息分批收敛到 `error_messages.py`（**只收本计划触及的文件**，不做全仓运动）
- D3：`chat_multistep.py:436-437` 改为携带用量的自有异常类型，不原地改异常对象
- D4：`_featureQueryService` 懒加载收敛为单一 helper
- D6：`ChatResponse` 内 `table_option` / `visual_rationale` 改 camelCase（前端契约字段）

---

## 五、Phase 3：#12–#17 逐个 rework 后合并

⚠️ **不得原样合并**。每条分支先 `git rebase --onto origin/main <base> <branch>`，再按下表改，再验收。

⚠️ **migration 必须顺序改号**，否则双 head、`alembic upgrade head` 失败（记忆 `qa-system-alembic-merge-number-collision`）：

| 顺序 | PR | 分支 | migration | 现号（撞车） | 新号 |
|---|---|---|---|---|---|
| 1 | #13 | `feat/aichat-p1-2` | `cost_source` | `0115` ❌ | **0125**（rev 0124） |
| 2 | #16 | `feat/aichat-p1-5` | `hypothesis verification` | `0116` ❌ | **0126**（rev 0125） |
| — | #12 / #14 / #15 / #17 | — | 无 migration | — | — |

### Task 3.1 — #12 `feat/aichat-p1-1`（项 5）
- **改**：恢复 LOW-2 安全决策（无行数子句不追加）；给 sqlglot 传 dialect，或对非 `LIMIT` 方言解析失败即回退两阶段 LLM
- **删**：`_CLAUSE_FETCH_FIRST` 死代码；`_CLAUSE_LIMIT`/`_CLAUSE_ORDER_BY` 若锚点扫描不再用则一并删
- **验收**：Oracle 方言输入不得产出 `LIMIT n`

### Task 3.2 — #13 `feat/aichat-p1-2`（项 7）
- **改**：`LlmConfig` 加 `capability` 列（或删掉 `selectModel` 的该参数）。二选一，**不能留「有参数无字段」的哑雷**
- **改**：`cost_source` 语义 —— 拿不到真实 usage 的流式路径标 `estimated`，不标 `measured`
- **加**：给 `cost_source` 提 SSOT 常量（禁散落字面量）
- **可不做**：能力路由的生产接线（YAGNI）。若做，必须同时改 6 个调用点 + 前端模型配置表单

### Task 3.3 — #14 `feat/aichat-p1-3`（项 9）
- **改**：`onDone` / `onError` 清 `currentAssistantMessageId`；`loadSessionMessages` / `resetSession` / `clearForUserSwitch` / `clearMessages` / `deleteSession` 五条入口也清（记忆 `qa-system-store-flag-clearing-paths`：标志位有多条清除路径，漏一条就是跨会话泄漏）
- **改**：`isCurrent()` 不再在 id 为 null 时放行一切；改测试替身而非削弱守卫
- **删**：`Last-Event-ID`（后端零读取点，是死头）；`aborted` 字段若无渲染消费则删
- **RED**：跨会话回归用例 —— A 会话流式结束后切 B 会话，B 的消息不得被标 `aborted`

### Task 3.4 — #15 `feat/aichat-p1-4`（项 10）
- **决策点（需用户裁定）**：依赖图恒空、策略恒默认 ⇒ 是补生产者（`StepQueryPlanner` 填 `depends_on`），还是砍掉这套 no-op 字段？**YAGNI 倾向后者** —— L1 串行架构下依赖图无来源，留着只是恒真的死逻辑
- 若保留：`skipped`/`skip_reason` 必须进 SSE payload（否则前端渲染成 success）；`skip` 与 `isolate` 要么实现出差异、要么合并成一个值；删掉 `apply_failure` 中从不调用的 `compute_skipped`
- **L1 约束**：`multi_step_plan.py` docstring 的「不引入 DAG」与新模块的 cycle 检测口径矛盾，须择一并在 docstring 说清

### Task 3.5 — #16 `feat/aichat-p1-5`（项 11）
- **并入 SSOT**：删 `VerificationResult`，改用主干 `research_hypothesis_test.HypothesisVerdict`（四值已存在且更完备）
- **补 i18n**：6 个 `chat.hypothesis.status.*` key；**并修正新测试** —— 现测试断言 key 原文（`getByText(/supported/i)` 命中 `chat.hypothesis.status.supported`）是**假绿**
- **决策点**：证据回放（`TurnResultRepository` + `ReportRenderer`）全仓零调用 ⇒ 补接线，还是本次整块删除后另起任务？

### Task 3.6 — #17 `feat/aichat-p2-1`（项 12）
- **合 SSOT**：`classifyError` 不得与 `isRetryableLlmError` 双写 —— 重试决策唯一走 `isRetryableLlmError`；`classifyError` 若保留只做展示分类，且应**复用**重试策略的 hint/status 常量
- **补齐接线**：`chat_stream.py` 剩余 3 处硬编码（`:150`、`:164`、`:269`）改走 `classifyError`
- **前端消费**：`chat.ts` 的 `case "error"` 必须读 `d.errorType`，否则细分类零价值
- **决策点**：`RequestDeadline` 零调用点 —— 接进 `chat_service` 入口并实现 `remaining_token`/`remaining_cost` 共享，还是本任务删除？
- **删**：`_PERMISSION_HINTS` 死常量

---

## 六、Phase 4：文档与记忆收尾

### Task 4.1 — N2 回写
建 `docs/superpowers/specs/1012-code-review-cross-refs.md`：三份报告交叉引用矩阵 + **QA-1012-01 的 11 项「已由 `90403a24` 结清」回写进 AIChat 计划 P0 清单**（这是 N2 的实际代价）。
- 5 份报告/计划当前全是 untracked，本任务一并 `git add`

### Task 4.2 — 关闭已澄清的项
- **项 4 / N4**：在文档中标注「已闭合 + 闭合证据」，不再留作待办
- **P1-3 记忆**：`currentAssistantMessageId` 是 `qa-system-store-flag-clearing-paths` 的第 6 条路径
- **P1-5 记忆**：`HypothesisVerdict` 是判定 SSOT，第二套实现应并入
- **P1-3 i18n 假绿**：归入 `qa-system-false-green-test-commands`

---

## 七、Gate 汇总表

| Gate | 判定 | 命令/依据 | 阻塞谁 |
|---|---|---|---|
| G0.1 测试库干净 | 必须绿 | `alembic_version` = `0124`；`pytest -m unit` 无 setup error | 全部 |
| G0.2 traceback 取证 | 必须完成 | Task 0.2 清单入档 | Phase 2 |
| G1.1 流式 scope | 必须绿 | Task 1.1 RED 用例 + 反向删守卫自检 | Phase 3 全部 |
| G1.2 KPI 绑数据源 | 必须绿 | Task 1.2 RED 用例 | 发版 |
| G1.3 `toSse` 不抛 | 必须绿 | set/bytes 用例 | Phase 3-#14 |
| G1.4 无并发竞态 | 必须绿 | Task 1.4 并发用例 | 发版 |
| G2.1 52 条清零 | 必须绿 | `pytest -m unit -q` 0 failed | 发版 |
| G2.2 行数不增 | 必须绿 | `chat_service.py` ≤ 2000；`chat_multistep.py` ≤ 1000 | 发版 |
| G3.x 每 PR | 逐个绿 | 该 PR 自带测试 + 前述 G1.x | 合入 |

## 八、协调点（必须沟通）

1. **Task 3.4 / 3.5 / 3.6 各有一个「补接线 vs 砍死代码」的决策点** —— 需用户裁定，不能由实现代理自行决定（三个都偏向 YAGNI 砍掉，但砍掉意味着放弃原计划的部分目标）
2. **Task 3.5 的 i18n 假绿**：现测试断言的是 key 原文，改测试会让 diff 看起来像「把绿改成红」—— 需说明这是修正假绿
3. **Task 0.3 stash 清理**：4 个 stash 可能含未入库工作，drop 前须确认
4. **prod `qa_metadata` 版本/漂移**：`alembic_version=0116` 但 schema 含 0118/0120/0121 产物 —— 既有隐患，非本计划引入，需用户定夺是否单独处理

## 九、回滚预案

| 变更 | 回滚方式 | 数据影响 |
|---|---|---|
| Task 1.1 补 scope | 单 commit revert | 已有 NULL scope 行需一次性 `UPDATE` 回填，否则 revert 后状态机与新代码不一致 |
| Task 1.3 `_jsonSafe` 收敛 | revert | 无 |
| Task 3.x 各 PR | PR revert（尚未发版则直接 close） | migration 号已进主干则**不可 revert**，只能前滚 |
| Task 2.3 D6 改 camelCase | revert + 前端同步 | 前端 JSON 契约变更，需前后端同批发 |

**硬规则**：migration 一旦合入主干就**不可回滚**，只能前滚修复。Task 3.2 / 3.5 的 migration 必须先在测试库跑通完整 `upgrade` → `downgrade` → `upgrade` 三轮。

## 十、Self-Review Checklist

开工前逐条确认：

- [ ] Task 0.1 未使用 `UPDATE alembic_version` 伪造版本号
- [ ] `alembic upgrade` 显式覆盖 `DATABASE_URL` 指向 `qa_metadata_test`，**未裸跑**（`env.py:52` 会打生产库）
- [ ] 端口已用 `SELECT inet_server_port()` 确认（5433/5434 是两个库）
- [ ] Task 0.2 的失败分类来自真实 traceback，不是静态推断
- [ ] 每个 RED 测试都有**反向删守卫自检**（删掉守卫必须变红）
- [ ] 未出现「为让测试变绿而放宽断言」
- [ ] 未在 `raw/` 下改动任何文件
- [ ] 错误文案来自 `error_messages.py`，非硬编码中文
- [ ] 无裸 `git stash` / `git stash pop`
- [ ] `chat_service.py` 未增长
- [ ] 三个「补接线 vs 砍死代码」决策点已获用户裁定并写入 ledger
- [ ] migration 改号按 0125 → 0126 顺序，未并行
- [ ] 每个新 error code / i18n key 前后端同批扩展（union / i18n / store 三处）

---

## 附：审计方法论警告

本轮三份审计中，静态分析子代理多次产出**看似详尽但经复核不成立**的结论（虚构 conftest RuntimeError、虚构缺失方法、虚构 kwarg 漂移、失败数按给定总数反推凑数）。

**已复核否掉的具体错误断言**（留档以免重犯）：
- 「`ChatService` 缺 `_kpiMatcher`」→ 假，`chat_service.py:342-345` 有默认兜底
- 「`generateValidatedPlan` 新增 kwargs 导致漂移」→ 假，全是带默认值 keyword-only
- 「`_getClassFilterMaxClasses` 等 4 方法已不存在」→ 假，存在于 `chat_recall.py:223/261/322/376`

**规则**：静态审计只用来**缩小假设空间**，任何「X 是坏的」结论在进入计划前必须有一次真跑或逐行源码核验。