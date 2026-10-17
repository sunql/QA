# 变更：预算闸门静默失效 → 用户可见的降级信号（T15 后续）

- **日期**：2026-10-13
- **作者**：Claude / 启琳
- **状态**：**已实现并验证（本地，含评审修复轮）**，未部署（待与后台多步研究线一并上线）
- **Phase**：研究线 · 会话级预算闸门（Task 15）可观测性补口
- **关联变更**：
  - 前置：会话级预算只读闸门（Task 15，`e3b7ec4` 一族；本变更**只加信号，不改闸门语义**）
  - 登记处：`docs/superpowers/plans/2026-10-13-research-platform-phase2-acceptance.md` §3.2
    （原文把本缺口记为「**非本任务修复项**」，本变更是**该验收计划范围之外的扩展**，
    不并入 phase-2 验收签核）
- **SSOT 出处**：`backend/app/services/research_agent_ports.py`（error code 表）、
  `frontend/src/types/research.ts`（uiHint 分类）
- **迁移版本**：无（不动 schema）

---

## 1. 问题

会话级预算闸门在每个相位边界读会话累计成本，`cost >= budget` 即终止本轮。但读成本是
best-effort：`_readSessionCost` 读 `session_token_usage` 失败时**只留 WARNING 并返回
`None`** ⇒ `_sessionBudgetExceeded` 返回 `False` ⇒ **闸门静默 fail-open**。

若某会话的成本读取**持续**失败，该会话的预算上限**形同虚设**，而**用户侧零信号** ——
用户看到的是一次「照常跑完」的研究，无法得知守门已经失效、成本可能已超预算。

## 2. 根因

三个各自成立、合起来才致命的判断：

1. **fail-open 是对的**（计量打嗝不得杀研究）—— 这条**不改**；
2. 但「静默」是**独立**于 fail-open 的缺陷：放行可以，不告知不行；
3. 而 degraded 类 `research.error` **当时没有任何 UI 面** —— `researchStore` 只在
   终态写 `error`；`ResearchProgress` 的白名单不含 `research.error`；主区 Alert 只吃
   终态。⇒ 只加后端事件，用户**仍然**看不见。

## 3. 设计（用户已裁定：两个面都做）

| # | 决策 |
|---|---|
| 1 | **不改 fail-open 语义**，只加用户可见信号（后端事件 + 前端两面） |
| 2 | 阈值 **N=2 次连续读失败**：闸门每相位一次（新 turn ≤7 次），而 turn 常在 checkpoint 提前暂停（plan 暂停 ⇒ 仅 3 次检查）—— N=3 在短 turn 里永不触发；N=1 被一次瞬时抖动触发、下个相位又自愈 ⇒ 噪声。N=2 = 容一次重试，且任何有实际工作的 turn 都能在**第二个相位边界**前发出 |
| 3 | **每轮一次**（emit-once），不做「恢复后再失败」重发 |
| 4 | **只发信号，绝不改模型路由** —— 用户选定的模型一律照用（SSOT：memory `qa-system-no-model-override`） |
| 5 | `terminal=False` ⇒ 不关流、不落 `failed`，自动不进 `TERMINAL_ERROR_CODES` |
| 6 | 前端**按 `uiHint` 的类**分支，不按 `code`（仓库规则 ③）⇒ 未来新增 code 前端零控制流改动 |

计数器是**实例属性**（`self._budgetCostReadFailures` / `self._budgetDegradeEmitted`），
**不放 `state`**：服务每轮新建（`research.py:213`），`state` 每轮从 DB 重建（`rebuildState`）
⇒ 实例属性天然是「每 turn 一个」的单元。

## 4. 落地内容

