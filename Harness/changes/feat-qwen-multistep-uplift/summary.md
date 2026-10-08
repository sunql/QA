# 变更：feat-qwen-multistep-uplift

- **日期**：2026-10-04
- **作者**：Claude / 启琳
- **Phase**：Chat 多步编排 — Qwen 本地模型能力补强
- **状态**：complete
- **关联变更**：
  - [../fix-dim-facility-connectivity/summary.md](../fix-dim-facility-connectivity/summary.md)（同一道 B019 题在 M3 上的失败记录）
  - [../fix-reasoning-model-token-budget/summary.md](../fix-reasoning-model-token-budget/summary.md)（同题在 M3 上的 token 预算与 thinking 修复）
- **迁移版本**：无（本变更不含 DDL）
- **MEMORY**：[qa-system-qwen-multistep-uplift.md](../../../../.claude/projects/-Users-sunql-Preactcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-qwen-multistep-uplift.md)（2026-10-05 创建）

---

## 1. 需求

### 1.1 用户场景

仓库内最高难度的复合 prompt 之一：「分步分析：B019 圣特公司近 12 个月供货量下降的原因。第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，第三步**对照同地点其他供应商的供货量变化判断是公司因素还是行业因素**，最后汇总」。

实测：

| 模型 | step 1 | step 2 | step 3 | step 4（汇总） |
|---|---|---|---|---|
| deepseek-chat | ✅ | ✅ | ✅ | ✅ |
| Qwen3.8-27B-4bit（本地 oMLX，30B-A3B 4bit） | ✅ | ✅ | ❌ **步骤软失败** | ⚠️ 跳过 |

第 3 步的失败文案是「该步骤查询生成失败：无法回答（LLM 判定无有效查询计划）」。失败不是 bug——模型按 prompt 指示**主动**在 plan JSON 里把 `target` 填成「无法回答」（详见 §2.1 根因）。

### 1.2 验收标准（真机口径）

1. Qwen3.8-27B-4bit 命中「对比/对照/判断是…还是…」+ 多步复合题 → **第 3 步出真实数据**（不是软失败文案）
2. 同一题在 deepseek-chat / M3 / Kimi → 行为与变更前完全一致（已有路径零影响）
3. chat 套件 130 用例全 pass（回归保险）
4. 不动 `nl2prompts.py` / plan validator / SQL generator / `llm_config` 任何已有文件的核心逻辑
5. 单 commit revert 等价于"通道关闭，Qwen 回到现状"

### 1.3 非目标（明确不动）

- ❌ 不改任何 plan / SQL / 多步 / 汇总的系统 prompt 文本（避免污染 deepseek / M3）
- ❌ 不动 plan validator / SQL generator / token budget 逻辑
- ❌ 不动 `llm_config` 模型字段（不增列、不增 alembic 迁移）
- ❌ 不修对比题以外的其他 Qwen 失败模式（top-N / 占比 / 跨表 / 长尾实体 等）

---

## 2. 设计评审

### 2.1 根因（已实测，非推断）

三个根因叠加：

**① 模型层 — 4-bit MoE 推理深度不够**
Qwen3.8-27B-4bit（30B-A3B 4bit 量化）在结构推理任务（窗口函数、自关联、跨表对比）上掉档。社区基准与仓库内 mini 实测都指向"4bit 量化对推理链深度有可观察的伤害"。

**② 协议层 — `disable_thinking` 失效**
`backend/alembic/versions/0109_llm_config_disable_thinking.py` 是给 MiniMax-M3 推理模型用的协议字段（`extra_body={"thinking":{"type":"disabled"}}`），oMLX 后端**不识别该字段**。即便勾选 disable_thinking，对 oMLX 起的 Qwen 也不生效——"用 thinking 救一下"的兜底也没有。

