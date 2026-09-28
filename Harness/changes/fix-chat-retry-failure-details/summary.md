# 变更：fix-chat-retry-failure-details

- **日期**：2026-09-26
- **作者**：Claude / 启琳
- **Phase**：Phase 4 对话服务健壮性（`app/services/chat_service.py`）
- **状态**：done
- **关联变更**：[fix-multistep-failure-isolation](../fix-multistep-failure-isolation/summary.md)（C3/C4，同一函数的前一批）、[fix-llm-metering-blindspots](../fix-llm-metering-blindspots/summary.md)（H1/H2/H8/H9，同一约束）、[fix-c-fallback-global-filters](../fix-c-fallback-global-filters/summary.md)（P0 第 5 项，本批的前一批）
- **迁移版本**：无
- **SSOT 出处**：`Harness/wiki/chat-service-assessment.md` §2.3 **M7** + §9 遗留 1 + §10 遗留 1（评估为 2026-09-25，M7 是 P0 清空后与该主题最相关的下一项）

---

## 1. 需求

`_runQueryWithRetry`（`chat_service.py`）在首次 SQL 执行失败后，把执行错误回灌给
`generateSql` **重试一次**。二次失败（重试生成失败 / 重试执行仍失败）此前只有一行
`logger.warning`，三件事随之丢失：

| # | 影响 | 具体表现 |
|---|---|---|
| 1 | **用户可见的病因是误导性的** | 步骤文案（`StepResult.error` → 非流式响应字段 + 流式 `step_result` 事件，前端直接渲染）只印**第一次**的错误。用户读到「该步骤执行失败：ORA-00942 表或视图不存在」，会得出「这条 SQL 一上来就写错了」；真相是「首次错了、回灌重试**同样**错」。两句结论指向的排查方向完全不同（改本体映射 vs 改提示词/模型） |
| 2 | **没有可复现的记录** | 二次失败原因只在日志里一行（异常对象未挂载）；**重试那条 SQL 全文此前无处可查** —— 用户报「提示重试失败但看不到它试了什么」时无从应答（重试会重新生成 SQL，与首次那条可能不同） |
| 3 | **单步路径漏账** | 重试那次生成**确实调了 LLM**，但单步两条路径（非流式 `_processQuery` / 流式 `_streamQuery`）拿到异常后直接上抛/转 error 事件，从未取 token 记账。多步路径已由 C3 那批修好 ⇒ 违反核心约束 #3「每次 LLM 调用必须记录 Token 消耗与成本」在单步路径上不成立 |

**验收标准**：①二次失败时步骤文案同时给出两段原因，且两段都经脱敏（不得带出 SQL 全文
与参数值）；②重试那一次的生成 token 在**四种组合**（流式/非流式 × 回退多步/硬失败）
下都落 `session_token_usage` 台账并计入响应总额；③异常类型与消息对外保持逐字不变
（API 层按异常类型映射 HTTP 状态，改类型会连带改变对外契约）。

## 2. 设计评审

### 2.1 二次失败详情怎么带出去

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 包装成新异常 | `raise RetryFailedError(first, second) from firstErr` | ❌ **改异常类型**：`app/api/v1/chat.py` 按类型映射 HTTP 状态与错误码，新增类型要么走默认 500 要么得同步改 API 层 —— 一个可观测性改进不该动对外契约 |
| B 拼进原异常消息 | `firstErr.args = (f"{first}；重试：{second}",)` | ❌ 同样改**对外消息**（`_userFacingErrorText` 读 `str()`，用户会看到两段拼在一句里，且脱敏与截断的边界变得不可控）；`raise firstErr from genErr` 只填 `__cause__`，文案<b>不变</b>，等于没修 |
| C 挂私有属性 + 文案层拼装（**选定**） | `_attachRetryFailure(exc, stageLabel, error)` 只 `setattr`；`_stepFailedError` 读它并拼「首次：…；重试…：…」 | ✅ 异常类型/消息逐字不变（契约稳），失败的**呈现**与**携带**解耦；沿用本仓既有模式（`_attachRetryGenTokens`、`Nl2SqlError.tokens`）—— 不引入新机制 |

### 2.2 重试生成失败的 token 怎么取（复审 MEDIUM-1）

