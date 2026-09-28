# 变更：planner-step-limit

- **日期**：2026-09-28
- **作者**：Claude / 启琳
- **Phase**：架构升级 v3.1 甲线 **M2a**（planner 拆步步数硬限）
- **状态**：in-review（分支 `feat/planner-step-limit`，未合并）
- **关联变更**：无 predecessor（A1 `2026-09-28-m0-id-mapping` 为同计划相邻任务，代码无交集）
- **迁移版本**：无
- **commit**：`f1dac5f` → `932ad39` → `99e5d7c` → `8597874`（+ `74357fd` wiki）
- **设计稿**：`docs/superpowers/plans/2026-09-28-planner-step-limit.md`

---

## 1. 需求

**验收标准**（来自 A6/M2a）：问「完整分析华东销售下降所有原因」这类需要十几步的问题，应返回**拆解提示**（请聚焦单一维度提问），而不是一份 12 步的计划。

改动前的实际行为是**静默截断**，用户视角有三层问题：

| 现象 | 用户感受 | 后果 |
|---|---|---|
| `_plan_by_llm` 出口 `steps[:MAX_MULTI_STEP]` 静默砍到 4 步 | 拿到的是「12 问里的 4 问」，**没有任何提示** | 以为自己拿到了完整答案 —— 比报错更坏 |
| system prompt 写着「最多拆 4 个子步骤」 | —— | LLM 在 planner **之前**就自行合并/丢弃子问题，真实步数根本没传到执行层 |
| 规则快路径（`rule_based_split`，「第X步」标号） | —— | **完全无上限**：5 个标号 = 5 个数据步无条件执行（成本与耗时双重失控） |

**为什么是硬限而不是调大上限**：多步链的成本是 2N 倍 schema 投喂（plan 与 SQL 各投一遍，见 `Harness/wiki/nl2sql-engine.md` 成本模型），步数线性放大 token；且「静默截断」的形态在步数更多时只是把「12 问里的 4 问」变成「20 问里的 6 问」，用户依旧不自知。

## 2. 设计评审

### 候选方案

| # | 方案 | 否决理由 |
|---|---|---|
| A | 保留截断，只把上限从 4 调大（如 8） | 治不了本：用户仍不知道自己少了哪几步，只是分母变大了。且成本随步数线性上升，等于用钱买"更长的半截答案" |
| B | planner 自己判超限、自己返回提示 | **拒收文案必须说得出真实步数**。planner 若自截断后再判断，它看到的 N 恒等于上限值，文案退化成"需要 4 步，超出 4 步上限"这种废话。且 planner 是纯函数式的 LLM 输出点，没有 session，落库/存状态要额外打通 |
| C | **planner 如实上报，执行缝判定拒收**（选定） | —— |
| D | 在 planner 出口把超限计划转成 `None`（退化为单步） | 更糟：单步路径会拿一条覆盖 1/N 的 SQL 硬答，用户拿到的是**错误**答案而非"没有答案"。这恰是本变更要消灭的形态 |

### 最终决定

方案 C。职责按「**如实上报** / **受不受理**」切开：

- `step_query_planner`：删掉截断，删掉 prompt 里「最多拆 4 个子步骤」的**自限**（留着它，拒收分支永远不可达 —— 改了个死代码）；docstring 明确「步数超限不在此处理」
- `chat_multistep._isOversizedPlan`：唯一的守门谓词（`len(plan.data_steps) > MAX_PLAN_DATA_STEPS`）
- `chat_multistep._rejectOversizedPlan`：唯一的拒收动作（落库 + 存状态 + 固定文案），两条路径共用
- `chat_stream._streamMultiStep`：与非流式同口径，事件序列照抄「计划 target=无法回答」分支

**上限常量派生而非另写字面量**：`MAX_PLAN_DATA_STEPS = MAX_MULTI_STEP - 1`。两个语义不同的 5（含汇总步 vs 纯数据步）日后极易被"顺手统一"成一个，从而破坏循环守卫；测试 `test_max_plan_data_steps_derives_from_max_multi_step` 钉死这层派生关系。

### 评审后修补（MEDIUM）

code-reviewer 指出**跨路径 UI 漂移**：同一个超限计划，流式下发 `step_result`（前端渲染一张「超出步数上限」卡），非流式却返回 `steps=[]` —— 前端 `MultiStepPlanCard` 只在 `steps` 非空时挂载，空数组会让固定文案变成一段**没有归属的裸文字**。而非流式「无法回答」分支本就为同一理由填卡，故**非流式拒收才是三者中的异类**。已收敛到 `_oversizedStepResult` 一处（`8597874`）。

## 3. 数据模型变更

无。不涉及表、列、索引、迁移脚本（`迁移版本：无`）。

## 4. 接口契约变更

无新增端点；`POST /api/v1/chat` 与 `/api/v1/chat/stream` 在**超限场景**的响应形状补齐：