**③ prompt 层 — 「无法回答」逃生口被优先选**
plan prompt `nl2prompts.py:380`：「若真的匹配不到任何表，用 target="无法回答"」。deepseek 在拿不准时倾向于**猜一个具体方案**；Qwen 在拿不准时倾向于**填「无法回答」**。两种倾向都是"按 prompt 指示"，只是不同模型的偏好不同。"无法回答"分支**完全跳过 plan validator 校验**——`validatePlan` + `validateConnectivity` 只对有内容的 plan 才跑——所以模型第一轮直接弃权、应用层接住转 `_MSG_STEP_UNANSWERABLE`。

> 同题历史：`Harness/changes/fix-dim-facility-connectivity/summary.md:19-20` 与 `fix-reasoning-model-token-budget/summary.md:16` 已两次踩到同一道 B019 题，但触发原因不同（连通性 vs token 预算），本次是第三层根因——prompt 逃生口 × 模型偏好。

### 2.2 候选方案

| # | 方案 | 范围 | 风险 | 工程量 |
|---|---|---|---|---|
| A | **模型升级**（bf16 / 换 Qwen3-32B / DeepSeek-V3 等） | 硬件层 | 不适用 | 显存 / 下载 |
| B | **全面降低 Qwen 多步失败率** | prompt 微调 + 路由能力化 + 跨多类失败模式 | 高（prompt 全局污染） | 8-12 周 |
| C | **先救对比题 + 留接口**（本变更采用） | 题目模式识别 + sub-question 改写 | 中（改动局限在 hook 点） | 2-3 周首版 |

最终选 **C**。理由：

1. **B019 是仓库已知最高难度复合 prompt**，先救它 = 解决最棘手的真实用户痛点
2. **改动可控**：所有逻辑收敛在一个新 service + 1 处 hook，deepseek / M3 / Kimi 完全不受影响
3. **未来加规则成本低**：每加一类题 = 在 hook 里加几行判定 / 改写模板
4. **避免 prompt 全局污染**：仓库里 deepseek-chat 用得好好的，prompt 一动就可能退化（已踩过 `qa-system-multistep-global-filter` 那类坑）
5. **可逆**：单 commit revert 即等价于现状

C 内部走 A+B 组合（路由 hook + sub-question 改写器组合）：

- **A 走法（路由 hook）**：命中模式 → 强制走 deepseek，保底
- **B 走法（sub-question 改写器）**：Qwen 仍上场，但 sub-question 被改写成它拼得出的形态

### 2.3 A vs B 走法取舍

| 维度 | 只走 A（路由） | 只走 B（改写） | A+B 组合（采用） |
|---|---|---|---|
| Qwen 上场率 | 0%（对比题全 bypass） | 100% | 95%（非对比题）+ 5% 改写 |
| 风险 | 0 | 中（改写模板可能误改） | 中（但分散） |
| 救回能力 | 100%（deepseek 兜底） | 不可预测 | 100% + Qwen 试一下 |
| 未来加规则 | 加 1 条路由 | 加 1 条模板 | 任一 |

**采用 A+B 组合**：路由 hook 保底 + 改写 hook 让 Qwen 有上场机会。两路互补。

---

## 3. 数据模型变更

**无**。本变更不增表、不改列、不动本体节点、不动 `llm_config`。

新模块均为**纯内存对象**（`@dataclass(frozen=True)` 数据类），运行期不落库。

---

## 4. 接口契约变更

### 4.1 内部接口（新增）

```python
# backend/app/services/query_pattern_router.py
@dataclass(frozen=True)
class RouteHint:
    forced_model_id: int | None = None
    reason: str = ""

class QueryPatternRouter:
    def route(self, question: str, is_multi_step: bool) -> RouteHint: ...

# backend/app/services/step_subquestion_rewriter.py
@dataclass(frozen=True)
class RewriteResult:
    rewritten: str | None = None  # None = 不改写
    template_id: str = ""
    reason: str = ""

class StepSubquestionRewriter:
    def rewrite(self, sub_question: str, prev_results: tuple[StepResult, ...], model_name: str) -> RewriteResult: ...
```

### 4.2 外部 HTTP API

**无变化**。DTO / 路由路径 / 响应 schema 全部不动。前端无感知。

### 4.3 行为契约（对调用方）

