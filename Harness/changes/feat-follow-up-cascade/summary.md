# 变更：feat-follow-up-cascade（追问级联：A 分类层 + B 多步重跑 + C 兜底重试）

- **日期**：2026-09-25
- **Phase**：feature（用户体验：从误报「没有相关业务数据」 → 真实追问）
- **状态**：done
- **触发**：用户问三步多步查询后追问「4月份呢？」，系统误判为全新查询，又不注入历史状态，计划阶段无法映射任何表 → 兜底文案「抱歉，当前系统中没有与您的问题相关的业务数据…」（含可查询表清单）。根因：意图分类层 FOLLOW_UP 收紧后（N6 修复）无处理「整句省略式追问」这类句式；FOLLOW_UP 注入路径也只在 A/B 触发后才激活。
- **MEMORY**：（本会话一并写入）

---

## 1. 需求

用户在多步查询后用「X呢？」「那去年呢？」这类省略式追问整轮语义；当前系统将其判为 NEW_QUERY（无指代词 + 无追问关键词）→ 不注入历史 → 误报缺数据。

验收：

- 「4月份呢？」（5 字，结尾"呢"）→ 判为 FOLLOW_UP
- FOLLOW_UP 且上一轮是显式多步问题 → LLM 改写回完整多步问题，重跑整套多步
- 短句 NEW_QUERY 计划「无法回答」 + 有历史 + 短句 → 自动升级追问重试一次（先 B 多步重跑，再退回单轮 FOLLOW_UP 注入）
- N6 权衡保留：疑问词起头的「…呢」句（为什么/哪家/怎么…）仍走 NEW_QUERY，不误锚定

---

## 2. 设计：三层级联漏斗

```
┌──────────────────────────────────────────────────────────────┐
│ A. 分类层（intent_service.py：isEllipsisFollowUp）            │
│    「改动约束 + 呢/呐」短句 + 非疑问词前缀 → FOLLOW_UP         │
│    零 LLM 成本；N6 守卫已排除「为什么/哪家…呢」                │
└──────────────────────────────────────────────────────────────┘
                          ↓（intent=FOLLOW_UP）
┌──────────────────────────────────────────────────────────────┐
│ B. 执行层（chat_service.py：_prepareFollowUpMultiStep）       │
│    上一轮是显式多步 → LLM 改写回完整多步问题 + 规则/LLM 拆步    │
│    改写/拆步失败 → 退回下方单轮 FOLLOW_UP 状态注入              │
└──────────────────────────────────────────────────────────────┘
                          ↓（任何路径不可回答）
┌──────────────────────────────────────────────────────────────┐
│ C. 兜底层（chat_service.py：_isFollowUpRetryCandidate）      │
│    NEW_QUERY/QUERY + 有历史 + 短句（≤20 字）+ 非多步           │
│    → 升级追问重试一次（先 B，再退回单轮注入）                   │
└──────────────────────────────────────────────────────────────┘
```

意图分流：

- A 命中：intake→FOLLOW_UP → B（如适用）→ 单轮状态注入
- A 漏判且 C 命中：NEW_QUERY→FOLLOW_UP（重试一次）
- A/C 都漏判：行为不变（兜底文案）

---

## 3. 数据模型变更

无 alembic 迁移。

新增/复用：

- `SessionQueryState.last_question` / `last_sql` 已在用（C 谓词依赖 `last_sql != None` 区分上一轮是否曾成功）
- 无 schema 变更

---

## 4. 接口契约变更

无 API/契约变更（端到端无可见行为外溢）。

内部 LLM 调用新增一种 purpose：

| purpose | 触发位置 | 说明 |
|---|---|---|
| `follow_up_rewrite` | `_rewriteFollowUpQuestion` | 追问 → 完整多步问题改写；token 与其他目的同等计量 |

---

## 5. 实现要点

### A. `backend/app/services/intent_service.py`

- 新增模块级 `isEllipsisFollowUp(question)` 谓词（公共，与 C 共用）
- `_FOLLOW_UP_ELLIPSIS_RE = re.compile(r"^.{2,14}(?:呢|呐)[？?！!]*$")`
- `_ELLIPSIS_EXCLUDED_PREFIXES` 元组：疑问词起头的「…呢」句排除（守 N6）
- `_LOOSE_SEQUENTIAL_RE`：宽松顺序指令守卫（reviewer Finding 1 修复）——在 `_isFollowUp` 内独立于 `_isExplicitMultiStep`（保留原严格语义，避免误吞「把第一步的结果按金额降序排序」之类单「第X步」指代为 REFINE 的旧行为）
- `_isFollowUp` 闸门顺序：`_isExplicitMultiStep` → `_LOOSE_SEQUENTIAL_RE` → `isEllipsisFollowUp` → 关键词

### B + C. `backend/app/services/chat_service.py`

