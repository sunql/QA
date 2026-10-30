# 诊断报告：Qwen3.8-27B-4bit 在多步分析第 3 步「无法回答」

- **日期**：2026-10-04
- **场景**：同一份 4 步复合问题，deepseek-chat 完整跑通，Qwen3.8-27B-4bit 第 3 步返回「无法回答（LLM 判定无有效查询计划）」
- **原始问题**：「分步分析：B019 圣特公司近 12 个月供货量下降的原因。第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，第三步对照同地点其他供应商的供货量变化判断是公司因素还是行业因素，最后汇总」
- **结论先行**：失败发生在**模型层**，不在应用层；deepseek-chat 与 Qwen3.8-27B-4bit 都**走到**了计划阶段，但 Qwen 的计划 JSON 把 `target` 字段填成了「无法回答」——这是模型主动放弃的语义，不是解析失败、不是校验失败。

> **归档说明（2026-10-30）**
>
> - 本文档原落在**仓库根目录**，与 `CLAUDE.md` / `README.md` 并列，违反仓库文档归置约定 ⇒ 移入 `docs/`。
> - **行号已漂移**（本文写作于 2026-10-04 当时的代码）：`chat_stream.py` 已于 PR #54 拆成 5 个 mixin，
>   `chat_multistep.py` 的软失败分支现落在 `backend/app/services/chat_multistep.py:355-358`
>   （原文写 316-321），文案常量已上移到
>   `backend/app/services/messages_zh.py:706`（`MSG_CHAT_STEP_UNANSWERABLE`，
>   经 `chat_helpers.py:36` 以 `_MSG_STEP_UNANSWERABLE` 别名导出；原文写 chat_helpers.py:244-245）。
> - **结论与机制描述仍然成立**（`outcome.plan.isUnanswerable` → 步骤级软失败隔离），
>   本文保留作为 Qwen 多步失败模式的原始现场记录。

---

## 1. 现象（用户口述）

| 模型 | 模型 | 步骤 1 | 步骤 2 | 步骤 3 | 步骤 4（汇总） |
|---|---|---|---|---|---|
| deepseek-chat | 模型 id=1，max_input=64000，weight=10 | ✅ | ✅ | ✅ | ✅ |
| Qwen3.8-27B-4bit | 模型 id=3，max_input=32768，weight=12，本地 oMLX | ✅ | ✅ | ❌ 步骤软失败 | ⚠️ 跳过汇总（前置数据步有失败） |

失败卡片文案（前端展示原文）：

> 该步骤查询生成失败：无法回答（LLM 判定无有效查询计划）

注意关键短语：**「LLM 判定」**——意思是后端把模型返回的某个字段当作权威判定，没有自作主张。

---

## 2. 错误文案是从哪里冒出来的（应用层机制）

失败路径**只可能是**这一条（位于 `backend/app/services/chat_multistep.py:316-321`）：

```python
if outcome.sql is None or outcome.plan is None or outcome.plan.isUnanswerable:
    # 软失败（LLM 判定无法回答）：非硬异常，一直就是步骤级隔离
    return _StepRun(
        result=_failedStepResult(step_plan, _MSG_STEP_UNANSWERABLE),
        ...
    )
```

常量 `_MSG_STEP_UNANSWERABLE` 定义在 `backend/app/services/chat_helpers.py:244-245`，值正是用户看到的那行字。

这条分支的触发条件有三个（**OR**）：
1. `outcome.sql is None` —— SQL 生成失败
2. `outcome.plan is None` —— 计划 JSON 完全没解析出来（解析层异常）
3. **`outcome.plan.isUnanswerable`** —— 模型**主动**在计划 JSON 里写了 `target="无法回答"`

只有第 3 个条件带「LLM 判定」字样。结合后端日志与已知证据，Qwen 走的**就是这一条**：

```python
# backend/app/domain/query_plan.py:35 + 168-174
UNANSWERABLE_TARGET = "无法回答"

@property
def isUnanswerable(self) -> bool:
    return self.target == UNANSWERABLE_TARGET
```