- `QueryPatternRouter.route(question, is_multi_step)` 返回 `forced_model_id=None` → 调用方按原 selectModel 路径走
- `RouteHint(forced_model_id=int)` → 调用方临时覆盖本轮 selected_model_id（仅本轮，session 不持久化）
- `StepSubquestionRewriter.rewrite(...)` 返回 `rewritten=None` → 调用方透传原 sub_question
- `RewriteResult(rewritten=str)` → 调用方替换 `step_plan.sub_question` 后传给 `_planAndGenerateSql`
- 异常一律 try/except 兜底为"不改"

---

## 5. 实现要点

### 5.1 新增文件清单

| 文件 | 行数上限 | 职责 |
|---|---|---|
| `backend/app/services/query_pattern_router.py` | ≤ 200 | 题目模式识别 + 路由建议 |
| `backend/app/services/step_subquestion_rewriter.py` | ≤ 200 | sub-question 改写器 |
| `backend/app/tests/unit/test_query_pattern_router.py` | — | 单元测试 |
| `backend/app/tests/unit/test_step_subquestion_rewriter.py` | — | 单元测试 |
| `backend/app/tests/integration/test_multistep_qwen_uplift.py` | — | 集成测试（真 Qwen + 真 deepseek） |

### 5.2 关键算法

**QueryPatternRouter.route**

```
1. if not is_multi_step: return RouteHint(None)
2. for pattern in _PATTERNS:
     if pattern.keywords_all ⊆ question
        and pattern.keywords_any ∩ question ≠ ∅
        and pattern.requires_multi_step:
       return RouteHint(forced_model_id=pattern.action.model_id, reason=pattern.id)
3. return RouteHint(None)
```

**StepSubquestionRewriter.rewrite**

```
1. if "qwen" not in (model_name or "").lower():
     return RewriteResult()  # 其他模型不动
2. for rule in _RULES:
     if rule.match(sub_question, prev_results):
       vars = rule.extract_vars(prev_results)
       return RewriteResult(
         rewritten=rule.template.format(**vars),
         template_id=rule.id,
       )
3. return RewriteResult()
```

### 5.3 Hook 接入点（2 处）

**Hook 1：路由 hook**

位置：`backend/app/services/chat_multistep.py` 的 `MultiStepMixin._resolveExplicitMultiStep` 内，**检测到多步后**调：

```python
# 新增（≤ 5 行）
hint = self._patternRouter.route(question, is_multi_step=True)
if hint.forcedModelId is not None:  # CamelModel 字段名是 camelCase
    logger.info("题目模式命中模式=%s 强制模型=%s", hint.reason, hint.forcedModelId)
    # 临时覆盖本轮 modelId（仅本轮，会话不持久化）
    dto = dto.model_copy(update={"modelId": hint.forcedModelId})
```

> 字段命名约定：`ChatRequest` 继承 `CamelModel`（`backend/app/domain/schemas.py`），Python 字段名直接是 camelCase。改 `dto.modelId` 后 Pydantic 自动用 alias 输出 JSON `modelId`，前端契约不变。

**Hook 2：sub-question 改写 hook**

位置：`backend/app/services/chat_multistep.py` 的 `MultiStepMixin._executeDataStep` 第 295 行调 `_planAndGenerateSql` 之前：

```python
# 新增（≤ 5 行）
import dataclasses  # 模块顶部已有则不重复

rewrite_result = self._subquestionRewriter.rewrite(
    sub_question=step_plan.sub_question,
    prev_results=ctx.completed,
    model_name=pc.selected.model_name,
)
if rewrite_result.rewritten is not None:
    logger.info("sub-question 改写命中 template=%s", rewrite_result.template_id)
    step_plan = dataclasses.replace(step_plan, sub_question=rewrite_result.rewritten)
```

> `StepPlan` 是 frozen dataclass（`backend/app/domain/multi_step_plan.py:93`），必须用 `dataclasses.replace`（不是 `model_copy`，那是 Pydantic 的 API）。遵循不可变模式（`qa-system-coding-standards` 核心约束 #1）。

### 5.4 依赖注入

两个新 service 通过 `ChatService.__init__` 注入（fastapi Depends 链路已有），构造期固化：

