# 变更：fix-chat-llm-keyless-and-degrade

- **日期**：2026-09-26
- **作者**：Claude
- **Phase**：bugfix（Chat 流水线 LLM 可用性 + 多步降级收尾）
- **状态**：done
- **关联变更**：[feat-follow-up-cascade](../feat-follow-up-cascade/summary.md)（同批发现，其「未修」段指向本变更的第 2 项）
- **迁移版本**：无
- **MEMORY**：[qa-system-chat-llm-router-keyless-500.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-chat-llm-router-keyless-500.md)

---

## 1. 需求

排查上一条追问级联回归时，顺带发现两个独立缺陷。用户裁决「都优化一下吧」，故一并修复。

**验收标准**

1. 不带 `modelId` 调 `/chat`（脚本 / API 直调 / 前端未选模型）**不得**因自动路由选中「无 API key 的模型配置」而返回裸 `AttributeError` 500。
2. 所有配置都构造不出客户端时，`/chat` 与 `/chat/stream` 给出**明确**错误（503 + 可读文案 / `errorType=domain` 事件），而非「服务内部错误」。
3. 显式指定一个「存在但无 key」的 `modelId` 返回 503，**不得**退化成误导性的 404「配置不存在」。
4. 多步执行落到「无 aggregation_only 步骤」的降级分支时，**流式与非流式都必须**落库会话消息并保存查询状态（`last_question` / `last_sql`），且文案如实报告已完成的数据步骤数。
5. 无回归：chat 相关集成 + 单元套件除 1 个预存失败外全绿。

## 2. 设计评审

### 根因

**缺陷 1 — 超预算分支钉死无 key 配置（活缺陷）**

`ModelRouterService.selectModel` 分支 (7)：`sessionCost >= sessionBudget` → 无条件 `_cheapest(active)`。本地 `docker/.env` 的 `SESSION_BUDGET=0.1` 是个**开发期压测值**，会话跑几轮就永久超预算；而本地 DB 里最便宜的配置恰好是**没有 API key** 的 `Qwen3.8-27B-4bit`（`cost_per_1k_input=0`）：

```
 id |    model_name    |        provider         | is_active | has_key | cost_per_1k_input
----+------------------+-------------------------+-----------+---------+-------------------
  1 | deepseek-chat    | openai_compatible_proxy | t         | t       |          0.001400
  3 | Qwen3.8-27B-4bit | openai_compatible_proxy | t         | f       |          0.000000
  8 | kimi-k3          | moonshot                | f         | t       |          0.000000
```

`createClient`（`infrastructure/llm/factory.py`，判「有无可用 key」的 SSOT）对它返回 `None`，而 `_buildPipelineContext` 把这个 `None` 直接塞进流水线 → 首个 LLM 调用抛裸 `AttributeError: 'NoneType' object has no attribute 'complete'` → 500。

放大因素：`chatStore.ts:145` 的 `selectedModelId` 初值为 `null` 且**不持久化**，`ChatPanel` 也没有自动选中 → **刷新页面后前端自己就会不带 modelId 发请求**，同一会话只要成本已持久化过预算线，聊天 UI 直接 500。（排查中我曾误判「前端不受影响」，已更正。）

**缺陷 2 — chat 缺 `None` 兜底（活缺陷，与缺陷 1 互为表里）**

`doc_qa`（`rag_qa_service.py:184`）与 `wiki_qa`（`wiki_qa_service.py:182`）**都已**实现：

```python
if client is None:
    raise LLMUnavailableError(...)   # → 503
```

chat 流水线**漏了**这一步。于是即使补上缺陷 1 的候选过滤，「全部配置都无 key」的其余入口仍退化成内部 500，用户看到「服务内部错误」而不是「未配置可用的 LLM」。

**缺陷 3 — `_streamMultiStep` 降级分支不保存查询状态（潜在缺陷）**

`_streamMultiStep` 后循环的降级分支（所有步骤都不是 `aggregation_only`）只 `yield` 一句文案就结束：**不落库会话消息**（用户那一轮成了悬空的 user turn）、**不调用 `_saveQueryState`**（`last_question` 留旧值）。这与追问级联回归里「失败那轮把短句写回 `last_question`」是**同一种状态污染**，会让下一轮追问基于错误的历史。

