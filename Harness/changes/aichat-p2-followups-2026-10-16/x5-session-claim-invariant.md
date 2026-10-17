# X5 — 会话归属 + 状态互斥守卫下沉 ✅ PR #41

## 范围

承接 1012 cross-refs spec §P0-X5。
研究会话写路径的「归属 + 互斥」两件事原本散落在
`api/v1/research.py` 8 个端点 + 1 个私有 `_ownedSession` helper 里，
`active_run_id` 互斥判定内联在 createTurn，**answerCheckpoint 漏检**。

本批把两者归到 `app/services/session_claim.py`：

- `assert_research_session_ownership`（取代 `_ownedSession`，共享给 8 个端点）
- `assert_research_claim_invariant(expected="free" | "held")`（双断言下沉）

## 落地

**Worktree**：`/private/tmp/qa-fix-x5-session-claim`
**Branch**：`fix/qa-1012-p0-x5`（base=main tip `ec0570b0`）
**Commit**：
- `83ad58ec` fix(research): claim invariant 守卫下沉到 session_claim 共享模块
- `2b8359ed` fix(research): review 反馈修 — UX 文案分流 / M1 不返行 / M2 复用常量 / M3 端到端 race / M4 expire

**PR**：**#41** https://github.com/sunql/QA/pull/41

## 关键修复（review Critical）

**`answerCheckpoint` 加 `expected="held"` 守卫**。旧实现仅看
`checkpoint.status="pending"`，忽略会话自身的 claim 状态。Bug 链：

1. abort 后 `abortSessionIfActive` 把会话落 `aborted` 终态 + `active_run_id` 清空
2. 但 pending checkpoint 仍 `status="pending"`
3. 再答 checkpoint，`resumeTurn` 把 `aborted` 拉回 `running`，**且 resumeTurn 不 claim**
4. ⇒ `status=running + active_run_id=NULL` 的会话
5. 下次 `POST /turns` 放行（闸门只看 `active_run_id IS NULL`），起第二条状态机

详见记忆 [[qa-system-research-claim-invariant-create-only]]。

X5 修法：`answerCheckpoint` 在 `_ownedSession` 之后、`pending` 判定之前
加 `expected="held"` 断言。无 claim ⇒ 409 + 文案「未持有执行权」，
把这条路封死。

**`createTurn` 也走新守卫 `expected="free"`**，把内联判定下沉到服务层，
与 answerCheckpoint 对称；文案单点由 `assert_research_claim_invariant` 给出，
旧本地 `_busyMessage` 删除。

## 变更

| 文件 | 改动 |
|---|---|
| `backend/app/services/session_claim.py` | 新增（135 行）：两守卫 + 完整 docstring + claim invariant 设计说明 + 2 个 409 文案常量 |
| `backend/app/api/v1/research.py` | 8 处 `_ownedSession` → `assert_research_session_ownership`；createTurn 加 `expected="free"`；answerCheckpoint 加 `expected="held"`；2 处 inline "研究会话不存在" → 复用常量；删 `_ownedSession` 私有副本 + `_busyMessage` 本地分支文案 + `STATUS_AWAITING` 死导入 |
| `backend/app/tests/integration/test_research_session_claim.py` | 新增（10 个测试）：归属 4 态 + claim invariant 6 态（含 awaiting_user 文案分支） |
| `backend/app/tests/integration/test_research_api.py` | +2 端到端测试（held 守卫 + abort→answer race 端到端）；+`_claimSession` helper（review M4 改用 `dbSession.expire`） |

## 验证

- 31 个 research API 测试全绿（含 2 个新加的端到端：held 守卫 + abort→answer race）
- 10 个 session_claim 单元测试全绿
- 67 个 research_session_service + research_agent_service 测试全绿
- **共 110 个研究域测试全绿**

## Review 闭环（commit 2b8359ed）

PR #41 派 reviewer subagent 跑 2 HIGH + 4 MEDIUM + 3 LOW。

### H2（UX 回归）✅ 已修
- **Bug**：旧 `_busyMessage` 按 `row.status` 分流文案 —— `awaiting_user` 告诉
  用户「你是瓶颈」，`running` 告诉用户「后台在跑」；**不**回显 `active_run_id`
  UUID，**不**广告 cancel 入口（review Fix 1a）。新守卫一统文案并回显 UUID
  是 review Fix 1a 的回归。
- **修法**：`session_claim.py` 加 `_MSG_RESEARCH_BUSY_AWAITING` /
  `_MSG_RESEARCH_BUSY_RUNNING` 常量；按 `row.status` 分支选文案；UUID
  不再回显；保留「不广告 cancel 入口」。
- **新增测试**：`test_claim_invariant_free_uses_awaiting_user_copy_when_session_awaiting`
  钉死 awaiting_user 分支 + 反向断言。