| 端点/字段 | 变更 | 说明 |
|---|---|---|
| `POST /api/v1/chat` → `steps` | 超限时由 `[]` 变为 **1 张卡**（`description="超出步数上限"`、`sql=null`） | 与「无法回答」分支同型；其余场景不变 |
| `POST /api/v1/chat` → `answer` | 超限时为固定文案 `MSG_PLAN_TOO_MANY_STEPS`，含**真实**步数与上限 | `该问题需要拆解为 {steps} 步，超出 {limit} 步上限，请聚焦单一维度提问（如先分析客户层面原因）。` |
| `POST /api/v1/chat/stream` | 超限时事件序列：`multi_step_plan`(1 步概览) → `step_start` → `token` → `step_result` → `done` | **不出现** `sql`/数据步事件；`done` **不带** `steps`（与非流式同一分支惯例，避免前端清空计划卡） |

`intent` 仍为 `multi_step`（超限是「受理与否」，不是新的意图类型）。

## 5. 实现要点

| 文件 | 改动 |
|---|---|
| `app/domain/multi_step_plan.py` | 新增 `MAX_PLAN_DATA_STEPS = MAX_MULTI_STEP - 1`（派生，不写字面量） |
| `app/services/step_query_planner.py` | 删截断、删 prompt 自限、修 docstring；prompt 增加「如实列出全部子问题，不要因为数量多就自行合并或截断」 |
| `app/services/chat_multistep.py` | `_isOversizedPlan`（谓词）+ `_oversizedStepResult`（卡片唯一出处）+ `_rejectOversizedPlan`（落库+存状态）；`_executeMultiStep` 顶部早返回 |
| `app/services/chat_stream.py` | `_streamMultiStep` 顶部同口径早返回（放在读 cache multiplier **之前**：拒收不需要它，省一次 DB 读） |
| `app/services/messages_zh.py` | 新增 `MSG_PLAN_TOO_MANY_STEPS`（插在多步降级段之后，刻意避开文件末尾，减少与 A1 追加段的合并冲突） |

**守门位置为什么是这两个缝**：生产里 `MultiStepPlan` 只有两个构造点（`step_query_planner.py` 规则/LLM 各一），但有 **10 个执行入口**（`chat_service.py` ×5 + `chat_stream.py` ×5，含追问级联）。把谓词放在执行缝 ⇒ **一处即全覆盖**；放在构造点则要改 2 处且未来新增入口会绕过。早返回位于任何数据步之前 ⇒ 不生成 SQL、不烧数据步 token。

**错误处理**：超限是**正常业务结果**而非异常 —— 不抛错、不重试、不降级；infra 层 `except` 语义不变。日志用 `info`（拒收率是要观察的产品指标），与 `_finalizeMultiStepDegrade` 的 `warning` 区分。

## 6. 测试

| 文件 | 用例 | 验证点 |
|---|---|---|
| `tests/integration/test_chat_multi_step.py`（新增 `TestOversizedPlanRejected`） | `test_oversized_plan_is_rejected_with_hint` | 6 步 → 文案含**真实**步数 6 与上限 4；`steps` 为 1 张拒收卡；无业务 SQL |
| 同上 | `test_oversized_plan_burns_no_data_step_tokens` | 台账只剩 `step_plan` + `multistep_global_filter` 两行 ⇒ 拒收发生在**任何数据步之前**（与前 4 步照常执行的分水岭） |
| 同上 | `test_rule_path_oversized_is_rejected_too` | 规则快路径（5 个「第X步」标号）同样拦下 —— **此前完全无上限** |
| 同上 | `test_four_data_steps_at_limit_still_executes` | **边界反向验证**：恰好 4 步必须放行（只测"坏的被拦"会让上限被写成 `>=` 也照样绿） |
| 同上 | `test_streaming_oversized_yields_hint_without_data_steps` | 流式同口径：无 `sql` 事件；概览 1 步；`step_result` 恰好 1 条且 `sql is None` |
| `tests/unit/test_step_query_planner.py` | `test_steps_over_limit_pass_through_untruncated` | 6 步计划**原样返回**（回归自 `..._truncated`） |
| 同上 | `test_system_prompt_does_not_cap_step_count` | prompt 契约：`"最多拆"` 不得再出现（自限会让拒收分支不可达） |
| `tests/unit/test_multi_step_plan.py` | `test_max_plan_data_steps_derives_from_max_multi_step` | 上限必须**派生**，防两处漂移 |

**回归证据**（`COVERAGE_FILE=/tmp/.cov_a6`，unit 与 integration **分进程**跑，避免 TRUNCATE 陷阱）：

- unit：2602 passed / **51 failed**；51 例与基线 `5fe3958` 的失败集合 **`diff` 为空**（不是数量相同，是逐条同名）
- integration：@@COV@@

## 7. 安全审查

触发条件命中：用户输入（问题文本经 planner LLM 解析）、LLM 调用。**已跑 security-reviewer**：