诚实说明：两处 `MultiStepPlan` 构造点（规则拆步 `_splitByRulePattern` 与 LLM 拆步）**都必然追加一个 aggregation 步骤**，且没有任何下游会把它过滤掉 ⇒ 该分支**当前在生产不可达**。这是**潜在**隐患（未来改拆步逻辑就可能踩），不是活 bug。仍修，因为成本极低且两路径共用一个收尾函数能永久消除「只改一条路径」的偏差。

### 候选方案与取舍

| 方案 | 取舍 |
|---|---|
| A. 只加 `client is None` → 503 兜底 | 最小改动。但用户仍拿不到答案——超预算这个**可恢复**场景被降级成硬失败。**不采纳为唯一方案** |
| B. 把 `SESSION_BUDGET` 调大 | 只治本地配置，治不了「库里存在无 key 的激活配置」这个**数据面**根因（换环境照样复发）。**不采纳为唯一方案** |
| C. 自动路由候选池剔除无可用 key 的配置 + `None` 兜底（**采纳**） | 纵深防御：路由层保证「选出来的能真正调用」，兜底层保证「实在没有时错误明确」。判据**复用 `createClient`**，不二次实现 key 解析（含各 provider 的 env 回退） |

选项 C 的一个关键细节：**过滤只作用于自动路由**。显式 `modelId` 仍按**全量**配置查找，否则「配置存在但没有 key」会退化成误导性的 404「配置不存在或已禁用」。

降级收尾同理抽取 `_finalizeMultiStepDegrade` 供流式/非流式**共用**，而不是各写一份。

### 最终决定

采纳 C。同时把 `docker/.env` 的 `SESSION_BUDGET` 从 `0.1` 回调到 `1.0`（对齐代码默认值，其注释已记录 0.1 会导致质量坍塌）。因为候选池已过滤，即使再触发超预算分支，也会降级到最便宜的**可用**模型而非直接失败。

## 3. 数据模型变更

无表结构变更。仅 `docker/.env`（gitignored，本地）的 `SESSION_BUDGET` 值 0.1 → 1.0。

**数据面观察**（非本次改动引入，记录备查）：`llm_config` 中存在激活但无 key 的配置（id=3），这是缺陷 1 能成立的土壤。运维上建议无 key 的配置保持 `is_active=false`。

## 4. 接口契约变更

对外路径/请求字段无变化，仅**失败形态**变化（这是本次的核心价值）：

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 不带 `modelId`，池中含无 key 配置 | 500 裸 `AttributeError` | 200，路由到可用模型 |
| 全部配置无法构造客户端（非流式） | 500 裸 `AttributeError` | **503** `{"success":false,"error":"未配置可用的 LLM，无法回答该问题"}` |
| 全部配置无法构造客户端（流式） | 通用 `except` → `errorType=internal` | **`errorType=domain`** 的 error 事件 |
| 显式 `modelId` 指向「存在但无 key」 | 500 | **503**（而非 404） |
| `modelId` 不存在 / 已禁用 | 404 | 404（不变） |

新增文案常量：`app/services/messages_zh.py: MSG_LLM_UNAVAILABLE = "未配置可用的 LLM，无法回答该问题"`，与 `doc_qa` / `wiki_qa` 同口径。

## 5. 实现要点

**`backend/app/services/chat_service.py`**

- 新增 `_usableModelConfigs(configs)`：用 `self._llmFactory(c) is not None` 筛掉无法构造客户端的配置（判据复用 `createClient`，不二次实现 key 解析）。全部不可用时**回退原列表**，让调用方的 `None` 兜底给出 503，而不是抛 `NoAvailableModelError`。剔除时打 INFO 日志（候选 N → M），便于线上观测。
  - **逐配置隔离 `ConfigError`**（code-review 发现的 HIGH，已修，见 §7）：`createClient` → `_resolveApiKey` → `decryptApiKey` 对非法密文**抛 `ConfigError`**。本 helper 会遍历**每一个**配置（改动前只解密被选中的那一个），列表推导无隔离 ⇒ DB 里单条密文损坏的配置（跨环境 restore 导致 FERNET key 不匹配、手工改库）会让**所有**走自动路由的请求 400。改为逐配置 `try/except ConfigError` → 按「不可用」筛掉并记 warning。语义上「解不开的密文」正是「不可用」。