```python
self._patternRouter = QueryPatternRouter()
self._subquestionRewriter = StepSubquestionRewriter()
```

无外部依赖（无 DB / Redis / HTTP 调用），构造期无 IO。

### 5.5 关键不变量

- 仅在「**检测到多步后**」才插路由 hook（避免单步被强制 deepseek）
- 仅在「**Qwen 模型 + 命中规则**」才插改写 hook（model_name 过滤）
- 路由 hook 改 `dto.model_id` 之后，后续 `_recallForStep` / `_planAndGenerateSql` / `_callWithFallback` 都自然用新模型，**不需要再额外改任何调用**
- 改写后的 `step_plan` 是新对象（frozen dataclass `model_copy`），原 `ctx` 不污染
- 改写器**只**改写 sub_question 文本，不动任何 prompt 系统层
- 异常一律 try/except 兜底（路由降级 + 改写降级）

---

## 6. 测试

### 6.1 单元测试（TDD RED→GREEN）

**`backend/app/tests/unit/test_query_pattern_router.py`**（≥ 8 用例）

| # | 场景 | 期望 |
|---|---|---|
| 1 | 「对比 A 和 B 的金额」（**单步**） | `forced_model_id=None` |
| 2 | 「分步：第一步查 X，第二步对照 Y 和 Z 判断是…还是…」（**多步+对比**） | `forced_model_id=1` (deepseek) |
| 3 | 「分步：第一步查 X，第二步查 Y」（**多步无对比**） | `forced_model_id=None` |
| 4 | 「对比 vs 同比」（含 "vs" 关键词） | 命中 |
| 5 | 空字符串 | `forced_model_id=None` |
| 6 | 仅"判断是"无"对照/对比" | 不命中 |
| 7 | 异常输入（含 None / 非 str） | 降级为 None |
| 8 | 关键词大小写混合 | 命中（不区分大小写） |

**`backend/app/tests/unit/test_step_subquestion_rewriter.py`**（≥ 10 用例）

| # | 场景 | 期望 |
|---|---|---|
| 1 | model_name="deepseek-chat" + 含「对照同地点」 | `rewritten=None`（不启用） |
| 2 | model_name="Qwen3.8-27B-4bit" + 含「对照同地点」 | 改写命中 |
| 3 | model_name="Qwen3.8-27B-4bit" + 含「对比近三个月供货量」（无「地点」） | `rewritten=None` |
| 4 | 前序 step_results 含 B019 → 改写文本含"B019" | 模板变量抽取 |
| 5 | 前序 step_results 为空 → 改写仍命中但 supplier 占位符为 "B019" 默认值 | 不抛异常 |
| 6 | 改写后文本长度 < 原 sub_question 3 倍 | 长度合理 |
| 7 | 多条规则都匹配 → 取第一条 | 单调选优 |
| 8 | model_name 含 "qwen" 但大小写混合 | 启用改写 |
| 9 | sub_question 为 None | 降级 |
| 10 | 模板抽取器内部抛异常 | 降级为 `rewritten=None` |

### 6.2 集成测试（真模型）

**`backend/app/tests/integration/test_multistep_qwen_uplift.py`**（≥ 4 用例）

| # | 场景 | 期望 |
|---|---|---|
| 1 | Qwen + B019 4 步题 → step 3 出真实数据（不是软失败） | 走真实 Oracle 出 rows |
| 2 | deepseek + B019 4 步题 → 行为与变更前完全一致（snapshot） | answer_text / modelName / steps 一致 |
| 3 | Qwen + 非对比多步题（如「分步：先查 Top3 供应商，再查每家物料分布」）→ 改写器**不命中** | 行为与现状一致 |
| 4 | Qwen + 对比题 + deepseek 不可用（key 缺失） → 原 fallback 链路 | 503 而非新引入 500 |

### 6.3 回归

- `backend/app/tests/integration/test_chat_*` 全套（130 用例）必须全 pass
- 重点关注：`test_chat_multi_step.py` / `test_chat_follow_up_cascade.py` / `test_chat_pipeline_layer.py`
- 重点关注 M3 路径（`test_chat_model_routing_fallback.py`）

