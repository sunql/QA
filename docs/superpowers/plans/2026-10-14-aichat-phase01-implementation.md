# AIChat 缺口 Phase 0 + Phase 1 实现计划

- 日期：2026-10-14
- 上游：[2026-10-14-aichat-gap-closure.md](./2026-10-14-aichat-gap-closure.md)（审计与定级）
- 前序：[2026-10-13-aichat-service-priority.md](./2026-10-13-aichat-service-priority.md)（12 项 + N1–N5）
- 基线：`origin/main` = `b81bf1e0`

## 本轮范围（用户裁定）

| 项 | 裁定 |
|---|---|
| 本轮做到哪 | **Phase 0 + Phase 1** —— 修完主干 4 类生产缺陷，可独立发版 |
| Phase 3（#12–#17） | **全部补接线**（不做 YAGNI 砍除）。下一轮执行，本轮只锁定改号顺序 |
| 4 个陈旧 stash | 已核实覆盖并清理；stash1 独有内容已抢救（见 Task 0.4） |

**不在本轮**：Phase 2（52 条测试债 + 行数拆分 + 规范欠账）、Phase 3、Phase 4。
Phase 2 的取证工作（Task 0.2）在本轮做，因为它同时充当 Phase 1 的**改前基线**。

---

## 0. 本轮要修的 5 件事

| 编号 | 缺陷 | 症状（用户可见） | 严重度 |
|---|---|---|---|
| **F1** | 项3 流式漏传 scope（`chat_stream.py:255` 读 / `:802` `:1058` `:1323` 写） | 切换数据源后旧会话状态串味；NULL 行与 scoped 行并存时 500 | **P0 数据正确性** |
| **F2** | 项6 KPI 用固化 FeatureDefinition | 用户选的数据源与 KPI 实际查询的数据源不一致 | **P0 功能正确性** |
| **F3** | 项8 `toSse` 去掉 `default=str` 后 set/bytes 抛 TypeError | 流式回答中途 500 | **P0 崩溃** |
| **F4** | 项1 preamble 响应存在 `ChatService` 单例可变实例状态 | 并发下 A 用户看到 B 用户的回答 | **P1 竞态** |
| **F5** | 项2 意图长度守卫 9/10 口径不一 | `'汇总一下今年采购'` 被推去追问澄清 | **P1 分类** |

F4 是安全性质的（跨用户数据泄漏），虽排第四但不可降级。

---

## 1. 分支策略

全部从 `origin/main` 切，**不用 local main**（冻在 `08b3ae5a`，含 127 个研究线 commit——记忆 `qa-system-branch-lineage-and-base`）。

| Task | 分支 | 触及文件 |
|---|---|---|
| 0.1 | （无分支，直接修测试库） | — |
| 0.2 | （无分支，取证） | — |
| 1.1 F1 | `fix/aichat-gap-f1-stream-scope` | `chat_stream.py` `chat_context.py` |
| 1.2 F2 | `fix/aichat-gap-f2-kpi-datasource` | `chat_stream_output.py` `chat_service.py` |
| 1.3 F3 | `fix/aichat-gap-f3-sse-encoding` | `stream_events.py` `utils/json_safe.py` `chat_chart_persist.py` |
| 1.4 F4 | `fix/aichat-gap-f4-preamble-race` | `chat_stream_output.py` `chat_service.py` `chat_stream.py` |
| 1.5 F5 | `fix/aichat-gap-f5-intent-threshold` | `intent_service.py` |

**冲突矩阵**（决定串行顺序）：

| | chat_stream | chat_service | chat_stream_output | 其他 |
|---|---|---|---|---|
| F1 | ✅ | | | chat_context |
| F2 | | ✅ | ✅ | |
| F3 | | | | ✅ 独立 |
| F4 | ✅ | ✅ | ✅ | |
| F5 | | | | ✅ 独立 |

- **F1 → F2 → F4** 必须串行（逐级叠加，冲突面递增）
- **F3、F5** 与其余三者文件不相交，可**并行切分支**，但**串行合并**

## 2. 执行序

```
0.1 修测试库 ──┬─→ 0.2 取基线 ──┬─→ 1.1 F1 ──→ 1.2 F2 ──→ 1.4 F4 ──┐
               │                │                                    ├─→ 终验 ──→ 发版
               │                └─→ 1.3 F3 ─────────────────────────┤
               └─→ 1.5 F5 ─────────────────────────────────────────┘
0.4 stash 抢救（已并行完成）
```

---

## Phase 0：解阻（不改产品代码）

