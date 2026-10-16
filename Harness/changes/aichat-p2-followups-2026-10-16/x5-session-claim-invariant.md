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
**Commit**：`83ad58ec` fix(research): claim invariant 守卫下沉到 session_claim 共享模块
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
| `backend/app/services/session_claim.py` | 新增（114 行）：两守卫 + 完整 docstring + claim invariant 设计说明 |
| `backend/app/api/v1/research.py` | 8 处 `_ownedSession` → `assert_research_session_ownership`；createTurn 加 `expected="free"`；answerCheckpoint 加 `expected="held"`；删 `_ownedSession` 私有副本 + `_busyMessage` 本地分支文案 |
| `backend/app/tests/integration/test_research_session_claim.py` | 新增（9 个测试）：归属 4 态 + claim invariant 5 态 |
| `backend/app/tests/integration/test_research_api.py` | +1 个 `test_answer_checkpoint_409_when_session_has_no_claim` 端到端；+`_claimSession` helper 模拟真实 flow 的 `active_run_id`（FK 约束要求 `runState.id` 而非 `turn.id`） |

## 验证

- 31 个 research API 测试全绿（含新加的 held 守卫端到端）
- 9 个 session_claim 单元测试全绿
- 67 个 research_session_service + research_agent_service 测试全绿
- **共 108 个研究域测试全绿**

## 反向断言 / 防退化

- `test_claim_invariant_held_409_when_no_active_run` —— 守卫降级 / 守卫前移 / 闸门错位立刻 RED
- `test_ownership_404_when_admin_impostor` —— admin 放行越权立刻 RED（X5 起 admin 也不放行 research 写路径）
- `test_answer_checkpoint_409_when_session_has_no_claim` —— 端到端失守立刻 RED

## 副作用 / 兼容性

- `_ownedSession` 私有副本删除（功能 100% 平移到 `assert_research_session_ownership`，404 文案逐字节一致）
- `_busyMessage` 本地分支文案删除（统一由 `assert_research_claim_invariant` 单点给出）
- **admin 行为**：旧 `_ownedSession` 不放行 admin；新守卫同样不放行（与 chat 域 `session_guard.assertSessionOwnership` 不同 —— research 写路径要求硬归属，避免 admin 误写他人会话；admin 自己的 `dbUserId` 行走正常分支）
- **测试侧**：4 个 `appendTurn + openCheckpoint` 测试加 `_claimSession` helper 模拟真实 flow 的 `active_run_id` 状态；FK 约束要求 helper 用 `runState.id` 而非 `turn.id`（独立 worktree 第一次跑时遇到 FK 错误 → 已修正）

## 阻塞 / 后续

- M5 stub 模式回填（序 4，4h）— 下一项
- L1 prompt 注入（序 6，1d+）
- N4 qwen 知识保留（序 7，独立）

## 链接

- Spec：`docs/superpowers/specs/2026-10-13-1012-code-review-cross-refs.md` §P0-X5
- 母 SSOT：`Harness/changes/aichat-p2-followups-2026-10-16/remaining-work-ssot.md` 序 3
- 平行项：X1（PR #37）/ X2（PR #38）/ X3（PR #39）/ X4（PR #40）已合
- 下一项：M5 stub 模式回填