计划 prompt（`nl2prompts.py:327` / `380`）**主动教**模型在拿不准时填 `target="无法回答"`：

> 2. 若无法匹配任何表，target 填"无法回答"，其余字段留空。

所以 Qwen 的失败模式**不是 bug**，是模型**按 prompt 指示**选的字面量答案。

---

## 3. 为什么 deepseek-chat 不「无法回答」、Qwen3.8-27B-4bit 会

三个证据点一起指向同一个根因，叠加放大。

### 3.1 这道题的 prompt 在两个月内「实测」被两个不同模型绊倒过

`fix-reasoning-model-token-budget/summary.md:16` 记录的 mini 完整复刻题：

> B019 圣特公司供货量下降的原因，重点看不同收货地点的变化

跑在 MiniMax-M3（推理模型）上时，连续 3 次 HTTP 400，**plan reason 全为 `PLAN_REPLY_EMPTY`**——同一 prompt、同一个底层校验链；切到 deepseek-chat 立即跑通。

`fix-dim-facility-connectivity/summary.md:19-20` 原文直接引用：

> 分步分析：B019 圣特公司近 12 个月供货量下降的原因。第一步统计各月供货量趋势，第二步按收货地点拆分各月供货量，**第三步对照同地点其他供应商的供货量变化**……

这道题第 2 步在 MiniMax-M3 上整步报废，**触发点就是 DIM_FACILITY 的本体连通性**——`validateConnectivity` 报「无法通过关联路径连通：DIM_FACILITY」。

**这两条历史记录合起来说明**：B019 多步题 = 本仓库里**最高难度的复合 prompt**。第 2/3 步需要模型同时做到：
- 在本体里认出收货地点 = `DIM_FACILITY`（而不是凭直觉造表）
- 想到 `DWD_GOODS_RECEIPT_DTL.RCV_SITE_CODE → DIM_FACILITY.FCY_0` 这个**值域 100% 重叠的连接键**（由用户手动补的 4 条边之一）
- 第 3 步要构造一个**对比口径**——「B019 的供货量趋势 vs 同地点其他供应商的供货量趋势」，这不是 JOIN 能直接出答案的，需要窗口函数 / 子查询 / `partition by SITE_CODE` 一类的相对聚合

deepseek-chat 模型能做到这一步。Qwen3.8-27B-4bit 做不到，下文三条原因。

### 3.2 4-bit 量化的 MoE 在结构推理上掉档（主因）

`backend/seed_models.py:42-59`：

```
Qwen3.8-27B-4bit  (MoE, 4bit)
- 30B-A3B（激活参数 ~3B，总参数 30B）
- 4bit 量化 → 内存换精度
- max_input_tokens=32768
```

4-bit 量化对模型**推理链深度**的伤害在社区基准上早有数据。本仓库没有跨模型基准，但有**直接相关的工程证据**：

- `Harness/changes/fix-reasoning-model-token-budget/summary.md:88-95`：同是「关掉思维链」实测，MiniMax-M3 的对比结果是「带 thinking 能先推理『B019 是编码不是名称』，产出干净单表；关掉后选多表且口径不确定，撞本体连通性校验」——**关掉/削弱 reasoning 路径**会让模型对**编码 vs 名称、连接键归属、跨表对比口径**这类问题**更容易选错**。
- Qwen3.8-27B-4bit 没有 `disable_thinking` 配置（`backend/alembic/versions/0109_llm_config_disable_thinking.py` 是给推理模型 M3 用的；oMLX 后端无对应协议字段，**目前线上 Qwen3.8-27B-4bit 的 `disable_thinking` 永远是 False 但也不生效**）——本来「有 thinking 救一下」的兜底也没有。
- 4bit + MoE 在量化时容易出现**专家路由偏移**：低 bit 下路由器权重精度损失，可能让「对比推理」专家在 Step 3 这种长上下文里被静音，结果是模型的 response 退化到「不懂就放弃」。