### H1（死导入）✅ 已修
- 删 `research.py` 的 `STATUS_AWAITING` import（`_busyMessage` 已删，
  唯一使用方没了）。`STATUS_RUNNING` 仍 import（abortSession 还在用）。

### M1（不返行）✅ 已修
- `assert_research_claim_invariant` 改返 `None` —— 调用方已在
  `assert_research_session_ownership` 拿到 row，二次 fetch 是浪费 +
  identity map 二次写。docstring 同步修。
- **测试侧**：用 `dbSession.refresh(row)` 重读断言 `active_run_id`，
  代替原先「`out.active_run_id == ...`」的返回行断言。

### M2（常量复用）✅ 已修
- `research.py` 的 2 处 inline `"研究会话不存在"` 改成从 `session_claim`
  导入的 `_MSG_RESEARCH_SESSION_NOT_FOUND` 常量。`session_claim.py`
  的 docstring 已声明逐字节契约，避免文案漂移。

### M3（端到端 race）✅ 已修
- 原 `test_answer_checkpoint_409_when_session_has_no_claim` 是「不调
  `_claimSession`」绕过法，没真走 abort。
- **新增** `test_abort_then_answer_pending_checkpoint_409`：真走
  `POST /abort` 把 claim 释放，再答 checkpoint ⇒ 409；附带守护性断言
  「abort 后 `active_run_id` 真为 NULL、`status` 真为 `aborted`」 ——
  守护 `abortSessionIfActive` 的释放语义。一旦该 SQL 漏写
  `active_run_id=None`，守护 SELECT 立刻 RED。

### M4（`_claimSession` 不刷 ORM）✅ 已修
- flush 后用 `dbSession.expire(rowInMap, ["active_run_id"])` 显式让
  `row.active_run_id` 重走 SQL，不再依赖调用方手工 refresh。

### LOW（L1-L3）— 无需修
- L1 (`isHeld` camelCase)：与项目 `camelCase` 规则一致，是有意偏离 PEP 8。
- L2 (`_busyMessage` 注释无 test 链接)：H2 修完后该文案常量被钉死，
  注释里描述已经足够。
- L3 (ruff 报 H1)：H1 修了之后一并消失。

## 反向断言 / 防退化

- `test_claim_invariant_held_409_when_no_active_run` —— 守卫降级 / 守卫前移 / 闸门错位立刻 RED
- `test_ownership_404_when_admin_impostor` —— admin 放行越权立刻 RED（X5 起 admin 也不放行 research 写路径）
- `test_answer_checkpoint_409_when_session_has_no_claim` —— 端到端失守立刻 RED
- `test_claim_invariant_free_uses_awaiting_user_copy_when_session_awaiting` —— H2 文案分支回退立刻 RED；反向断言「不写 running 分支的文案」防分支写错
- `test_claim_invariant_free_409_when_active_run` 同时钉死「不回显 `active_run_id` UUID」+ 「awaiting_user 分支不写 running 文案」
- `test_abort_then_answer_pending_checkpoint_409` 内嵌守护 SELECT —— 守护 `abortSessionIfActive` 释放语义（漏写 `active_run_id=None` 立刻 RED）+ 守护「不被续跑污染」（`status` 仍为 `aborted`）

## 副作用 / 兼容性

- `_ownedSession` 私有副本删除（功能 100% 平移到 `assert_research_session_ownership`，404 文案逐字节一致）
- `_busyMessage` 本地分支文案删除（统一由 `assert_research_claim_invariant` 单点给出，按 status 分流）
- **admin 行为**：旧 `_ownedSession` 不放行 admin；新守卫同样不放行（与 chat 域 `session_guard.assertSessionOwnership` 不同 —— research 写路径要求硬归属，避免 admin 误写他人会话；admin 自己的 `dbUserId` 行走正常分支）
- **测试侧**：5 个 `appendTurn + openCheckpoint` 测试加 `_claimSession` helper 模拟真实 flow 的 `active_run_id` 状态；FK 约束要求 helper 用 `runState.id` 而非 `turn.id`（独立 worktree 第一次跑时遇到 FK 错误 → 已修正）

## 阻塞 / 后续

- M5 stub 模式回填（序 4，4h）— 下一项
- L1 prompt 注入（序 6，1d+）
- N4 qwen 知识保留（序 7，独立）

## 链接

- Spec：`docs/superpowers/specs/2026-10-13-1012-code-review-cross-refs.md` §P0-X5
- 母 SSOT：`Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md` 序 3
- 平行项：X1（PR #37）/ X2（PR #38）/ X3（PR #39）/ X4（PR #40）已合
- 下一项：M5 stub 模式回填