### Task 0.1 — 修测试库 alembic 污染 【阻塞全部后续】

**问题**：`qa_metadata_test` 的 `alembic_version` 停在 `0114`，代码 head 已到 `0124` ⇒ 88 条 unit 测试 setup error（`multi_step_run_session_id_fkey does not exist`）。

**严禁** `UPDATE alembic_version` 伪造版本号——记忆 `qa-system-never-fake-alembic-version-number` 记录了这么干会造出整片假 error 并白费两轮 19 分钟的重跑。

**严禁**裸跑 `alembic upgrade head`——`alembic/env.py:52` 用 `settings.databaseUrl` 即 `.env` 的 `DATABASE_URL`，**直接打生产库 `qa_metadata`**（记忆 `qa-system-alembic-targets-prod`）。

```bash
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system

# 0.1a 先确认连的是哪个库（5433 与 5434 是两个不同的库；服务端真实端口是 5432）
psql "postgresql://qa_user:<pw>@localhost:5434/qa_metadata_test" \
  -c "SELECT current_database(), inet_server_port();"

# 0.1b 重建（该库只含 fixture，可完全重建）
psql "postgresql://qa_user:<pw>@localhost:5434/qa_metadata_test" \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO qa_user;"

# 0.1c 显式覆盖 URL 真跑 migration
cd backend && DATABASE_URL="postgresql+asyncpg://qa_user:<pw>@localhost:5434/qa_metadata_test" \
  uv run python -m alembic upgrade head
```

**验收 Gate 0.1**
- [ ] `SELECT version_num FROM alembic_version` = `0124`
- [ ] `uv run pytest -m unit -q --tb=no 2>&1 | tail -3` — **0 error**（failed 允许存在，是 Task 0.2 的对象）
- [ ] 生产库未被触碰：`psql .../qa_metadata -c "SELECT version_num FROM alembic_version"` 与操作前一致

### Task 0.2 — 取 unit 失败的真实 traceback 【Phase 1 的回归基线】

**为什么必须真跑**：两轮静态审计都没坐实 `test_chat_service.py` 的失败断言，静态分析子代理产出过虚构结论（虚构 conftest RuntimeError、虚构缺失方法、失败数按给定总数反推凑数）。

```bash
cd backend && uv run pytest -m unit -q --tb=line > /tmp/unit-baseline.txt 2>&1
tail -40 /tmp/unit-baseline.txt          # ⚠️ 不要用 tail -15，会丢掉完整失败列表
```

**产出**：一份基线清单（失败用例名 + 一行 traceback + 三分类：过期替身 / 真实缺陷 / 跨测试污染），存入本计划 SDD 工作区。

**验收 Gate 0.2**
- [ ] `/tmp/unit-baseline.txt` 完整保存，失败用例总数与 summary 行一致
- [ ] 基线清单入档，且**在改任何代码之前**产出

⚠️ 这份基线是 Phase 1「未引入新失败」的唯一判据。**没有它，1.1–1.5 的验收都不可信。**

### Task 0.4 — stash1 独有内容抢救（已完成）

`stash@{1}` 的 `test_id_mapping_api.py`（316 行）含 main 上没有的**输入校验 422 边界测试**（`limit=10000`、非法 unified_id 格式、缺字段）。main 上同名文件仅 161 行，只有 happy path。

已抢救至 `.superpowers/sdd/2026-10-13-aichat-service-priority/recovered/id_mapping_api_stash_tests.py`。

⚠️ 该文件是 2026-09-28 的，对应当年 API 形态（测 `count` 端点，而 main 的 `id_mapping.py` 无此路由）。**本轮不移植**，列入 Phase 2 待办（补输入校验边界测试，符合「系统边界必须校验输入」约束）。

其余 3 个 stash（`rate_limit.py` / `main.py` routers / 前端两个页面）已逐项核实被 main 覆盖并 drop，无内容损失。

---

## Phase 1：五个缺陷修复

每个 Task 独立分支，TDD（RED → GREEN → IMPROVE），完成后过 task review。

### Task 1.1 【F1 / P0】流式路径补齐 SessionQueryState scope

**缺陷**：`chat_stream.py` 的 `_saveQueryState` 调用点中，L567/L608 带了 `userId`/`datasourceId`，**L802（主查询成功）/ L1058（多步聚合成功）/ L1323（断连兜底）没带**；`classifyAndRecall` 的 L255 也没带。而流式是**默认路径**。