| 级别 | 结论 |
|---|---|
| CRITICAL | 无 |
| HIGH | 无 |
| MEDIUM | 无**新增**。报告了一处**域外既存**问题：`ChatRequest.sessionId` 由客户端控制，而 `GET /session/chat-history`、`GET /session/{sessionId}/messages`、`DELETE /session/{sessionId}` 未按用户过滤（`_storeSessionMessages` 不写 `user_id`）⇒ 任意登录用户可读/删他人会话。与本变更无关，**建议另立变更**（本变更不含任何会话归属逻辑） |
| LOW | `step_query_planner` 的 `complete()` 未传 `maxTokens`（既存，非本次引入）；已确认拒收路径不会把用户输入拼进 SQL，`_sanitize` 转义链路未改变 |

code-reviewer 结论 **APPROVE**（0 CRITICAL / 0 HIGH / 1 MEDIUM 已修 / 1 LOW 既存）。其 LOW 为成本**展示**口径：拒收响应的 `tokensUsed/cost` 未含先前已花的 `multistep_global_filter`，与所有其它多步响应一致，台账（`SessionTokenUsage`）记录正确，无重复计费。

**本变更对安全是净改进**：规则快路径此前**无上限**，等于给了一条"输入 100 个「第X步」标号即可驱动 100 次 SQL 生成"的放大路径；现已收敛到 4 步。

## 8. 部署验证

```bash
$ docker ps --format '{{.Names}}\t{{.Status}}' | grep qa-
qa-backend   Up 3 hours      0.0.0.0:8000->8000/tcp
qa-postgres  Up 3 days (healthy)
qa-milvus    Up 34 hours (healthy)

$ curl -s http://localhost:8000/openapi.json | python3 -c "import sys,json;print(len(json.load(sys.stdin)['paths']))"
226
$ curl -s http://localhost:8000/openapi.json | python3 -c "...chat paths..."
['/api/v1/sessions/chat-history', '/api/v1/wiki/chat', '/api/v1/chat', '/api/v1/chat/stream', '/api/v1/chat/suggest']
```

**未把分支代码灌进共享容器**（有意）：`docker exec qa-backend grep -c MAX_PLAN_DATA_STEPS /app/app/domain/multi_step_plan.py` → **0**，即容器仍在跑改动前的代码。分支未合并时 `deploy_backend.sh` 会让共享 dev 栈运行"不对应任何分支"的代码，干扰其他 worktree（乙线等）对着它做的验证。

因此本变更的等价验证走**真 PostgreSQL + 完整 API 链路**（`POST /api/v1/chat` 走 ASGI 全栈，非直接调 service，符合 `Harness/rules/测试规范.md` §真实数据库测试）：见 §6 的 5 条 integration 用例。**无 SQL/DB 结构改动**，故不触发 `scripts/*_realdata.py` 门禁。

**合并后需观察**：info 日志 `拆步超限，按上限拒收（%d 步 > %d 步）` 的频次。若拒收率过高，优先调 prompt 的「只拆彼此独立的子问题」措辞（见 §2 候选 A 的否决理由），最后才考虑抬上限。

## 9. 关联

- 设计稿：`docs/superpowers/plans/2026-09-28-planner-step-limit.md`（含 Task 1–4 的 TDD 步骤与自审表）
- Wiki：`Harness/wiki/nl2sql-engine.md` §「步数硬限（`MAX_PLAN_DATA_STEPS=4`）」——多步决策 SSOT 新增一节（常量出处 / 守门谓词 / 拒收动作 / 两条路径事件序列 / 为何放执行缝 / 有意为之的覆盖面下降）
- Rules：`Harness/rules/测试规范.md`（真实 PG + 完整 API 链路）、`Harness/rules/开发流程规范.md`（10 阶段：本变更为 需求→设计→TDD→审查→变更记录）
- Memory：`qa-system-planner-step-limit.md`（限制在执行缝的理由 + 分支状态）、`qa-system-value-sample-cache-order.md`（进程级缓存致断言失序）、`qa-system-dedicated-pg-a1.md`（并发分支用独立 PG 容器，已扩记 `qa-pg-a6`）
- 相邻任务：A1 统一 ID 中枢（`2026-09-28-m0-id-mapping`）—— 同属甲线，无代码交集

---

## SSOT 校验清单

- [x] frontmatter 元数据齐全（日期 / 作者 / Phase / 状态 / 关联变更 / 迁移版本 / commit）
- [x] 9 段都非空，无 TBD/TODO 占位
- [x] 第 2 段 ≥ 2 个候选方案对比（A/B/C/D 四案）
- [x] 第 3 段无迁移（`迁移版本：无`），不涉及文件名长度校验
- [x] 第 7 段已跑 security-reviewer，CRITICAL/HIGH/MEDIUM/LOW 逐级给出
- [x] 第 8 段 docker compose 冒烟命令 + 输出贴出，并说明为何未部署分支代码
- [x] 第 9 段 ≥ 3 个跨文件链接（设计稿 / Wiki / Rules / Memory / 相邻任务）
- [x] 相关 wiki 已更新（`nl2sql-engine.md`）
- [x] MEMORY 索引已加（`MEMORY.md` 两行）
- [x] 无 SQL/DB 改动 ⇒ 不触发 `scripts/<feature>_realdata.py` 门禁