### 3.3 prompt 给了「无法回答」这个逃生口，小模型会优先选

prompt 中 `nl2prompts.py:380`：

> selectedClasses / ... / 至少填一项；若**真的**匹配不到任何表，用 target="无法回答"。

deepseek-chat 在拿不准时倾向于**猜一个具体方案**（即使多表、口径可能跑偏）；Qwen3.8-27B-4bit 在拿不准时倾向于**填「无法回答」**——这两种倾向都是「按 prompt 指示」，只是不同模型在「不确定」时的偏好不同。

「无法回答」这个分支之所以**特别危险**，是因为它**完全跳过了校验链路**：plan validator (`validatePlan` + `validateConnectivity`) 只对**有内容的 plan**才跑；对「无法回答」它**直接放行**——所以 `_planIssues` 返回空列表、`generateValidatedPlan` 不会触发任何重试（`nl2service.py:556-589` 的 retry 循环对空 issue 直接返回），模型第一轮直接弃权、应用层接住后转 `_MSG_STEP_UNANSWERABLE`。

### 3.4 步骤 3 的 prompt 复杂度被前序注入「撑大」

`backend/app/domain/multi_step_plan.py:223-313` 的 `inject_to_prompt(current_index)`：每步 SQL 生成时都会拿到**前序步骤的实体列表/聚合值**，第 3 步会收到 step 1 的「B019 12 个月各月供货量」+ step 2 的「B019 各收货地点 × 月供货量矩阵」。

deepseek-chat 拿到这堆东西后能写出带 `partition by` 的窗口聚合；Qwen3.8-27B-4bit 拿到的输入比 step 1/2 长得多（累积了 2 步的实际数据），**推理深度被压扁**，「对比口径」这一步就崩了。

---

## 4. 不是的原因（验证证据）

排除几个会让人误判的方向：

| 怀疑 | 证据 | 结论 |
|---|---|---|
| 解析层失败（`PLAN_REPLY_EMPTY` / `PLAN_REPLY_JSON_INVALID`） | 用户报错文案是「**LLM 判定**无法回答」，不是「回复为空」或「JSON 无效」 | 排除 |
| 本体连通性失败（`validateConnectivity` 报 DIM_FACILITY） | `fix-dim-facility-connectivity` 已合并两道闸门，失败会回灌 plan 重试；用户文案没有出现「无法通过关联路径连通」字样 | 排除 |
| plan 超限（5 步拒收） | 数据步只有 3 个（`MAX_PLAN_DATA_STEPS=4`），不触发 `_isOversizedPlan` | 排除 |
| token 截断（`PLAN_REPLY_TOO_LARGE`） | 4 步题在 32K 窗口内绰绰有余；step 1/2 通过即证没截断 | 排除 |
| SQL Guard 黑名单 / 写操作拦截 | step 1/2 出数即证只读白名单已放行 | 排除 |
| Qwen key / endpoint 错 | step 1/2 跑成功即证 createClient / endpoint / key 全通；失败发生在 step 3 这一调用内部 | 排除 |
| 拆步规则把"对比"误判多步 | `_EXPLICIT_MULTI_STEP_KEYWORDS` 已刻意**排除**「对比」关键词（`step_query_planner.py:30-37` 注释） | 排除 |

---

## 5. 真因一句话**：**模型层的「无对比能力」** × **prompt 的「无法回答」逃生口**

Qwen3.8-27B-4bit（30B-A3B 4bit）在 step 3 拿到的子问题是：

> 对照同地点其他供应商的供货量变化判断是公司因素还是行业因素

需要：
1. 意识到「同地点」= 把 B019 的供货量矩阵按 `DIM_FACILITY.FCY_0` 分组，再去 DWD 层拉**同分组**的聚合
2. 构造窗口函数或自关联做对比（同一 RCV_SITE_CODE 下 B019 vs 其他供应商）
3. 把对比结果写成 plan JSON 的 selectedProperties + aggregations + groupBy