**后果**：
1. 三条写入路径产生 `user_id`/`datasource_id` 为 NULL 的行
2. 下轮追问按 scope 过滤时 NULL ≠ uid ⇒ 读不到自己的 state ⇒ 追问丢失锚点（即项 3/N3 的原始问题**在默认路径上没修掉**）
3. 同一 `session_id` 下 NULL 行与 scoped 行并存时，`chat_context.py:525` 的 `.scalar_one_or_none()` 抛 `MultipleResultsFound` → **500**

**RED 测试**（新建 `backend/app/tests/integration/test_chat_stream_query_state_scope.py`）：
1. 走流式主查询成功后，库中该 session 行的 `user_id`/`datasource_id` **非 NULL**
2. 跨数据源同 sessionId 第二次请求，`classifyAndRecall` 的 `state` 为 `None`
3. 预置一条 scoped 行 + 一条 NULL 行（人工 insert）后，读路径不抛异常且返回 scoped 行

**反向删守卫自检**：注释掉这 4 处的 scope 参数，上述 3 条必须全部变红——这是「是谁拒的」的必要证据（记忆 `qa-system-same-status-code-false-green`）。

**GREEN**：
- `chat_stream.py:255` 补 `userId=user.id, datasourceId=dto.datasourceId`
- `chat_stream.py:802` / `:1058` / `:1323` 各补两参
- `chat_context.py` 的读取加健壮性：scope 行存在时优先返回 scope 行，NULL 行不参与竞争（不可用 `scalar_one_or_none` 的裸语义）

**禁止**：不得为了让它变绿而放宽断言；不得在测试里直调 service 绕过 API 链路（约束：真实 PostgreSQL + 完整 API 链路）。

**验收 Gate 1.1**
- [ ] 3 条新用例绿
- [ ] 反向删守卫自检：变红
- [ ] **逐点核对**（不看计数）：`grep -n "_saveQueryState(" backend/app/services/chat_stream.py` 的每个调用点，后续 12 行内都有 `userId` 与 `datasourceId`
- [ ] `pytest -m integration` 无新增失败（对比 Gate 0.2 基线）
- [ ] `chat_service.py` 行数未增长

---

### Task 1.2 【F2 / P0】KPI 绑当前数据源

**缺陷**：`_executeCalculationLogic`（`chat_service.py:1500,1522`）用 `feat.datasource_id` + 固化 `calculation_logic`，**完全忽略 `dto.datasourceId` 与本轮时间/过滤条件**。

**连带缺陷**：`chat_stream_output.py:197` 的 mismatch 判定用 `feat.data_source`（`KpiCatalog` 的 `Text` 列，存的是 `"采购域"` 这类描述文本）与 `dto.datasourceId` 做 `str()` 比较，而真正执行查询用的是 `FeatureDefinition.datasource_id`（`int`）⇒ **判定字段 ≠ 执行字段**，判定恒不成立。现有测试 `test_stream_kpi_datasource_mismatch_warns` 是**因错误原因而绿**。

**RED 测试**（新建 `backend/app/tests/unit/test_kpi_datasource_binding.py`）：
1. 用户选 datasource_B，KPI 固化在 datasource_A ⇒ **不得**走 L1 直答，须降级 L2 NL2SQL
2. mismatch 判定改为比对 `int` 字段后，跨数据源场景能触发
3. **反向自检**：把判定改回 `data_source` 字符串比较，用例 1 必须变红

**GREEN**：
- `_executeCalculationLogic` 改用 `dto.datasourceId`；固化 `calculation_logic` 若不支持本轮时间/过滤条件，显式降级而非静默套用
- mismatch 判定改比 `FeatureDefinition.datasource_id`（int）

**验收 Gate 1.2**
- [ ] 新用例绿 + 反向自检变红
- [ ] 现有 `test_stream_kpi_datasource_mismatch_warns` 仍绿（若它断言的是错误原因，须一并修正并在报告里说明）
- [ ] 无新增失败

---

### Task 1.3 【F3 / P0】`toSse` 不再因 set/bytes 崩溃

**缺陷**：`stream_events.py:91-98` 的 `toSse` 去掉了 `default=str`，而 `_jsonSafe`（`:19-46`）的兜底是 `return value` 原样透传 ⇒ `set` / `bytes` 让 `toSse()` **直接抛 TypeError**。`toSse` 在生产有 9 个调用点，流式中途抛异常 = 一次已进行一半的流被打成 500。

另外：
- `stream_events.py:26` 的 docstring 写着「其他 → 原样（让 json.dumps 的 **default=str** 兜底）」——该行为已不存在，注释是错的
- `_jsonSafe` 这个名字全仓 **4 处定义、3 种语义**（`stream_events.py` 原样透传 / `chat_chart_persist.py:105` str 化 / `multi_step_compressor.py:112` 委托 SSOT / `research_session_service.py:70` 的 `_coerceJsonSafe`）