- `_buildPipelineContext` 重构：
  - 显式 `modelId` → 在**全量** `configs` 中查找（保 404 语义正确），`routeCandidates = configs`。
  - 自动路由 → `routeCandidates = self._usableModelConfigs(configs)`，`selected = self._modelRouter.selectModel(routeCandidates, ...)`。
  - `client = self._llmFactory(selected)`；`if client is None: raise LLMUnavailableError(MSG_LLM_UNAVAILABLE)`。
  - `_PipelineContext.configs` 也改传 `routeCandidates` —— 该字段仅作 `_callWithFallback` 的候选池，收窄后 fallback 同样选不中无 key 配置。
- `_resolveL4LlmClient` 同步走 `_usableModelConfigs`，并把自身的 `self._llmFactory(selected)` 包进 `try/except ConfigError → return None`：该方法的 docstring 明确承诺「返回 None = L4 不触发（**降级**而非报错，还有 L2/L3 兜底）」，但工厂调用原本在 try 之外（`_runL4AgentLoop` 的 try 也覆盖不到），密文损坏会把整轮请求打成 400——与自身契约矛盾。
- 新增 `_finalizeMultiStepDegrade(session, dto, completed, *, last_plan, last_sql, last_data, total_cost, _t0) -> str`：无汇总步骤时的降级收尾，**流式与非流式共用**。有成功的数据步骤时如实报告「已完成 N/M 个数据步骤，但汇总分析失败」，否则给通用异常文案；两者都调 `_storeSessionMessages` + `_saveQueryState`。
- 非流式 `_executeMultiStep` 尾部与流式 `_streamMultiStep` 尾部均改为调用该 helper（流式照旧 `yield StreamEvent(EVENT_TOKEN, ...)` 下发文案）。
- 顺手删除被遮蔽的重复 `_consumedTokens` 桩定义（保留真实的那个）。

**关键设计点**：过滤**不覆盖**「key 合法但 endpoint 不可达」（如本地 ollama 未起）——那种情况 `createClient` 会正常返回客户端，失败发生在调用期，由既有的 `_callWithFallback` 处理。此边界已在 helper docstring 显式写明。

## 6. 测试

**新增 `backend/app/tests/integration/test_chat_model_routing_fallback.py`（5 用例，全绿）**

测试用**真实** `ModelRouterService`（注入 `Settings(SESSION_BUDGET=0)` 确定性触发超预算分支）+ 按「配置有无 key」返回客户端/`None` 的假工厂（与 `createClient` 同判据），避免依赖 tiktoken 与真实外部调用。

- `TestOverBudgetRouting::test_over_budget_routes_around_keyless_config` — 200 且 `modelName == deepseek-chat`（不是最便宜的无 key 配置）
- `TestNoUsableLlmFallback::test_all_configs_keyless_returns_503`
- `TestNoUsableLlmFallback::test_all_configs_keyless_stream_emits_domain_error_event` — 断言 `errorType == domain` **且**含具体文案；只看「最后是 error 事件」会被通用 `except`（`errorType=internal`）蒙过去
- `TestNoUsableLlmFallback::test_explicitly_selected_keyless_model_returns_503` — 显式 modelId 仍走全量查找，得 503 而非 404
- `TestUndecryptableConfigIsolation::test_corrupt_ciphertext_config_is_skipped_not_fatal`（review 驱动新增）— DB 里有密文损坏的配置时，自动路由跳过它并用健康模型回答。假工厂走**真实 `decryptApiKey`**，只把网络客户端换成 stub，故 `ConfigError` 由生产代码真实抛出。RED 实测复现 `400 {"error":"API Key 密文无法解密，可能密钥已变更"}`

**新增 `test_chat_multi_step.py::TestNoAggregationStepDegrade`（2 用例，全绿）**