- 新增常量 `_FOLLOW_UP_RETRY_MAX_LEN = 20`
- 新增三个方法（不动 `_planAndGenerateSql` / `_executeMultiStep` 已有逻辑）：
  - `_isFollowUpRetryCandidate(question, intent, state)` 静态谓词
  - `_rewriteFollowUpQuestion(session, dto, pc, state)` LLM 改写（user input 经 `StepQueryPlanner._sanitize` 转义，prompt 不接受自由指令）
  - `_prepareFollowUpMultiStep(session, dto, pc, state)` 改写 + 拆步 + 累加 token/cost
- 接入两处入口（与原结构镜像一致）：
  - `_handleGenericQuery`（非流式）：B 分支前置；outcome.sql 为 None 时 C 升级
  - `_streamQuery`（SSE）：同上
- `IntentResult` 是 frozen dataclass，重试用 `dataclasses.replace(result, intent=FOLLOW_UP)`；`dto` 用 `model_copy(update={"question": rewritten})`

### 成本与边界

- A：零 LLM 成本（纯正则）
- B：每触发 1 次改写（~200 token）+ 规则拆步（零 LLM，因为改写后问题带「第X步」标号）；改写失败 / 拆不出多步退回原路径
- C：仅当 NEW_QUERY 不可回答 + 短句 + 有历史才重试一次（一次额外的两阶段 LLM 调用）；长句或上一轮无 SQL 不触发
- 重试有边界：`outcome.sql is not None` 后停止（C 只重试一次）

---

## 6. 测试

新增/扩充：

- `backend/app/tests/unit/test_intent_service.py` — 6 个 A 层用例：
  - 正例：4月份呢？ / 那去年呢 → FOLLOW_UP
  - 守卫：无历史 → QUERY；疑问词「…呢」→ NEW_QUERY；显式多步「…呢」→ NEW_QUERY
- `backend/app/tests/integration/test_chat_follow_up_cascade.py` — 8 个 B/C 用例（真实 PG 测试库）：
  - B1：多步上一轮 + FOLLOW_UP → 改写 + 多步重跑（验证 rewrite LLM 被调用、多步 SQL 执行次数、sessionMessage 锚定改写后问题、token 计量 purpose="follow_up_rewrite"）
  - B2：单步上一轮 → 不调用改写器，走单轮 FOLLOW_UP 注入
  - B3：改写 LLM 返垃圾 → 退回单轮 FOLLOW_UP，resp.sql 非空
  - C1：短句 NEW_QUERY 不可回答 + 有历史 → 重试为 FOLLOW_UP 成功
  - C2：上一轮 last_sql=None → 不重试，兜底文案
  - C3：长句不可回答 → 不重试
  - C4（Finding 3）：C 触发且上一轮多步 → 先 B 多步重跑，每步计划仍"无法回答"，各 step.error 非空（验证 _executeMultiStep 失败隔离而非退化 fixed 兜底）
  - C5（Finding 3）：C 重试本身仍不可回答 → 退化到 fixed 兜底，无二次重试（防无限循环）

回归（全部 PASS）：

- `app/tests/unit/test_intent_service.py` — 65/65（含 N6 老用例）
- `app/tests/integration/test_chat_service_state.py` — 25/25
- `app/tests/integration/test_chat_api.py` — 12/12
- `app/tests/integration/test_chat_stream_api.py` + `test_chat_multi_step.py` — 14/14
- `app/tests/unit/test_chat_service.py` + `test_step_query_planner.py` — 161/162（唯一失败为预存的 ADS 权重测试，与本次改动无关，stash 后复跑仍失败）

Reviewer 反馈：0 CRITICAL/HIGH，3 MEDIUM（Finding 1 已修；Finding 2 SSE 测试本次未补——见 backlog；Finding 3 已补两个 C 层用例），4 LOW（Finding 4 长度上限 / Finding 6 改写成功日志——记 backlog；Finding 5 死代码已清；Finding 7 正则边界——记 backlog）。

**Backlog**：
- SSE 路径 B/C 集成测试（需要 fake `completeStream`，超出本次范围；流式逻辑通过代码审查保证镜像一致性）
- 改写输出长度上限（`_clipText(..., _STATE_HISTORY_FIELD_LIMIT)`），防御性
- B 改写成功 INFO 日志（追溯"为什么历史里出现我没问过的问题"）
- A 正则扩展：`它呢`/`它们呢`（canonical 缩写）`4月份呢。`（句号收尾）

---

## 7. 决策记录

1. **为什么用 LLM 改写而非字符串拼接**？追问里改动的可能是时间/条件/排序/列名中的任意一种，字符串规则漏概率高；LLM 改写一次即可（成本可控），且能把追问与上一轮问题统一回结构化多步。
2. **为什么 C 只重试一次**？用户提的"成本可控 + 不阻塞 + 兜底"；无限重试/树形回退会让体验比当前"无法回答"更糟。
3. **为什么 A 排除疑问词前缀**？N6 文档化的权衡：泛化疑问（"为什么A公司最多呢"）是无前置实体的真新问题，误锚定的代价高于误判 NEW_QUERY。
4. **为什么 state.last_sql 必须非 None**？上一轮本身就是"无法回答"时追问没有可锚定的查询，重试只会空耗 token。
5. **不修改 dto.question 的存储**？B 重跑多步时通过 `dto.model_copy` 创建 `dto2` 替换问题；落库消息与查询状态都锚定改写后的完整问题（支持下一轮继续追问）。前端显示由客户端 echo，不感知服务端改写。

