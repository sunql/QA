---
created: 2026-10-15
updated: 2026-10-15
sources:
  - docs/superpowers/plans/2026-10-14-aichat-phase03-kickoff.md
  - Harness/changes/2026-10-15-phase3-reconciled-main/summary.md
  - Harness/wiki/phase3-reconciled-main-test-results.md
  - memory/qa-system-phase3-reconciled-main.md
tags: [recon, post-merge, r234, phase3]
status: draft
---

# phase3 合流后 R2/R3/R4 开门问题核验

PR #19 fast-forward 合并到 main @ `4b95098c` 后，phase03-kickoff §1.2/§1.3/§1.4
三个开门问题的核验结果。R1（§1.1 P0-3 vs F1/F1.1b 双写）已在 [[qa-system-p0-3-vs-f1-f11b-recon]]
中确认互补，不再重复。

## 状态总览

| 问题 | plan § | 状态 | 核验时间 |
|---|---|---|---|
| R2 6 个 PR rebase 决策 | §1.2 | **moot** | 2026-10-15 |
| R3 P0-3 之后接线核验 | §1.3 | **live, 全绿** | 2026-10-15 |
| R4 alembic 改号决策 | §1.4 | **moot** | 2026-10-15 |

## R2: moot — Plan B 路径已固化，无 rebase 发生

phase3 走的是 Plan B（squash-replacement），不是把 6 个 PR 单独 rebase。
PR #19 fast-forward 合并到 main @ `4b95098c`，把 11 phase3 commit cherry-pick
到 `bcf2fac0`（#18 squash 后）。原始 #12–#17 全部 closed superseded。
**从未执行 rebase**，所以"rebase 决策"问题已无意义。

证据：
- `git log --oneline 4b95098c` 顶端 11 commit 全是 phase3-reconciler 邮箱
- `gh pr list --state closed --search "phase3"` 显示 #12-#17 + #19
- 合并策略走的是 fast-forward（见 [[qa-system-phase3-reconciled-main]] 合并路径段）

## R3: live, F1-F5 接线点全跟 P0-3 兼容

P0-3 的 `SessionQueryState`（`app/domain/models.py`）已有 `user_id` (BigInteger) /
`datasource_id` (BigInteger) / `datasource_version` (Integer) 三元绑定。F1/F1.1b
是**等价的另一种写法**，需要核验两条路径不双写同一列。

### 关键写点核验（4b95098c 实测）

| 字段 | 写点位置 | 谁写 | P0-3 冲突？ |
|---|---|---|---|
| `user_id` | `chat_context.py:627` (`_saveQueryState` update) | 主流程 | 无（feature branch 不碰） |
| `user_id` | `chat_context.py:644` (`_saveQueryState` insert) | 主流程 | 无 |
| `datasource_id` | `chat_context.py:627/644` 同上 | 主流程 | 无 |
| `datasource_version` | `chat_context.py:627/644` 同上 | 主流程 | 无 |

`git grep -n "datasource_version" backend/app/ @ 4b95098c` 全仓只有 2 处写，
feature branch 都没动。

### F1/F1.1b 关键修复跨 merge 保留

`bcf2fac0` 上的 F1.1b 修复（`userId=user.dbUserId` 而非 `user.userId`，防
PostgreSQL `bigint = varchar` 500）在 `4b95098c` 上完整保留 — `chat_stream.py`
L69–L165 共 7 处替换全部健在（行数从 1360 → 1367，因 F1.1b 主体微调）。
**Why**: 这是 P0-3 三元绑定能正确存 user_id 整数的关键；丢了 = 写库全 500。
**How to apply**: 后续 P0 改动如改 `SessionQueryState.user_id` 类型，必须
同步 grep `userId=user.userId` 全部替换路径。

### F2a / F2c / F3 / F4 / F5

- **F2a** (kpi_catalog.datasource_id) — alembic `0125_kpi_catalog_datasource_fk.py`，
  prod 5433 已迁移 + 字段 verified
- **F2c** (meta 帧合并) — frontend `chatStore.ts` import 集合取并集；无 P0 冲突
- **F3** (`default=str` for SSE) — `chat_stream.py` 末端 + 测试 1 处；纯增量
- **F4** (preamble 单例) — `chat_stream.py` import + 1 个 helper；纯增量
- **F5** (短追问 CLARIFY) — `chat_multistep.py` intent 判定；纯增量