通过 monkeypatch `StepQueryPlanner.rule_based_split` 返回两个数据步骤（无 aggregation）来构造该分支；断言 `2/2`、`session_message` 为 `["user","assistant"]`、`state.last_question == question`、`state.last_sql is not None`。流式与非流式各一。

**RED 验证**：3 个路由用例修复前以**生产的同一错误**失败（`AttributeError: 'NoneType' object has no attribute 'complete'`）；2 个降级用例失败于 `assert "2/2" in body["answer"]`（收到通用异常文案）。

**回归**：

- 集成 7 文件（chat_api / chat_stream_api / chat_multi_step / chat_follow_up_cascade / chat_model_routing_fallback / multistep_global_filter / chat_service_state）：**74 passed**
- 单元 5 文件（chat_service / intent_service / multi_step_plan / nl2sql_service / step_query_planner）：**376 passed, 1 failed**
- 唯一失败 `test_chat_service.py::TestSearchByKeywordAdsWeighting::test_weight_from_system_config_db_value` 为**预存**问题（`system_config` 取值顺序依赖 `[29,14] != [14,29]`）；已用 `git stash push -- chat_service.py messages_zh.py` 后复跑确认**同样失败**，与本次改动无关。

## 7. 安全审查

未触发 security-reviewer 强制条件（不涉及认证、密钥读写、SQL Guard、加解密、支付）。人工检查要点：

- 变更**不新增**任何密钥读取路径——`_usableModelConfigs` 复用 `createClient`，不接触 `api_key_encrypted` 明文。
- 错误文案**不泄露**配置内容或 key 状态细节（统一为「未配置可用的 LLM」），不构成配置枚举侧信道。
- 显式 `modelId` 的 404/503 区分**不**越过 ACL：查找仍在已鉴权请求的配置列表内进行。
- 级别：**LOW**（无 CRITICAL/HIGH/MEDIUM）。

**code-reviewer 复审（2026-09-26）**：CRITICAL 0 / **HIGH 1** / MEDIUM 0 / LOW 2。

| 级别 | 问题 | 处置 |
|---|---|---|
| HIGH | `_usableModelConfigs` 无异常隔离 → 单条密文损坏配置击穿整条自动路由（**本次改动引入的回归**） | **已修**（逐配置 `try/except ConfigError`）+ 新增回归测试 |
| LOW | `_finalizeMultiStepDegrade` 两条用户文案硬编码在 service，未进 `messages_zh.py` | **已修**（`MSG_MULTI_STEP_DEGRADE_PARTIAL/FAILED`，`{done}/{total}` 占位） |
| LOW | 「可用池」语义是「能构造出客户端」而非「能连通」（OLLAMA 恒非 None） | 无需改；已在 helper docstring 显式声明，连通性失败交 `_callWithFallback` |

审查同时确认三项设计成立：`usable or configs` 的回退链路闭合（全不可用 → `None` 兜底 → 503）；显式 `modelId` 的 404 语义未被 503 掩盖（禁用/不存在在更早的分支先抛 `NotFoundError`）；`_finalizeMultiStepDegrade` 两处调用的 `_t0`/`total_cost` 口径一致、无重复落库。

## 8. 部署验证

代码与 `.env` 同批生效，必须走 build + up（`docker cp` 改不了环境变量）：

```bash
cd docker
docker compose build backend          # Image qa-system-backend Built
docker compose up -d backend          # Container qa-backend Started
```

容器内核对（部署前 md5 不一致 + `SESSION_BUDGET=0.1`，部署后全等 + 1.0）：

```
$ docker exec qa-backend printenv SESSION_BUDGET
1.0
$ docker exec qa-backend md5sum /app/app/services/chat_service.py /app/app/services/messages_zh.py
6f353b7b4586b01c99ee0a9d856dd473  /app/app/services/chat_service.py    # 与源码逐文件全等
aee8e24f736569e060e92807ccecd22f  /app/app/services/messages_zh.py
```

（`md5` 为含 code-review 修复的**最终**构建；首次部署为 `2eeaa268…`/`d83d971f…`，
两次都逐文件核对过容器 ↔ 源码。）