| 层 | 产出 |
|---|---|
| SSOT | `research_agent_ports.py`：`ERROR_BUDGET_METERING_DEGRADED`(= `budget_metering_degraded`) + `BUDGET_METERING_DEGRADED_MESSAGE` + `ERROR_SPECS` 第 7 条（`terminal=False, sessionStatus=running, payloadFields=(), uiHint="degraded"`） |
| 服务 | `research_agent_service.py`：常量 `SESSION_BUDGET_COST_READ_FAILURE_THRESHOLD=2`；`_sessionBudgetExceeded` 改为「成功断 streak / 失败计数」；新 helper `_noteBudgetCostReadFailure`（照抄 `markLlmUnavailable` 的形状：guard → warning → `emitEvent`） |
| 顺带修复 | `_readSessionCost` 的 `Decimal` 转换**挪进 `try`**（原先在 `try` 外，端口返回 `None`/非数值会抛 `decimal.InvalidOperation` **炸掉整轮**，而非按 best-effort 放行） |
| 前端 | `ResearchProgress.tsx` 新增 `progressLabelKey` 分类器（`research.error` **仅当** `uiHint === "degraded"` ⇒ `research.progress.degraded`）；`researchStore.ts` 新增 `degradedNotice: string \| null`；`ResearchSessionPage.tsx` 主区常驻 warning `Alert`；i18n 双语各 1 条 |
| 文档 | phase-2 验收计划 §3.2 从「当前**未**实现」改为已实现记录，并注明**范围之外** |

### 三个最锋利的坑（写实现时踩过/避开）

1. **发射点必须在 `_sessionBudgetExceeded`，绝不能在 `_readSessionCost`**：
   `_sessionBudgetMessage` 会**二次**调 `_readSessionCost`，计数器放那里会导致上报读被
   重复计数、甚至在**合法的超预算终止**里发出幽灵降级事件。有专门用例钉住
   （`test_message_read_failure_does_not_count_or_emit`）。
2. **`budget <= 0` 必须先于读操作短路**（结构上保证「故意关闭的闸门永不发此事件」）——
   否则计量器坏掉时可借降级事件逃逸。有双向用例（`usage.calls == 0` + `frames == []`）。
3. **`errorPayload` 硬校验字段集**：本 code `payloadFields=()` ⇒ 发射**不得**传任何
   `**fields`（传了 `ValueError` 炸在 turn 里）。

## 5. 验证

- **单测**（判定纯逻辑，零 DB）：`test_research_session_budget.py` 新增第 5 节 8 例 ——
  阈值下静默 / 达阈值恰好一条 / 整轮仍只一条 / **成功断 streak（判别性）** /
  恢复后再失败不重发 / 闸门关闭不读不发 / 二次读不计数。
  `test_research_event_bus.py` 契约同步（parametrize 加行 + 精确集合 7 个 + `errorPayload` 字段）。
  → **48 passed**
- **集成**（真实 PG + 完整 HTTP 链路 + 真 SSE 帧序）：`test_session_budget_gate.py` 第 4 节 ——
  持续读失败 ⇒ **恰好一条**降级 `research.error`（`uiHint=degraded`、
  `code not in TERMINAL_ERROR_CODES`、落在 `research.intent` 之后 `research.plan` 之前）、
  **末帧仍是 `research.done`**、会话 `done`；单次失败 ⇒ 反向断言无 error。→ **5 passed**
- **前端**：`ResearchProgress.test.ts`（新建 9 例）+ `researchStore.test.ts`（+10 例）+
  `ResearchSessionPage.test.tsx`（+1 页面级：主区 warning Alert + 时间线各一条且不关流）。
  → **116 passed**
- **反向自检（三处，均验过 RED）**：
  - 后端删 `self._budgetCostReadFailures = 0` ⇒ `test_success_resets_the_consecutive_streak` 转红；
  - 前端把页面 Alert 摘掉（`false && degradedNotice`）⇒ 页面级用例报
    「Unable to find an element with the text: 会话成本计量连续读取失败…」转红；
  - 前端删 `openSession` 里的 `degradedNotice: null` ⇒ 恰好
    `openSession 清上一会话的 degradedNotice` 一条转红（其余 80 条全绿 —— 证明没有别的
    用例覆盖这条跨会话路径，评审这条 HIGH 是真缺口，不是重复守卫）。
- **全量前端套件（如实登记）**：`11 failed | 1683 passed`。5 个失败文件**全部与本次改动无关**，
  已用干净 HEAD（`d1af4d4`）worktree 基线证认：其中 10 例在基线**同样失败**
  （`client.test.ts` 3、`RequireAuth.test.ts` 5、`chatStore.test.ts` 1、`EntityMappingPage` 1），
  第 11 例（`OntologyPage` 加治理字段）是**负载相关 flake** —— 孤立跑在基线与本分支**均通过**。
  ⇒ 本次改动**未引入**任何前端失败。