**RED 测试**（新建 `backend/app/tests/unit/test_stream_events_safe.py` 用例）：
1. `toSse(StreamEvent(EVENT_META, {"k": {1, 2}}))` 不抛 `TypeError`
2. `toSse(StreamEvent(EVENT_META, {"k": b"x"}))` 不抛 `TypeError`
3. `set` / `tuple` / `bytes` 嵌在 list / dict 深层时同样不抛（**递归**，当前 `:44-45` 只处理 list）

**GREEN**：
- `_jsonSafe` 兜底改回 `str(value)`，与 `chat_chart_persist._jsonSafe` 已声明的 doctrine 对齐（「宁可留个可读痕迹，也不静默丢值」）
- 递归处理 tuple / set / bytes
- 修正 docstring
- 收敛 4 处同名 → 统一委托 `app/utils/json_safe.py` 的 SSOT。**若收敛会牵动研究线的 `_coerceJsonSafe`（Decimal→float、未知类型静默字符串化，语义不同），则本轮只收敛 chat 侧三处，研究线另开任务并在报告里说明**

**验收 Gate 1.3**
- [ ] 新用例绿（含深层嵌套）
- [ ] `Decimal('1.10')` 仍保真为 `"1.10"`、`+Inf`/`NaN` 仍编码为 `{"__non_finite__": ...}`（P0-4 的既有语义不得回退）
- [ ] 反向自检：把兜底改回 `return value`，新用例必须变红

---

### Task 1.4 【F4 / P1，安全】消除 preamble 响应竞态

**缺陷**：`ChatService` 在 `chat.py:39` 是**模块级单例** `_service = ChatService()`。mixin 在 `chat_stream_output.py:204` / `:222` 写 `self._preamble_l1_response` / `_preamble_l4_response`，`chat_service.py:429/432` 读回。这两个属性**不在 `__init__` 中初始化**（全仓 0 处定义）。

并发下：A 请求写完 preamble → 在 `_storeSessionMessages` 处让出事件循环 → B 请求覆写该字段 → A 读到 B 的响应。**跨用户数据泄漏**。

这是 P0-1 新引入的（原内联实现无此共享状态）。

**RED 测试**（新建 `backend/app/tests/unit/test_preamble_concurrency.py`）：
1. 构造两个返回不同答案的假 preamble，并发发起两个请求，断言各自拿到自己的响应
2. `ChatService()` 实例上**不存在** `_preamble_l1_response` / `_preamble_l4_response` 属性（反向断言：实例上无跨请求可变状态）

**GREEN**：`_runL1AndL4Preamble` 直接返回携带响应的 `PreambleResult`（已有该 frozen dataclass），调用方就地构造，不经实例字段。

**验收 Gate 1.4**
- [ ] 新用例绿
- [ ] `grep -n "_preamble_l1_response\|_preamble_l4_response" backend/app/` **零命中**
- [ ] 并发用例在修复前必须失败（反向自检）

---

### Task 1.5 【F5 / P1】统一意图长度守卫口径

**缺陷**：`intent_service.py:580`（有 prior 分支）用 `len(stripped) <= 9`，`:636`（无 prior 分支）用 `<= 10`，**两处注释都写「≤ 10 字」**。commit `79d7a5e7` 只改了一半。

实测误判：`'汇总一下今年采购'`（8 字，无历史）→ CLARIFY，而它是带明确业务动作的查询问法。

**RED 测试**（扩充 `backend/app/tests/unit/test_intent_classify_order.py`）：
1. `'汇总一下今年采购'`（无历史）判 QUERY，不是 CLARIFY
2. `'汇总'`（2 字）仍判 CLARIFY（不能矫枉过正）
3. 两处长度守卫引用**同一个常量**（断言常量存在且两处调用点用它，而非字面量）

**GREEN**：`intent_service.py:50` 附近声明具名常量（禁内联魔数），`:580` 与 `:636` 都引用它。

**验收 Gate 1.5**
- [ ] 新用例绿
- [ ] `grep -nE "len\(stripped\) <= [0-9]" backend/app/services/intent_service.py` **零命中**
- [ ] 既有 68 条 `test_intent_service.py` + 4 条 `test_intent_supplier_360.py` 仍绿

---

## 3. 终验与发版

全部 5 个 Task 合并后：

