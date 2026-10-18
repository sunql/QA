# 变更：fix-chat-assistant-message-id-lifecycle

- **日期**：2026-10-18
- **作者**：Claude / phase3-reconciler
- **Phase**：Phase 3 P1-3 收尾（前端 chatStore 流式消息绑定）
- **状态**：in-review
- **关联变更**：[2026-10-15-phase3-reconciled-main](../2026-10-15-phase3-reconciled-main/summary.md)（P1-3 初版：placeholder + 指针 + closure 守卫，#14）
- **迁移版本**：无
- **MEMORY**：[qa-system-assistant-message-id-lifecycle.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-assistant-message-id-lifecycle.md)

---

## 1. 需求

P1-3 给 chatStore 引入了 `currentAssistantMessageId`（当前在途 assistant 消息的指针）与 `isCurrent()` 闭包守卫，但**指针只有 set、没有完整的 release**。用户可观察到的故障有三类：

1. **跨会话串流**：一轮 SSE 结束后指针仍指向最后一条 assistant 消息；此后切数据源 / 换渠道 / 载入历史 / 清空会话时指针不清，而 `isCurrent()` 把 `null` 当「放行」——上一轮残留的 token/done 帧会 patch 到**新会话的最后一条消息**上。
2. **已完成轮次被误标 aborted**：下一次 `sendMessage` 开头按指针把「上一轮」标 `aborted`。指针从未释放 ⇒ 一条**早已正常完成**的回复会被下一问伪造成「被中止」。旧测试 `重发：旧 assistantMessageId 标 aborted` 直接把这个 bug 断言成了期望行为。
3. **直接调用路径**：`isCurrent` 的 null 放行原本是为了兼容「不经过 `sendMessage` 直接调 `sendMessageStream`」的测试；实测全仓生产代码只有 `sendMessage` 一个调用点（见 §5），放行分支没有真实消费者。

**验收标准**：

- 流结束的每条路径（done / error / 非流式成功 / 非流式失败 / 流异常 / 无终端帧）都释放指针。
- 7 个会话切换/清空入口（`clearMessages` / `resetSession` / `clearForUserSwitch` / `setDatasourceId` / `enterChannel` / `loadSessionMessages` / `deleteSession`）都释放指针。
- 已完成的上一轮**不会**被下一次发送标 `aborted`；真正在途的一轮仍会被中止（行为不变）。
- 指针被清或换轮后，晚到的帧一律丢弃（严格 `isCurrent`）。

## 2. 设计评审

### 根因

- **A：释放点缺失**。`onDone` / `onError` 只清 `loading`，指针留给「handler 自己负责」；非流式分支与两级 catch 只清了一部分；`finally` 的注释明确写着「不重复清」。会话切换的 7 个 `set` 也都没有带指针。
- **B：null 语义过载**。`isCurrent() = id === null || id === assistantMessageId` 把「没有在途轮次」当成「任何消费者都合法」，于是**释放指针反而打开了闸门**。这两个缺陷叠加才构成跨会话串流：只要 A 漏一处，B 就让残留帧畅通无阻。

### 候选方案

| 方案 | 做法 | 取舍 |
|---|---|---|
| **A. 指针严格化 + 条件释放（采纳）** | `isCurrent` 严格比较；新增 `releaseAssistantMessageId()` 仅在指针等于本轮 id 时置 null；所有收尾与会话切换点调用 | 最小改动、零新状态；`null` 语义回归单义（= 无在途轮次）。条件比较防止本轮晚到的 `finally` 清掉下一轮刚写入的 id。 |
| B. generation counter | 每次发送单调递增 `runGeneration`，帧携带或闭包捕获自己的编号，仅编号相等才应用 | 语义最干净，但要给所有 12 个 handler 传参并新增状态位，改动面与回归风险显著更大；当前 `loading` 早返使并发轮次几乎不可能重叠，收益不成比例（YAGNI）。 |
| C. 保留 null 放行，只补释放点 | 只在 7 个会话切换处置 null | 治标：null 放行仍会让「清完指针后」的残留帧在新会话落地；且无法阻止下一轮把已完成轮次标 aborted。 |

**最终决定：A。** 直接调用 `sendMessageStream` 的兼容理由已不成立——生产代码唯一调用点在 `sendMessage` 内部，测试也都是 mock 到 API 层，不依赖 null 放行。

### 条件释放的必要性

