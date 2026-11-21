# 变更：Copilot 多轮会话（feat/copilot-multiturn）

- 日期：2026-11-21　分支：`feat/copilot-multiturn`（off `2536cea2`）
- 背景：I3 首版是**单轮**（每条消息 = 一次独立运行，无记忆）；用户实测后要求解决

## 一、三条规则（不做就会出真问题）

1. **追问 = 续跑，不是新开**：上一轮 run 停在 `PAUSED/NEEDS_INPUT` ⇒ 下一句走既有
   `resumeRun`（同一 run 继续 PLAN）。理由：重开一次 run 会把已经做过的检索/LLM
   调用再花一遍钱，而且 planner 会丢失已确认的上下文。
2. **上下文有界**：新问题带最近 **3 轮**、总字符 ≤ **1500**、单条摘要 ≤ **300**；
   超长**从最旧开始丢**（保新弃旧）。没有上限会撑爆 planner 的 observation 预算。
3. **会话绑定 Agent + 模型**：中途换场景/模型 ⇒ 422「请开新会话」；否则上一场景的
   结论会被喂给另一个 Agent。

## 二、数据模型（迁移 0162）

| 表 | 作用 | 关键列 |
|---|---|---|
| `procurement_copilot_conversation` | 会话壳 | `conversation_id`(uuid, unique)、`created_by`、`agent_code`、`model_config_id` |
| `procurement_copilot_turn` | 轮次 ↔ 运行映射 | `(conversation_id, seq)` unique、`question`、`run_id` |

**为什么不往 `agent_orchestration_run` 加列**：那是编排域共享表（P3/P5 都在用），
会话语义会反向污染所有 run；而且会话有自己的归属与生命周期。
**为什么轮次表存原始 question**：resume 会把补充输入追加进 `run.question`
（`[补充]…`），界面要展示用户原文就必须单独存。
两张表已在 `procurement_scope_ruling.py` 登记为 APPROVED（准则 H 守卫）。

## 三、API

- `POST /procurement/copilot/messages`：请求新增 `conversationId?`（缺省 = 新会话）；
  task 响应与 SSE `copilot_run` 事件回传 `conversationId` + `resumed`。
- **新增** `GET /procurement/copilot/conversations/{id}`：会话 + 每轮
  `{seq, question, runId, status, answer, pauseCode}`；**非创建者一律 404**。

上下文注入形态（只在**有历史**时出现，首轮不改写 run.question）：

```
[对话上下文]
1. 用户：<上一问>
   助手：<上一答 或（未完成）>
[当前问题]
<本次问题>
```

## 四、前端

- 记住 `conversationId`（来自 `copilot_run`）并在后续消息里带上；
- 「新会话」按钮清空线程与会话 id；
- 续问（`resumed=true`）在卡片标题上打「续问」标签；
- PAUSED 的追问仍以黄色 Alert 高亮，用户下一句就是回答。

## 五、验证

- 后端：**79 passed**（copilot API 9 例含 5 个多轮用例 / 上下文纯函数 6 例 /
  范围裁决 44 例 / 路由与生产鉴权守卫）
  - 多轮用例覆盖：首轮建会话、第二轮带上下文、**PAUSED 续跑同一 run**、
    他人读会话 404、会话内换 Agent 422
- 真机（dev 栈，本地 LLM）：
  - 第二轮 run 的 `question` 以 `[对话上下文]` 开头且包含上一问；
  - 追问后发「就选 AJE0000781 这个规格」⇒ `resumed=true`、**同一 runId**，
    工具调用 2 次（未重跑物料检索），tokens 累计 10314，终态 SUCCEEDED。
- 前端：页面 4 例（新增「第二轮带 conversationId / 新会话清空」）+ 全量套件回归。

## 六、已知边界（不扩大范围）

- 会话内**上一轮还在 RUNNING** 时再提问 ⇒ 按「新问题」处理（新 run），不会打断；
  此时上下文里该轮显示「（未完成）」。
- 被用户跳过（没回答）的 PAUSED run 会留在库里（孤儿 run）——可由
  `POST /agent-runs/{id}/cancel` 收尾，本期不做自动清理。
- 页面刷新后不会自动恢复线程（`GET conversations/{id}` 已就绪，前端接入留待下批）。