启动日志无 lifespan / ORM drift 报错（`Application startup complete.`）。

**真机冒烟**：

```bash
# 1) 不带 modelId（修复路径）
POST /api/v1/chat {"sessionId":"smoke-route-...","question":"各供应商收货量是多少","datasourceId":1}
→ HTTP 200, modelName = deepseek-chat
→ answer = "本次查询共涉及 385 家供应商（按供应商编码统计），合计收货量约 17.84 亿。..."

# 2) 证据：过滤器确实执行
docker logs qa-backend | grep 自动路由剔除
→ INFO:app.services.chat_service:自动路由剔除 1 个无可用 key 的模型配置（候选 3 → 2）

# 3) 显式点名无 key 配置 id=3（非流式）
POST /api/v1/chat {"...","modelId":3}  → HTTP 503
→ {"success":false,"error":"未配置可用的 LLM，无法回答该问题"}

# 4) 同场景流式
POST /api/v1/chat/stream {"...","modelId":3}
→ event: error
  data: {"error":"未配置可用的 LLM，无法回答该问题","errorType":"domain","detail":null}
```

**真实数据验证**：第 1 项返回的是 THBI Oracle 真实数据（385 家供应商 / 合计 17.84 亿收货量），非桩数据。

**部署后回归（容器跑的是整个工作区代码，一并验证追问级联未回归）**：
session `smoke-cascade-1790410744`，轮 1 显式两步问题 → 轮 2 追问 `4月份呢`：

```
轮1：multi_step_plan 1 / step_plan 3 / step_result 2 / done 1
轮2：multi_step_plan 1 / step_plan 3 / step_result 2 / done 1     ← 未退化成单步，无 error 事件
```

日志证据链（改写**确实生效**，而非碰巧）：

```
INFO chat_service: 自动路由剔除 1 个无可用 key 的模型配置（候选 3 → 2）
INFO step_query_planner: 拆步规则命中（第X步标号），跳过 LLM 判定: 第一步，查询一下3月份…
… ('smoke-cascade-1790410744', 1, 'deepseek-chat', 120, 33, 153, 0.0002604, 'follow_up_rewrite')
INFO step_query_planner: 拆步规则命中（第X步标号），跳过 LLM 判定: 第一步，查询一下4月份…   ← 已换成 4 月
```

`follow_up_rewrite` 计量行之后紧跟含「4月份」的拆步命中 ⇒ 改写通路与计量都在。

## 9. 关联

- 关联变更（predecessor）：[feat-follow-up-cascade](../feat-follow-up-cascade/summary.md) — 其「未修」段记录的本变更第 2 项
- Wiki：[Harness/wiki/chat-service-assessment.md](../../wiki/chat-service-assessment.md)
- Rules：[Harness/rules/测试规范.md](../../rules/测试规范.md)（真实 PG + 完整 API 链路）
- Memory：[qa-system-chat-llm-router-keyless-500.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-chat-llm-router-keyless-500.md)、[qa-system-follow-up-cascade.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-follow-up-cascade.md)
- 代码：`backend/app/services/chat_service.py`、`backend/app/services/messages_zh.py`
- 测试：`backend/app/tests/integration/test_chat_model_routing_fallback.py`、`backend/app/tests/integration/test_chat_multi_step.py`

---

## SSOT 校验清单（合并前必查）

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本=无 / MEMORY）
- [x] 9 段都非空，无 TBD/TODO/待补 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A/B/C + 取舍表）
- [x] 第 3 段无迁移（已注明「无表结构变更」）
- [x] 第 7 段未触发 security-reviewer，已给人工检查结论与级别（LOW）
- [x] 第 8 段 docker compose 冒烟命令 + 输出贴出
- [x] 第 9 段 ≥ 3 个跨文件链接
- [x] 相关 wiki 文档已更新（`Harness/wiki/chat-service-assessment.md`）
- [x] MEMORY 索引已在 `MEMORY.md` 更新
- [x] 涉及真实数据验证时已跑通（真机 curl + THBI Oracle 真实数据返回）