`finally` 属于「本轮」，但可能晚于下一轮写入指针的时刻执行（`loading` 早返当前把窗口压到接近零，属防御性设计）。无条件 `set({ currentAssistantMessageId: null })` 会把下一轮的 id 清掉，随后下一轮**全部帧**被严格 `isCurrent` 丢弃——即修复一个串流 bug 换来一个「回复空白」bug。`releaseAssistantMessageId` 用 `get() === assistantMessageId` 把这两轮隔开。

### code-review 发现并复现的 HIGH（已修）

首版只把「指针释放」改成条件式，**catch 分支仍无条件写 `currentAssistantMessageId: null`**，且未过 `isCurrent()`。可达路径：A 轮流式进行中 → `resetSession()`（清指针、置 `loading=false`，但 fetch 未 abort）→ 用户发 B 轮 → A 的 fetch 迟到 reject → A 的外层 catch 抢走 B 的指针，B 的全部帧被丢弃，且 A 的错误文案被写进 B 的气泡。同一形态还存在于非流式成功/失败分支，且 `finally` 的 `loading`/`isStreaming` 复位会关掉 B 的流式状态。

**修复**（并新增交叠轮次回归用例，修复前 RED）：

- 非流式成功与所有 catch 分支先过 `isCurrent()`；被取代的轮次不写消息/loading/`error`/指针。
- 这些分支**不再直接写指针**，释放统一由 `finally` 的条件释放完成。
- `finally` 的 `loading`/`isStreaming` 复位额外要求「指针仍属于本轮」。

### typescript-reviewer 发现并复现的 HIGH（已修）

收紧守卫后，孤儿轮次的 `onDone`/`onError`/catch/finally 全部因指针不匹配而不再写状态——**包括 `loading`**。而 `setDatasourceId` / `enterChannel`（以及 `clearMessages` / `loadSessionMessages` 失败分支）只清了指针、没清 `loading`，于是「流进行中切数据源/换渠道 → 旧流收尾 → `loading` 永远为 true → 输入框静默失效」。reviewer 用临时探针实测 `loading after orphan settles = true`。

**修复**：确立不变量「**指针被清 = 没有轮次拥有 `loading`**」——所有清指针的入口一并 `loading: false`（`enterChannel` / `setDatasourceId` / `clearMessages` / `loadSessionMessages` 失败分支；`resetSession` / `clearForUserSwitch` / `deleteSession` 原本已置 false）。回归用例：流进行中切数据源 → 孤儿流收尾 → `loading === false`；并给 `expectCleared` 加统一断言。

### 复审发现的 3 个直接溅射面问题（已修）

第二轮复审（typescript-reviewer 全量 + code-reviewer 定向）在修复本身的溅射面内又发现三处，均修复并各自有 RED→GREEN 证据：

1. **doc-QA 跨渠道串台（code-reviewer MEDIUM / typescript-reviewer LOW）**：`loading: false` 落到 `enterChannel` / `setDatasourceId` 后，「doc-QA 流在途 → 切到 chat → 发新问题」成为可达路径。`sendDocQa` 的 SSE 回调与 catch 完全没有所有权判据，旧流的 `token` 会 `patchLastMessage` 到新 chat 气泡上（reviewer 复现串入 `DOCQA-LEAK`），`done`/`error` 还会把新一轮的 `loading` 关掉。修复：`sendDocQa` 捕获自己的占位消息 id，以「渠道仍是 `doc_qa` 且占位仍是最后一条」为所有权，回调开头与 catch 分支早返。
2. **`loadSessionMessages` 失败留下永久转圈的占位（typescript-reviewer MEDIUM）**：该失败分支保留 `messages`（与 `clearMessages` 不同），但清掉指针后孤儿流的所有帧都被丢弃，`isStreaming: true` 再也不会被改写。修复：失败分支就地给仍在流式中的消息置 `isStreaming: false` + `aborted: true`。
3. **两个 handler 级释放用例是假绿（typescript-reviewer MEDIUM）**：`expect(...)` 写在 `mockImplementation` 里时，`AssertionError` 与 handler 同栈抛出，被 `sendMessage` 的 catch 吞掉（此时指针已释放，连错误分支都不写），删掉 `onDone`/`onError` 的释放行用例仍通过。修复：改成在 mock 内把指针快照到局部变量、出 mock 后断言；handler 未执行时快照为哨兵值 `"unset"` 也会失败。

### 已知遗留（不在本 PR 范围）

`onDone` 之后的 `attachHypotheses()` 仍会在 await 后 `patchLastMessage`，不校验轮次/会话：若假设拉取期间用户切会话或发下一问，旧的「可能原因」会落到新气泡上。同属迟滞写入类，但修复需要按 `assistantMessageId` 定向 patch（而非最后一条），留待单独跟进。