---

## 8. 文件清单

变更：

- `backend/app/services/intent_service.py`
- `backend/app/services/chat_service.py`
- `backend/app/tests/unit/test_intent_service.py`

新增：

- `backend/app/tests/integration/test_chat_follow_up_cascade.py`
---

## 9. 回归修复（2026-09-26）：改写结果等于上一轮时不应判为无效

**现象**：多步跑完后重发同一追问（例：上一轮 5 月三步问题 → 用户又问"5月份呢"），
返回「无法回答（LLM 判定无有效查询计划）」。更普遍地，凡改写器把追问合并回上一轮
完整问题、结果与 `prior` 逐字相同的追问，都退化失败。

**根因**（`_rewriteFollowUpQuestion` 守卫）：

```python
if rewritten in (prior, dto.question.strip()):   # 旧守卫
    return None
```

`rewritten == prior` 被当作"改写无效"丢弃 → B 层第二道闸门失败 → 退回单轮
FOLLOW_UP 状态注入。但单个追问是 4 字短句（"5月份呢"），单轮计划无从下手 →
`outcome.sql is None` → 固定兜底文案。而 C 层（短句升级重试）明确排除 FOLLOW_UP，
**B 是该意图的唯一机会，改写失败即无兜底**。

**复合放大**：失败那一轮会把短句写回 `last_question`（`_saveQueryState(question=dto.question,
sql=None)`）。此后 `_prepareFollowUpMultiStep` 闸门 1（上一轮须是多步）永久为假 →
该会话后续所有追问都拿不到 B 层，形成不可自愈的死局——这解释了用户"又问一次还是
不行"。

**判据（2026-09-26 07:41 容器日志）**：`follow_up_rewrite` 用量行（180 token）出现后
没有任何 `拆步规则命中` 日志，紧接着是一次 11224 token 的单轮计划调用 → 改写成功但
被守卫丢弃，请求从未进入多步。

**修法**：只丢弃真正无用的改写——空串，或只是回显追问本身（合并未发生）。与
`prior` 相同视为**有效**，语义是"照上一轮的问题重跑一遍"，正是用户重发追问时想要的。

```python
if not rewritten or rewritten == dto.question.strip():
    return None
```

**回归测试**：`test_chat_follow_up_cascade.py::TestFollowUpMultiStepRerun::
test_rewrite_equal_to_prior_reruns_prior_multi_step`（先 RED：`'follow_up' == 'multi_step'`
失败 → 修后 GREEN）。同文件 9/9 通过。

**真机验证**（`/chat/stream`，modelId=1，session `smoke-followup-1790409826`）：

| 轮次 | 用户输入 | 改写 | 事件 | 结果 |
|---|---|---|---|---|
| 1 | 三步 3 月问题 | 未调用 | multi_step_plan 1 / step_plan 4 / step_result 3 | 3 月报告 |
| 2 | `3月份呢` | 180 tok → prior | multi_step_plan 1 / step_plan 4 / step_result 3 | 3 月报告（修复前为「抱歉…」） |
| 3 | `4月份呢` | 180 tok → 4 月 | multi_step_plan 1 / step_plan 4 / step_result 3 | 4 月报告 |

三轮回答均不含「抱歉」，`last_question` 收尾为完整 4 月三步问题（下一轮追问仍可级联）。

**后续修复（2026-09-26）**：
[fix-c-fallback-global-filters](../fix-c-fallback-global-filters/summary.md) —— 本特性的 C 兜底分支
（`_isFollowUpRetryCandidate` → `_prepareFollowUpMultiStep` → 多步重跑）漏传 `global_filters`，
而同级 B 分支传了 ⇒ 同一段多步代码因入场点不同而丢掉跨步口径约束（非流式/流式对称缺口）。
修法：把全局约束抽取收敛进共享前置 `_prepareFollowUpMultiStep`（4 个入场点只透传）。

**相邻缺陷（已另行修复）**：本次排查顺带发现两个独立缺陷，已由
[fix-chat-llm-keyless-and-degrade](../fix-chat-llm-keyless-and-degrade/summary.md) 修复——

1. 模型路由在 `sessionCost >= SESSION_BUDGET`（`docker/.env` = 0.1）时强制 `_cheapest` =
   `Qwen3.8-27B-4bit`（`llm_config.id=3`，**无 API key**）→ `createClient` 返回 None →
   裸 `AttributeError` 500。**此处曾误判「前端始终下发 modelId 故聊天路径不受影响」**：
   `chatStore.ts` 的 `selectedModelId` 初值为 `null` 且不持久化、`ChatPanel` 无自动选中，
   **刷新页面后前端自己就会不带 modelId 发请求**，同一会话成本已持久化过预算线即 500。
2. `_streamMultiStep` 后循环降级分支（无 aggregation 步骤）不落库、不保存查询状态，
   与本段记录的「短句写回 `last_question`」是同一种状态污染（当前生产不可达，属潜在隐患）。