P1-4 `MultiStepDependencyResolver.validate()` (`chat_multistep.py:653`) 与
`session_query_state` 完全解耦（plan 层操作，无 DB 写）。

## R4: moot — alembic 撞号已修，0127 已落 prod

撞号处理与 prod 5433 迁移已在 [[qa-system-phase3-reconciled-main]] §状态 +
[[qa-system-alembic-merge-number-collision]] 完整记录：

- `b1ea6cb32588_run_state.py`（hex 模板）不动
- `0125_kpi_catalog_datasource_fk.py` (#18 F2a)
- `0126_session_token_usage_cost_source.py` (P1-2，原 `0115` 改号)
- `0127_analysis_hypothesis_verification.py` (P1-5，原 `0116` 改号)
- prod 5433: `SELECT version_num FROM alembic_version` → `0127`（2026-10-15 done）
- 6 个 DDL 全部 verified（见 phase3 SSOT §8 实跑记录表）

### ~~小注：commit message drift（不影响正确性）~~ → 已修 2026-10-16

~~P1-2 (`d4bd9dae`) 的 commit message 仍含 `0115` 字样~~ **已修**：`filter-branch --msg-filter`
把 d4bd9dae body 第 7 行 `alembic 0115 加 cost_source 列（head 0114→0115）` 改为
`alembic 0126 加 cost_source 列（head 0125→0126，renumbered from 0115）`；
14 commits 全部 rewrite，`git push --force-with-lease origin main` 落地
（`+ 50619f06...c19a0260 main -> main`）。新 d4bd9dae = `56f39015`，
新 HEAD = `c19a0260`。**Why**: 单 commit message-only 漂移 + 私有仓 + 近期
+ 无外部消费者 ⇒ amend 是最干净的修。**How to apply**: 多人共用 branch 时
改用 follow-up commit。

## 关注点 / 意外

1. `chat_stream.py` 长度：`bcf2fac0` 1360 行 → `4b95098c` 1367 行（+7），
   与 F1.1b 7 处 userId 替换 + 默认 str fallback + preamble 单例的合计行数
   变化一致；**没有意外的 feature 改动**。
2. `datasource_version` 全仓只有 `chat_context.py:627/644` 两处写 —
   P0-3 三元绑定的"单写点"约束成立，**feature branch 没有破坏**。
3. P1-4 multistep dep resolver 与 session_query_state 完全解耦 — **两个
   系统独立演进无副作用**。
4. P1-2 commit message 漂移（`0115` vs `0126`）— 文档性，行为无影响。

## 后续行动推荐

R2/R3/R4 三个开门问题全部关闭。下一阶段方向（按优先级）：

1. **R3 推荐的 prod 烟雾测试**（待 user 授权）：

   ```sql
   SELECT COUNT(*) FROM session_query_state
   WHERE user_id IS NULL AND datasource_id IS NOT NULL;
   ```

   期望：0 行，或仅遗留 pre-P0-3 历史行。验证 P0-3 三元绑定 + F1.1b
   修复在 prod 真实流量下没有写出 NULL user_id 的脏行。

2. ~~**P1-2 commit message 漂移**：amend `d4bd9dae` 改 `0115` → `0126`~~ →
   **已修**（filter-branch + force-push，2026-10-16）。

3. **71 红 backlog**（独立 work）：

   | 套件 | 红数 | 根因 |
   |---|---|---|
   | chat integration | 28 | PLAN_EMPTY × 3 = 无 embedding API key |
   | ontology integration | 9 | Milvus dim(1024) vs embedding 1536 mismatch |
   | wiki integration | 24 | `*_requires_auth` + stub auth 403（container AUTH_MODE=real） |
   | Python 3.14 asyncio | 10 | pre-existing on bcf2fac0 |

4. **worktree 清理**（待 user 确认）：`/tmp/qa-main-merge` + `/tmp/qa-phase3-pr19`。

## 关联

- [[qa-system-phase3-reconciled-main]] — phase3 close-out SSOT
- [[qa-system-p0-3-vs-f1-f11b-recon]] — R1（P0-3 vs F1/F1.1b 互补）
- [[qa-system-alembic-merge-number-collision]] — 撞号修法
- [[qa-system-p1-p2-pr-base-fake-diff]] — 350 文件假象解释
- [[qa-system-bcf2fac0-baseline]] — #18 squash 后 main head
- [[qa-system-no-ci-main-can-go-red]] — 71 红 pre-existing 警示