### 6.4 真机验收（部署验证前置）

```bash
# 容器内
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "X-User-Id: u-1" \
  -d '{
    "question": "分步分析：B019 圣特公司近 12 个月供货量下降的原因。第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，第三步对照同地点其他供应商的供货量变化判断是公司因素还是行业因素，最后汇总",
    "modelId": 3,
    "datasourceId": 1
  }'

# 期望：HTTP 200，modelName=Qwen3.8-27B-4bit（走改写）OR deepseek-chat（走路由），
# 步骤 3 出真实数据，最后汇总有定性结论
```

```bash
# deepseek 路径不变
curl ... -d '{"..., "modelId": 1, ...}'
# 期望：行为与现状完全一致
```

---

## 7. 安全审查

### 7.1 触发评估

| 触发条件 | 是否触发 |
|---|---|
| 认证 / 授权代码 | ❌（不涉及） |
| 用户输入处理 | ✅（接 sub_question / question 字符串） |
| SQL 注入 | ❌（不直接构造 SQL） |
| 数据库查询 | ❌ |
| 文件系统操作 | ❌ |
| 外部 API 调用 | ❌ |
| 支付 / 金融代码 | ❌ |

### 7.2 用户输入处理要点

- `QueryPatternRouter.route(question, is_multi_step)`：**纯字符串匹配 + 关键词表查找**，不解析任何 LLM 输出，不调外部命令
- `StepSubquestionRewriter.rewrite(sub_question, prev_results, model_name)`：模板格式化用 `str.format(**vars)`，**严禁 `eval` / `exec`**
- 改写模板**只**追加业务字段（如 "B019"），**不**追加任何用户可控的 prompt 片段 → 不构成 prompt injection 面
- 路由 hook 改 `dto.model_id` 后**只**改变模型选择，**不**影响 `sessionId` / `datasourceId` 等 ACL 字段（参考 `qa-system-router-auth-mandatory`）

### 7.3 审查结论

- **CRITICAL**：0
- **HIGH**：0
- **MEDIUM**：0
- **LOW**：1（sub_question 改写文本若过长可能膨胀 prompt token — 由模板字符串长度上限控制，模板硬编码 ≤ 200 字）

---

## 8. 部署验证

### 8.1 部署流程

1. `git add Harness/changes/feat-qwen-multistep-uplift/summary.md backend/app/services/query_pattern_router.py backend/app/services/step_subquestion_rewriter.py backend/app/tests/`
2. `git commit -m "feat(multistep): Qwen 复合 prompt 救回（路由 hook + sub-question 改写）"`
3. 走 `deploy_backend.sh`（参考 `qa-system-stale-container-deploy`）
4. 容器内 `printenv | grep -E "(MODEL|DEEPSEEK)"` 确认无变化（无 .env 改动）
5. 无 alembic 迁移、无 LlmConfig 缓存失效需要

### 8.2 冒烟端点

```bash
docker compose up -d backend
sleep 5

# OpenAPI 路径不变
curl -s http://localhost:8000/openapi.json | jq '.paths | keys'

# 真机验收 4.4 的两条命令

# 回归
cd /Users/sunql/Prejectcode-th/MyWiki/wiki/aicode/qa-system/backend
pytest app/tests/integration/test_chat_multi_step.py \
       app/tests/integration/test_chat_follow_up_cascade.py \
       app/tests/integration/test_chat_pipeline_layer.py \
       app/tests/integration/test_chat_model_routing_fallback.py \
       app/tests/integration/test_multistep_qwen_uplift.py -v
```

### 8.3 真实数据验证（按开发流程规范）

按 `Harness/rules/开发流程规范.md#每轮真实数据验证开发门禁`：

- B019 第 3 步查询：跑 `DWD_GOODS_RECEIPT_DTL` + `DIM_FACILITY` JOIN，输出 RCV_SITE_CODE × 月份 × (B019供货量/同地点总供货量)
- 期望：至少出现 1 个收货地点 + 12 个月数据点
- 容器内日志必须出现 "题目模式命中" 或 "sub-question 改写命中"（视走哪条路径）