- **前端覆盖率闸门（如实登记：当前不可测）**：仓库 `vitest.config.ts` 有
  `lines/functions/branches/statements ≥ 80` 阈值，但 vitest v8 provider 在**套件存在失败**时
  不产出覆盖率报告（`coverage/` 目录与 text 表都不生成）⇒ 在那 10 个既有失败修掉之前该闸门
  无法评估。本次新增代码路径由上述新增用例**双向**覆盖。
- **`tsc -b`（如实登记）**：13 个错误，**全部**在本次未触碰的文件里
  （`src/api/__tests__/*`、`src/components/common/__tests__/*`；其中
  `RequireAuth.test.ts` 报 `Module './RequireAuth' has no exported member 'isAllowedWhenMustChange'`
  正是它那 5 个用例失败的原因）⇒ **既有** build 断裂，非本次引入。

## 6. 边界（如实登记，不在本次范围）

- `budget <= 0`（闸门被显式关闭）时**永不**发此事件 —— 这是设计，不是缺陷；
- reaper 边界（`leased` 之外、`lease_until` 不看）沿用 Task 15 登记，未动；
- 「恢复后再失败」不重发 —— 若将来需要会话级持续态，应另立机制而非松这个 flag。

## 7. 评审与修复轮（code-reviewer / opus，只读）

结论：**Approve with follow-ups**，0 CRITICAL / 1 HIGH / 2 MEDIUM / 3 LOW。逐条处置：

| 级别 | 发现 | 处置 |
|---|---|---|
| HIGH | `degradedNotice` **跨会话泄漏**：`openSession` 清了 `error` 却没清它，而切到**已结束**会话不建流（`connectStream` 那条清除点够不着）⇒ 上一会话的降级横幅挂在新会话页面上 | **已修**：`openSession` 与 `sendQuestion` 的 `set` 各补 `degradedNotice: null`；补 2 条用例（反向自检确认承重） |
| MEDIUM | degraded 面从「预算计量」**扩散到整类**（`step_failed` / `llm_unavailable` / `sql_validation_failed` / `hypothesis_generation_failed` 也会弹主区横幅），且无断言钉住 | **保留类级行为**（计划原文即要求改「degraded 兜底分支」+ 规则 ③ 要求按类分支），补断言 `step_failed` 也置 `degradedNotice`，并在分支注释里写明「此前完全静默，本次统一补面」。**已在交付说明中明确告知用户可否决** |
| MEDIUM | `test_single_cost_read_failure_is_silent` 的「单次失败」可能**不是闸门消费的**（路由上下文读同一个端口）⇒ 承重不足 | **评审前提被证伪**：`_runFrom:636` 的相位边界闸门**先于**该相位的模型解析，首个 `getSessionCost` 就是闸门的。但原用例**证明不了**这点 ⇒ 已让假替身记录调用序（含对照组 `"turns" in calls` 证明路由确实跑过），并断言 `failures_on == [0]` 且 `calls[:2] == ["cost","cost"]` |
| LOW | 兜底值是 i18n key 但页面直渲 ⇒ 会显示字面 key | **已修**：改用 `i18n.t(...)` 取**已翻译**文案（有先例：`chatStore` 亦 import `i18n`） |
| LOW | emit-once 实为「每次后台运行一次」，resume 新建实例 ⇒ 同一 turn 内可能重发 | **登记为设计**（前端重设同一文案，无害），已改写 `__init__` 与 helper 的注释，不再说「每 turn 一次」 |
| LOW | `_FlakyTokenUsage` 缺 `getSessionTurnCount`/`getLastModelId` ⇒ 路由上下文走 `AttributeError` 被吞的偶然路径 | **已修**：三个读法补齐（保真度：memory `qa-system-fake-double-fidelity`） |

评审同时确认守住的不变式：`budget <= 0` 短路先于读、发射点不在 `_readSessionCost`、
成功断 streak、`Decimal` 转换收紧、`errorPayload` 字段集、`ERROR_SPECS` 恰好 7 个、
前端按 `uiHint` 类分支、页面 Alert 真挂载、计数器用实例属性、无安全面。
评审全程只读（已用 `shasum` 前后比对确认工作区未被改动）。