## 3. 数据模型变更

无。纯前端 Zustand store 内存状态，不涉及 DB / 迁移 / 索引。

## 4. 接口契约变更

无 API / DTO 变更。仅收窄 store 内部字段 `currentAssistantMessageId: string | null` 的语义：

- 改前：`null` = 「放行」（没有任何消费方区分的两种状态混用）。
- 改后：`null` = 「当前没有在途 assistant 轮次」；非 null 时只有该 id 的帧可写。

全仓 grep 确认该字段**只被 `chatStore.ts` 自身读写**，无组件/其他 store 消费，故不构成对外契约。

## 5. 实现要点

- `frontend/src/stores/chatStore.ts`
  - `isCurrent()`：`get().currentAssistantMessageId === assistantMessageId`（严格）。
  - 新增 `releaseAssistantMessageId()`：`get() === assistantMessageId` 时才置 null。
  - 释放点：`onDone`、`onError`，以及 `finally` 统一兜底（条件释放）。
  - **所有 catch 与非流式成功分支都先过 `isCurrent()`**：迟到轮次的响应/失败不得改写新一轮的消息、loading 或指针；这些分支**不直接写指针**，释放统一交给 `finally` 的条件释放（否则无条件 `null` 会抹掉下一轮的 id——code-review 复现的 HIGH）。
  - `finally` 的 loading/isStreaming 复位同样要求「指针仍属于本轮」，迟到的收尾不得关掉下一轮的流式状态。
  - **不变量「指针被清 = 没有轮次拥有 `loading`」**：所有清指针入口一并 `loading: false`——否则孤儿流收尾时 finally 因指针不匹配跳过复位，输入框永久禁用。
  - 会话切换：`clearMessages`、`resetSession`、`clearForUserSwitch`、`setDatasourceId`（换会话分支）、`enterChannel`（换渠道/空会话分支）、`loadSessionMessages`（成功与失败）、`deleteSession`（当前会话分支）。
  - `sendDocQa`：捕获 `placeholderId`，`stillOwned()` = 「渠道仍是 `doc_qa` 且占位仍是最后一条」；SSE 回调开头与 `catch` 分支 `if (!stillOwned()) return;`（doc-QA 子系统的轮次所有权，`loading` 的清理同样受它保护）。
  - `loadSessionMessages` 失败分支：保留 messages，同时把仍在流式中的消息就地收尾（`isStreaming: false` + `aborted: true`）。
- 关键不变量：**指针的生命周期 = 一轮流的生命周期**，跨轮次与跨会话都不复用；**任何 SSE 回调都必须有轮次所有权判据**（chat 用指针、doc-QA 用占位 id）。
- `sendMessageStream` 的生产调用点唯一（`chatStore.ts:627`），故严格守卫没有旁路消费者。

## 6. 测试

`frontend/src/tests/chatStore.assistantMessageId.test.ts`（重写，19 例）+ `frontend/src/tests/chatStore.docQaOwnership.test.ts`（新增，2 例）：

- 收尾释放：`onDone`、`onError`（断言在 handler 执行时指针已清，不等 finally）、流异常（无终端帧，`finally` 兜底）。
- 晚到帧：本轮 `onDone` 后再 `onToken` 不得改写消息（旧实现 null 放行会追加）。
- 交叠轮次（code-review HIGH 的回归）：旧轮迟到失败不得清掉新一轮指针/loading，也不得把旧错误写进新气泡；新一轮帧仍生效。
- 孤儿流（typescript-reviewer HIGH 的回归）：流进行中切数据源 → `loading` 立即复位，孤儿流收尾后仍为 false。
- 回归：`已完成的上一轮不会被下一次发送误标 aborted`（替换原先断言 bug 的用例）。
- 在途中止：新一轮 `sendMessage` 标旧轮 `aborted` + 不新建占位（`loading` 早返），指针保留至旧流收尾再释放。
- 会话切换：`expectCleared` 参数化覆盖 `clearMessages` / `resetSession` / `clearForUserSwitch` / `setDatasourceId` / `enterChannel` / `loadSessionMessages`（成功 + 失败）/ `deleteSession`，统一断言指针与 `loading` 都已复位。
- 去除 `as any` 种子（reviewer MEDIUM）：暴露并删除一个不存在的 `currentRunId` 字段，种子加正向断言防止静默空转。
- handler 级释放（onDone/onError）改为「mock 内快照 → mock 外断言」，避免 `AssertionError` 被 `sendMessage` 的 catch 吞掉造成假绿。
- doc-QA 所有权（`chatStore.docQaOwnership.test.ts`）：doc-QA 流在途 → 切渠道 → 发新一轮 → 旧流 `token`/`done`/promise 收尾均不得写进新气泡或抢 `loading`；旧流失败不得写 `error`。
- `loadSessionMessages` 失败后孤儿占位不再永远转圈（`isStreaming=false` + `aborted=true`）。