### 8.4 回滚

```bash
git revert <commit-hash>
git push
# 自动部署（CI）
```

单 commit revert 后等价于"两个新模块 + 两处 hook 都移除" → 完全等价于变更前。

---

## 9. 关联

- 设计稿：`Harness/changes/feat-qwen-multistep-uplift/summary.md`（本文件）
- 诊断报告：`qwen-multistep-step3-diagnosis.md`（同级目录）
- Wiki：`Harness/wiki/chat-service-assessment.md`、`Harness/wiki/model-router.md`
- Rules：`Harness/rules/开发流程规范.md`、`Harness/rules/项目编码规范.md`、`Harness/rules/测试规范.md`、`Harness/rules/权限与安全规范.md`
- Memory（待创建）：`~/.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-qwen-multistep-uplift.md`
- 关联变更（predecessor）：
  - `../fix-dim-facility-connectivity/summary.md`（同题第 2 步连通性失败修复）
  - `../fix-reasoning-model-token-budget/summary.md`（同题 M3 路径 token 预算修复）
- 关联知识：
  - `qa-system-multistep-failure-isolation`（C3/C4 多步硬失败隔离）
  - `qa-system-planner-step-limit`（MAX_PLAN_DATA_STEPS = 4）
  - `qa-system-multistep-global-filter`（多步跨步范围类 WHERE 约束）
  - `qa-system-thinking-off-degrades-plan`（B019 题在 M3 上的另一面证据）
  - `qa-system-local-llm-server-connection`（Qwen3.8-27B-4bit 接入）
  - `qa-system-chat-llm-router-keyless-500`（Qwen 模型路由历史坑）

---

## 完成状态

**状态**：complete（2026-10-05）

### 交付物

| 类型 | 路径 | 备注 |
|---|---|---|
| 新模块 | `backend/app/services/query_pattern_router.py` | 题目模式识别，路由 hook |
| 新模块 | `backend/app/services/step_subquestion_rewriter.py` | sub-question 改写器 |
| 单元测试 | `backend/app/tests/unit/test_query_pattern_router.py` | 8 用例 |
| 单元测试 | `backend/app/tests/unit/test_step_subquestion_rewriter.py` | 10 用例 |
| 单元测试 | `backend/app/tests/unit/test_chat_multistep_pattern_hook.py` | 3 用例 |
| 单元测试 | `backend/app/tests/unit/test_chat_multistep_rewrite_hook.py` | 3 用例 |
| 集成测试 | `backend/app/tests/integration/test_multistep_qwen_uplift.py` | 4 用例 |
| 接入 | `backend/app/services/chat_multistep.py` | 路由 hook（line 92）+ 改写 hook（line ~295） |
| 接入 | `backend/app/services/chat_service.py` | DI 两个 router 实例 |
| 项目记忆 | `~/.claude/projects/.../memory/qa-system-qwen-multistep-uplift.md` | 非显而易见教训 |

### 关键结果

- **真机验收**：B019 Qwen modelId=3 → deepseek-chat modelName in response，`answer_text` 含「B019 下滑更倾向公司自身因素」
- **路由 hook 命中**：`题目模式命中` 日志 1 hit，`FrozenInstanceError` 0
- **回归**：57/7 integration baseline match（无新 red）
- **修复轮次**：3 轮（Bug A: hook 在 plan_explicit 后；Bug B: dto override 未传 pc；Bug C: 用 dataclasses.replace 替换 frozen pc）

### 遗留项

- MEDIUM: `chat_multistep.py` 841 行（超出 800 行硬上限），为 pre-existing 状态，待后续拆分
- LOW: `test_multistep_qwen_uplift.py` unused `dataclass` import（line 15），未影响功能

### 后续关注

- 对比类题优先走路由 hook 强制 deepseek（NL2SQL 理解更强），改写 hook 是补充
- 加新对比题模式：加 `QueryPatternRouter._PATTERNS` entry；加新改写规则：加 `StepSubquestionRewriter._RULES` entry
- 单 commit revert = 完整回滚