```bash
# 后端 unit + integration 必须不同进程（记忆 qa-system-mixed-suite-truncate-hazard）
cd backend
uv run pytest -m unit -q --tb=line > /tmp/unit-after.txt 2>&1
uv run pytest -m integration -q --tb=line > /tmp/integ-after.txt 2>&1

# 逐条对比 Gate 0.2 基线：失败数不增，且无新增失败用例名
diff <(grep -oE "^FAILED [^ ]+" /tmp/unit-baseline.txt | sort) \
     <(grep -oE "^FAILED [^ ]+" /tmp/unit-after.txt | sort)
```

**发版 Gate**
- [ ] 5 个 Task 各自的 Gate 全绿
- [ ] unit 失败用例集合 ⊆ 基线集合（无新增）
- [ ] integration 无新增失败、无 error
- [ ] `chat_service.py` ≤ 2000 行（当前 1987，余 13）
- [ ] `chat_multistep.py` ≤ 1000 行（当前 966，余 34）
- [ ] 错误文案来自 `error_messages.py`，本轮未新增硬编码中文
- [ ] 无裸 `git stash` / `git stash pop`
- [ ] `raw/` 目录零改动

---

## 4. 本轮不做（留给下一轮）

| 项 | 归属 | 备注 |
|---|---|---|
| Phase 2：52 条测试债 | Phase 2 | Task 0.2 的基线清单是它的输入 |
| Phase 2：`chat_stream.py` 1332 行拆分 | Phase 2 | 原计划完全没提，是本轮新发现 |
| Phase 2：D2/D3/D4/D6 规范欠账 | Phase 2 | 122 处硬编码错误消息等 |
| Phase 2：stash1 的 422 边界测试移植 | Phase 2 | 已抢救，见 Phase 0 Task 0.4 |
| Phase 3：#12–#17 | Phase 3 | **已裁定「全部补接线」**；本轮只锁定改号顺序 #13→`0125` 先于 #16→`0126` |
| Phase 4：1012 报告交叉引用 | Phase 4 | 顺带回写「QA-1012-01 的 11 项已由 `90403a24` 结清」 |
| prod `qa_metadata` 版本漂移 | 待用户定夺 | `alembic_version=0116` 但 schema 含 0118/0120/0121 产物，非本轮引入 |

## 5. Phase 3 裁定备忘（本轮不执行，供下一轮开工时读）

用户裁定 **全部补接线**，因此以下「砍死代码」的倾向作废，改为补齐：

| 分支 | 必须补的接线 |
|---|---|
| #13 `feat/aichat-p1-2` | `LlmConfig` 加 `capability` 列 + 6 个 `selectModel` 调用点传参 + 前端模型配置表单 |
| #15 `feat/aichat-p1-4` | `StepQueryPlanner` 产出 `depends_on`/`on_failure`；`skipped`/`skip_reason` 进 SSE；`skip` 与 `isolate` 实现出差异；删 `apply_failure` 中死函数 `compute_skipped` |
| #16 `feat/aichat-p1-5` | `TurnResultRepository` 接进 `ReportRenderer` 实现证据回放；i18n 6 key；**并修正断言 key 原文的假绿测试** |
| #17 `feat/aichat-p2-1` | `RequestDeadline` 接进 `chat_service` 入口 + 实现 `remaining_token`/`remaining_cost` 共享；前端 `chat.ts` 读 `d.errorType`；`chat_stream.py` 剩余 3 处硬编码改走 `classifyError` |

仍然要做（与裁定无关，是正确性问题）：`classifyError` 并入 `isRetryableLlmError` SSOT、`VerificationResult` 并入 `HypothesisVerdict`、补齐 LLM 配置与路由的字段契约。

---

## 6. 风险与回滚

| 变更 | 回滚 | 数据影响 |
|---|---|---|
| 1.1 补 scope | commit revert | 已产生的 NULL scope 行需一次性 `UPDATE` 回填，否则 revert 后状态机与代码不一致 |
| 1.3 收敛 `_jsonSafe` | revert | 若牵动研究线则只回滚 chat 侧三处 |
| 1.4 改 preamble 返回 | revert | 无 |
| 0.1 测试库重建 | 无需回滚 | **只影响测试库**；生产库不得触碰 |
| Phase 3 的 migration | 不可 revert | 一旦进主干只能前滚；改号须按 0125 → 0126 顺序，否则双 head |

**硬规则**：`alembic upgrade` 必须显式覆盖 `DATABASE_URL` 指向 `qa_metadata_test`。`env.py:52` 会用 `.env` 的生产 URL。