验证结果：

- `npx vitest run src/tests/chatStore.assistantMessageId.test.ts src/tests/chatStore.docQaOwnership.test.ts` → **21 passed**（19 + 2；初版实现前 12 failed / 2 passed，RED→GREEN；后续三批新增用例分别在修复对应问题前实测 RED）。
- 关联套件（chatApi / chatStore / assistantMessageId / datasourceScope / chatStoreDocQa / docQaOwnership / hydration / guards / kpiWarning / ChatPage / ChatPanel / ChatHistoryPanel，12 文件）→ **192 passed**。
- 新增的 catch/孤儿流用例在**修复前的实现**上实测 RED（`2 failed | skipped`），恢复修复版后 GREEN；doc-QA 与 load-failure 三条用例在移除对应守卫后实测 **3 failed / 18 passed**，恢复后 GREEN。
- `npx tsc --noEmit` → 无输出（通过）。

## 7. 安全审查

不触发 security-reviewer：纯前端内存状态机，不涉及认证、密钥、SQL、用户输入解析、文件系统或外部 API 调用；无新增网络请求与数据出口。

两轮审查结果（发现全部修复后复审）：

- code-reviewer：**0 CRITICAL / 1 HIGH（catch 抢指针，已修 + 回归用例）/ 2 MEDIUM（迟到帧与交叠轮次用例已补）/ 1 LOW（wiki 措辞已对齐）**。
- typescript-reviewer：**0 CRITICAL / 1 HIGH（孤儿流 `loading` 卡死，已修 + 回归用例）/ 1 MEDIUM（`as any` 种子空转，已改为类型化 + 正向断言）/ 3 LOW（handler 级释放已加判别断言；mock 类型未收紧、`attachHypotheses` 迟滞写入记为已知遗留）**。
- 第二轮复审（typescript-reviewer 独立探针 + code-reviewer 定向重审）：HIGH 与 `as any` MEDIUM 均确认 RESOLVED；新发现 2 MEDIUM（handler 级断言假绿、`loadSessionMessages` 失败留下永久转圈占位）+ 1 LOW/MEDIUM（doc-QA 跨渠道串台），全部修复并补齐 RED→GREEN 证据后，两轮 reviewer 的 open findings 清零。

## 8. 部署验证

- 无迁移、无后端改动；**只发前端**。发布时必须重新 `docker compose build` 前端镜像（见 memory `qa-system-frontend-deploy-build-required`，仅重启不会带新代码）。
- 发布后冒烟（人工）：
  1. 连发两轮问题 → 第一条回复**不出现** aborted 徽章。
  2. 流进行中切换数据源/渠道 → 新会话不出现旧会话的 token 或 done 内容。
  3. 流进行中重发 → 旧轮标 aborted，新轮正常返回（回归）。
  4. 文档问答流进行中切到 chat 并发问 → 新回答里不出现 doc-QA 的片段，输入框在 chat 流结束后恢复可用。
  5. 流进行中在历史面板点一条载入失败的会话 → 旧气泡不再显示「正在输入」动画。
- CI：仓内无前端 CI（memory `qa-system-no-ci-main-can-go-red`），合并前以本地 vitest + tsc 为准。

## 9. 关联

- 设计稿：无独立 spec（P1-3 收尾项，来自 chat 服务优先计划审计）。
- Wiki：[Harness/wiki/frontend.md](../../wiki/frontend.md)（新增「Chat 流式消息绑定的生命周期」小节）。
- Rules：[Harness/rules/变更记录强制规范.md](../../rules/变更记录强制规范.md)、[Harness/rules/测试规范.md](../../rules/测试规范.md)。
- Memory：[qa-system-assistant-message-id-lifecycle.md](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-assistant-message-id-lifecycle.md)。
- 关联变更：[2026-10-15-phase3-reconciled-main](../2026-10-15-phase3-reconciled-main/summary.md)（P1-3 初版引入指针与守卫，#14）、[aichat-p2-followups-2026-10-16](../aichat-p2-followups-2026-10-16/remaining-work-ssot.md)（P1-3 收尾项来源）。