第一轮复审指出：本批只修了「重试执行也失败」分支，**重试生成自己失败**的分支仍丢账 ——
`generateSql` 把已消耗 token 挂在 `Nl2SqlError.tokens` 上交回（它自己不落账），但那个
`except` 里没人读。修法有两条：

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 就地读属性 | `_attachRetryGenTokens(firstErr, getattr(genErr, "tokens", None) or (0, 0))` | ⚠️ 可用，但把「哪些异常可计量」这一知识又抄了一份 |
| B 复用既有提取器（**选定**） | `_attachRetryGenTokens(firstErr, self._consumedTokens(genErr))` | ✅ 与 `_callWithFallback` 读同一份知识（`isinstance(exc, Nl2SqlError) and exc.tokens is not None` 否则 (0,0)）；零用量时 `_accountRetryGenUsage` 返回 None ⇒ **不会写 0-token 假审计行** |

### 2.3 两段文案的截断策略（复审 LOW-3）

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 整体尾部截断（原实现） | 拼完两段后统一 `_clipText(…, 200)` | ❌ **实测**：首次原因是 500 字时，200 字上限全被首次原因占满，重试原因**整段消失** —— 恰好把本批要暴露的信息吃掉（又回到「只报第一次」） |
| B 两段各自限量（**选定**） | 每段 `_clipText(…, _STEP_FAILED_SEGMENT_LIMIT=90)`，拼完再套总上限 200 | ✅ 上限仍是硬的（`首次：`3 + 90+3 + `重试后仍执行失败：`9 + 90+3 = 198 ≤ 200`），但两段都保证可见；段内截断发生在**脱敏之后** ⇒ 不会截出半个 `[SQL:` 标记泄漏内部信息 |

## 3. 数据模型变更

无。复用既有 `session_token_usage`（`purpose="nl2sql"`）与 `Nl2SqlError.tokens` 契约，
无迁移。

## 4. 接口契约变更

**无字段/类型/状态码变更**；变的是用户可见文案与台账行数（这正是本次目的）：

| 面 | 修复前 | 修复后 |
|---|---|---|
| `StepResult.error`（首次失败 + 重试执行也失败） | `该步骤执行失败：ORA-00942: 表或视图不存在` | `该步骤执行失败：首次：ORA-00942: 表或视图不存在；重试后仍执行失败：ORA-00904: 标识符无效` |
| `StepResult.error`（首次失败 + 重试**生成**失败） | 同上（只报首次） | `…；重试生成失败：无法生成有效的查询 SQL，请换一种问法或补充本体元数据` |
| `StepResult.error`（未触发重试 / 重试成功） | `该步骤执行失败：<原因>` | **逐字不变**（向后兼容，有用例钉住） |
| 异常类型与消息 | — | **逐字不变**（`_attachRetryFailure` / `_attachRetryGenTokens` 只 `setattr`，有用例断言 `type(exc)` 与 `str(exc)`） |
| 台账 / 响应 `tokens`·`cost` | 单步路径漏「重试生成」那一行 | 如实多一行并计入总额（金额方向是**变多**——此前少算，非新增消耗） |

新增的服务端日志：二次失败的 `logger.warning(..., exc_info=True)`；重试执行仍失败时
额外打印**重试 SQL**（`_clipText(sql, _RETRY_SQL_LOG_LIMIT=2000)`，仅服务端）。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `app/services/chat_service.py` | ①`_stepFailedError` 改为两段拼装（各自脱敏 + 各自限量）；②新增 `_STEP_FAILED_SEGMENT_LIMIT`；③新增 `_RETRY_FAILURE_ATTR` / `_RetryFailure(stageLabel, error)` / `_attachRetryFailure` / `_retryFailure`；④`_runQueryWithRetry` 两个二次失败分支分别挂详情与用量（生成失败分支补 `_consumedTokens(genErr)`）+ 重试 SQL 进日志；⑤新增 `_accountRetryGenUsage`（取用量 → 落账 → 返回增量），单步两处（`_processQuery` / `_streamQuery`）与多步 `_executeDataStep` 共用；⑥`_attachRetryGenTokens` / `_accountRetryGenUsage` / `_runQueryWithRetry` 的 docstring 同步到「两个分支」 |
| `app/tests/unit/test_chat_step_error_text.py` | 新增 `TestRetryFailureDetails`、`TestStepFailedErrorWithRetryFailure`（含两段各自兜底、两段各自脱敏、总上限、**长首次原因不得挤掉重试原因**）；`_retryErr()` 造第二条可区分的语句异常 |
| `app/tests/integration/test_chat_multi_step.py` | 新增 `_RetryGenFailsLlm`（按回灌提示词固定句判定 → 返回无 ```sql 围栏内容）；`TestSingleStepRetryMetering` 补 3 例：重试生成失败落账、流式硬失败、**非流式硬失败**（此前类 docstring 误称该路径会因回滚丢行，见 §7）；`TestStepFailureIsolation` 补文案用例 |

**不可变性**：`_RetryFailure` 是 `frozen dataclass`；新增函数均返回新值，未原地改写入参
（`setattr` 作用于上抛的异常对象 —— 那是「挂载元数据」的既定模式，不改变其类型/消息）。

## 6. 测试

TDD：每条改动先写用例、看它按**预期原因**红，再改实现。三处 RED 证据（均为行为级）：

**RED-1（M7 本体）**：临时移除 `except` 里唯一一行 `_attachRetryFailure`（`# TEMP-RED-PROBE`），
集成用例报出 M7 的原症状 ——

```
AssertionError: 未交代重试这一步：该步骤执行失败：ORA-00942: 表或视图不存在
```

**RED-2（复审 MEDIUM-1，重试生成失败的 token）**：

```
AssertionError: nl2sql 台账行数不符：[(20, 10), (20, 10), (20, 10)]
assert 3 == 4
```
（缺的正是重试生成那次调用的 (10, 5)；同批日志可见 `回灌重试的 SQL 生成失败: 无法生成有效的查询 SQL…`）

**RED-3（复审 LOW-3，长首次原因挤掉重试原因）**：

```
AssertionError: 该步骤执行失败：首次：xxxxx…(200 字)… ；重试后仍执行失败：重试的病根
assert False   ← 重试原因整段消失
```

GREEN：

```
app/tests/unit/test_chat_step_error_text.py                16 passed
app/tests/integration/test_chat_multi_step.py              24 passed
chat 集成回归切片（chat_api / chat_stream_api / chat_multi_step / follow_up_cascade
  / chat_history_api / l1_routing / model_routing_fallback / pipeline_layer
  / service_state / chat_agent_run / token_usage_service）  126 passed
app/tests/unit（全量，带 TEST_DATABASE_URL）              2 failed, 2259 passed, 1 skipped
  ↑ 2 项失败均为预存、与本批无关：test_chat_service.py::TestSearchByKeywordAdsWeighting
    （ADS 加权重排死代码，已被 _rankByLayer 覆盖）+ test_dependencies.py::test_stub_disabled
    （Header.lower() 版本漂移）—— 两者曾用 HEAD worktree 单跑证明前置存在
  （另：不带 TEST_DATABASE_URL 跑会有 2 个 ERROR —— unit/conftest 的 seedEngine 要求真实
    PG 地址、禁止 sqlite，属环境缺变量而非缺陷；带变量跑即消失，已复跑确认）
```

用例设计上**刻意避免的两种假绿**：①台账用例断言「行数 == 本次请求实际发生的生成调用次数」
（不变量），而不是断言某个 `purpose` 列表 —— 后者会被实现细节固化（C3 那批判例正是被此
反噬过）；②文案用例断言两段**内容**同时在，而不是断言拼接用的模板字符串。

## 7. 安全审查

两轮都跑了（`code-reviewer` ×2 + `security-reviewer` ×1，均为静态审查，不跑测试以避与
我方套件争用同一测试库）：

| 轮次 | 结论 |
|---|---|
| code-reviewer（第一轮，M7 实现后） | **APPROVE** — 0 CRITICAL / 0 HIGH / **2 MEDIUM** / 3 LOW，两条 MEDIUM 与一条 LOW 当场修完（见下），另 2 条 LOW 记录 |
| code-reviewer（第二轮，收口后） | **APPROVE** — **0 / 0 / 0 / 1**（5 个维度逐项核验：重复记账、信息泄漏、对外契约、边界、测试有效性） |
| security-reviewer（收口后） | **PASS-WITH-WARNINGS** — 0 CRITICAL / 0 HIGH / 0 MEDIUM / 2 LOW |

| 级别 | 发现 | 处置 |
|---|---|---|
| MEDIUM-1 | **重试生成自己失败时 token 仍丢账**：`generateSql` 把用量挂在 `Nl2SqlError.tokens` 上交回（它自己不落账），而该分支只挂失败详情、没挂用量 ⇒ 四种组合下都漏这一笔 | 先写 RED（3 行 vs 4 行）再修：`_attachRetryGenTokens(firstErr, self._consumedTokens(genErr))`（§2.2 方案 B） |
| MEDIUM-2 | **类 docstring 的因果判断是错的**：本文档初稿与 `TestSingleStepRetryMetering` 的 docstring 都写「非流式硬失败会因 `getDb` 回滚而丢掉台账行，故不断言该路径」。**该结论被证伪**：`TokenUsageService.recordUsage` 每次调用都 `session.add()` + `await session.commit()`（`token_usage_service.py:57-58`），行在每次 LLM 调用后即已提交，`getDb` 的 `rollback()` 只能回滚未提交的工作 | 改正 docstring，并补上此前**完全缺失**的非流式硬失败用例（`pytest.raises(RuntimeError, match="ORA-00942")` + 断言 2 行台账）。该用例同时钉住一条环境事实：非流式硬失败是**异常穿透 ASGI 重抛**（无 catch-all 异常处理器，`ASGITransport` 默认把异常重抛给调用方），不是 500 响应 |
| LOW-3 | 整体尾部截断会让长首次原因挤掉重试原因 | 两段各自限量 + 新增「长首次原因不得挤掉重试原因」用例（§2.3） |
| LOW-4 | 用例里的行数硬编码未写明它所依托的不变量 | 补注释：「每次 `generateSql` 恰好落一行 nl2sql（无论成功与否，失败由异常携带 token 交回）」 |
| LOW（code-reviewer 二轮，唯一 1 条） | `setattr` 挂私有属性形式上偏离全局「不可变数据」规则 | **不改**：该偏离是 repo 既定模式且经论证（改异常类型会连带改变 API 层状态映射）；评审结论亦为「有充分论证的形式性偏离，不构成缺陷」 |
| LOW-1（security） | **新增的一处用户可见披露面**：「重试生成失败」分支首次把 `genErr` 文案带给用户，当 `genErr` 为 `LlmClientError` 时其 `.message` 内嵌底层 provider 异常的 `str(exc)` | **记录为已知残差，不猜着修**：脱敏契约针对的是 SQLAlchemy 语句异常的**确定性**泄漏（RED 实测带出 `[SQL:` + 参数值），provider 错误体「可能回显请求内容」目前是**臆测**、无观测证据。可落地时再加（生成失败段对非 `Nl2SqlError` 走固定文案）；已进 §9 关联的 backlog |
| LOW-2（security，**既有行为**） | `retryErr` 经 `%s` 格式化把 `[parameters: {…}]`（业务参数值）写进**服务端**日志 | 旧代码早已如此、本批未扩大该面；服务端日志可接受，纳入日志留存/脱敏策略待议，不在本次范围 |

**复核方法（逐项查证 + 反证，非抽样）**——两份复审独立确认了本批的三条关键推断：

1. **无重复记账**：重试生成那次调用**不走** `_callWithFallback`（`_:4226` 直调
   `self._nl2sql.generateSql(self._llmFactory(cfg), …)`），而 `fallback_<purpose>` 行只在
   `_callWithFallback` 内部降级时写（`_:4518-4521`）；`nl2sql_service.py` 全文无
   `recordUsage`（grep 零命中）⇒ `_accountRetryGenUsage` 是唯一收口、三条调用方互斥且各只调一次。
   这正是 §9 遗留 1 当年提示「要小心与 `fallback_sql` 行重复计数」的那处 —— **核验为不适用**。
2. **四种组合的台账行数**（与 4 个新集成用例断言一致）：非流式×回退多步 4 行 / 非流式×硬失败 2 行
   / 流式×回退多步 4 行 / 流式×硬失败 2 行（另有 `step_plan` 等其它 purpose 行，不计入 nl2sql）。
3. **脱敏在截断之前** ⇒ 不可能截出半个 `[SQL:` 标记；两段各自兜底、`stageLabel` 为代码内字面量；
   `Nl2SqlError` 进用户文案的是**固定通用模板**（`messages_zh.py`），`detail`（可能含内部描述）不进。

预存 lint（**非本次引入，未顺手改以免污染本批 diff**）：`ruff` 对三个文件的诊断与
`HEAD` 逐条一致（`chat_service.py` 29 条：`UP017`×7、`F821`×6、`UP037`×2、`SIM114`×2、
`B904`×2、`F401`×3、`B007`、`F811`、`F841`×2、`SIM103`×2、`I001`；`test_chat_multi_step.py`
1 条 `I001`）。其中两条 `B904` 就在本次改动的 `raise firstErr` 上（未加 `from genErr`）——
**未改**：`from` 只填 `__cause__`（服务端日志已 `exc_info=True` 打全链），而把 `genErr` 变成
显式 `__cause__` 会改变异常链在 API 层的呈现路径，超出「只挂私有属性」的既定边界。

## 8. 部署验证

代码提交 `270f372`，随后重建并重启后端（用户要求「重新用最新代码打包后端镜像并启动」）：

```bash
cd docker && docker compose build backend && docker compose up -d backend
# => Image qa-system-backend Built（14.2s）/ qa-backend Started
```

**镜像确实装了本次代码**（不是「重建了但跑旧代码」—— 见 memory 里 `docker cp` 部分覆盖的教训）：

| 检查 | 结果 |
|---|---|
| `docker exec qa-backend md5sum /app/app/services/chat_service.py` vs 仓库 md5 | `0474f6eb…` == `0474f6eb…` **MATCH** |
| 新符号在容器内出现次数（`_STEP_FAILED_SEGMENT_LIMIT` / `_accountRetryGenUsage` / `_attachRetryFailure` / `_RetryFailure`） | **14** |
| 容器状态 / 启动日志 | `Up`；`Application startup complete`（无 ORM drift、无迁移缺失告警） |

**容器内行为探针**（真机跑部署后的代码，非仅比对 hash；`docker exec -w /app qa-backend python -c …`）：

```
文案: 该步骤执行失败：首次：(builtins.RuntimeError) ORA-00942: 表或视图不存在；重试后仍执行失败：(builtins.RuntimeError) ORA-00904: 标识符无效
泄漏检查: 无                     ← 断言 [SQL:/[parameters:/SECRET_TABLE/ACME-机密客户/OTHER 均不出现
用量携带: (123, 45) | 类型/消息不变: ProgrammingError
长首次原因下重试原因仍在: True   ← 500 字首次原因也不挤掉重试原因
未触发重试时文案不变: 该步骤执行失败：连接超时
```

端点抽样（经 nginx，验证整体可用性未被本次部署破坏）：

| 端点 | 结果 |
|---|---|
| `GET /api/v1/health`（直连 8000 / 经 nginx 5173） | **200 / 200** |
| `GET /api/v1/menu-config` | 403（鉴权闸正常） |
| `GET /api/v1/ontology/health/joins` | 200 |

**附带确认**：本次后端容器被重建（IP 变更），经 nginx 的请求仍全程正常（无 502）⇒
`fix-nginx-upstream-stale-ip` 的 `resolver` + 变量 `proxy_pass` 再次经受住容器重建。

> **未重建前端**：本批无任何前端改动（改的是后端错误文案的生产侧，前端只是渲染既有字段）。
> 未做「真实触发一次回灌重试失败」的端到端演练 —— 那需要构造一条必然执行失败且重试
> 也失败的 SQL（要可写的业务库对象 + 受控模型输出），超出「部署验证」范围；行为等价性
> 由上表的容器内探针 + 4 个集成用例（真实 PG + 完整 API 链路）共同覆盖。

## 9. 关联

- 评估文档：`Harness/wiki/chat-service-assessment.md` §2.3 M7（本次标 ✅ 已修）、§9 遗留 1
  （三个子项本批全部闭合）、§10 遗留 1
- 前一变更：`../fix-multistep-failure-isolation/summary.md`（C3 引入 `_stepFailedError`
  与「步骤失败隔离」；本批在其上补二次失败详情）、`../fix-llm-metering-blindspots/summary.md`
  （「失败路径也是计量路径」这一教训的出处）、`../fix-c-fallback-global-filters/summary.md`
  （P0 第 5 项，本批的前一批）
- 契约出处：`app/domain/exceptions.py`（`Nl2SqlError.tokens`）、
  `app/services/token_usage_service.py`（每次调用即 commit —— MEDIUM-2 的关键证据）、
  `app/services/nl2sql_service.py`（`generateSql` 只累加 token 不落账）
- 规则：`Harness/rules/开发流程规范.md`（TDD + 双审）、根 `CLAUDE.md` 核心约束 #3（Token 计量）
- Memory：`qa-system-m7-retry-failure-details.md`（新增）

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / SSOT 出处）
- [x] 9 段都非空，无 TBD/TODO 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（3 处设计决策各 2–3 案，含被否理由）
- [x] 第 3 段：无迁移（显式写「无」）
- [x] 第 7 段：已跑 code-reviewer **两轮**（APPROVE / APPROVE 0-0-0-1）+ security-reviewer（PASS-WITH-WARNINGS），两条 MEDIUM 的闭合证据 + 2 条 LOW 残差的处置 + 预存 lint 如实标注
- [x] 第 8 段给出部署命令 + 真机验证结果（md5 MATCH + 容器内行为探针 + 端点抽样，已执行）
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki 文档（`chat-service-assessment.md` §0 进度 / §2.3 M7 行 / §3 P0 第 5 项尾注 / §9 遗留 1 / §10 遗留 1 / 新增 §11）已同步
- [x] 至少 1 条 MEMORY 索引已添加（`qa-system-m7-retry-failure-details.md`）