Qwen3.8-27B-4bit 在这一步拼不出合法 plan JSON，按指示**主动放弃** fallback。

---

## 6. 这是不是 bug？该不该改代码？

**不是 bug。**

- 后端行为**完全按设计**（prompt 允许 target="无法回答"，代码对 isUnanswerable 走软失败分支并如实展示）
- 已有纵深防御：plan 校验 + 连通性校验 + 步骤级失败隔离 + 汇总步前置守卫（C3 修复），全部**按预期触发**
- `fix-dim-facility-connectivity`（2026-10-03）刚刚专门为这道题加了一道闸门

**根因是模型能力上限**，不是代码缺陷。如果要修，应走以下路径中的某一个（**与本报告无关**，不在本次「不修改代码」范围）：

| 方向 | 做法 | 代价 |
|---|---|---|
| ① 换模型 / 升级量化 | 换 bf16 版本，或换 Qwen3-32B / DeepSeek-V3 等 | 本地显存 / 下载时间 |
| ② 提示工程 | step 3 子问题改写：「对比 B019 vs 同地点其他供应商在同一收货地点的月供货量，输出同比/环比」 | 可能反过来让 deepseek 表现也下降 |
| ③ 路由策略 | 「含『对照/对比/判断是…还是…』的多步 → 自动选 deepseek，不选本地」 | 加路由规则 |
| ⑤ 模型配置缓存失效+disable_thinking 配置适配 oMLX | 让本地模型也能用 thinking（M3 路径） | 协议层 / oMLX 支持度未知 |

`baseline`（现状）：模型选错 → 应用层如实展示 → 用户看到「无法回答」。是**正确行为**，不代表要把代码改成「永远不说无法回答」。

---

## 7. 真机验收口径（如果将来要重跑这条题验证修复）

1. `POST /api/v1/chat` 带 `modelId=3`（Qwen）+ 原 prompt → 复现「步骤 3 软失败」卡片
2. 同 prompt 改 `modelId=1`（deepseek）→ 4 步全过、汇总结论出现「行业因素/公司因素」定性
3. 容器日志必须出现 `多步步骤查询生成失败，隔离该步骤`（chat_multistep.py:302 警告）
4. 看不到「无法通过关联路径连通」（那是 `validateConnectivity` 失败的信号，不是这里的信号）
5. 看不到 `PLAN_REPLY_EMPTY` / `PLAN_REPLY_JSON_INVALID`（那是解析层失败，不是这里的信号）

如果某天第 3 步真跑通了，**先核对 plan JSON**——大概率是 `target="其他有效值" + aggregations=[对比窗口]`；若还是 `target="无法回答"`，说明模型层问题没解决、只是换了个表达。

---

## 8. 引用

- `Harness/changes/fix-dim-facility-connectivity/summary.md` — 同一道题在 MiniMax-M3 上的早期失败记录
- `Harness/changes/fix-reasoning-model-token-budget/summary.md` — 同一道题在 MiniMax-M3 上的 token 预算与 thinking 关掉的影响
- `backend/app/services/chat_multistep.py:316-321` — `_MSG_STEP_UNANSWERABLE` 触发点
- `backend/app/services/chat_helpers.py:244-245` — 文案常量
- `backend/app/domain/query_plan.py:35, 168-174` — `UNANSWERABLE_TARGET` / `isUnanswerable`
- `backend/app/services/nl2prompts.py:327, 380` — plan prompt 中的「无法回答」逃生口
- `backend/app/services/step_query_planner.py:69-78` — 拆步系统 prompt
- `backend/seed_models.py:42-59` — Qwen3.8-27B-4bit 配置
- memory: `qa-system-thinking-off-degrades-plan`、`qa-system-multistep-failure-isolation`、`qa-system-planner-step-limit`、`qa-system-dim-supplier-join-island`