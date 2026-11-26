# Phase 3 补接线开门检查（2026-10-14）

> 这份 plan **不写实现任务**——只回答 4 个开门问题，让下一轮 SDD 走对路径。

## 0. 状态快照（2026-10-14 23:xx 实测）

| PR | head ref | base（与 main 的共同祖先）| state | mergeable | files | 唯一新增 migration |
|---|---|---|---|---|---|---|
| #12 | `feat/aichat-p1-1` | `c1857683` | OPEN | **CONFLICTING** | 350 | 无（仅重命名/复用 P0 的 0114） |
| #13 | `feat/aichat-p1-2` | `c1857683` | OPEN | **CONFLICTING** | 350 | `0115_session_token_usage_cost_source.py` |
| #14 | `feat/aichat-p1-3` | `c1857683` | OPEN | **CONFLICTING** | 349 | 无 |
| #15 | `feat/aichat-p1-4` | `c1857683` | OPEN | **CONFLICTING** | 347 | 无 |
| #16 | `feat/aichat-p1-5` | `c1857683` | OPEN | **CONFLICTING** | 351 | `0116_analysis_hypothesis_verification.py` |
| #17 | `feat/aichat-p2-1` | `c1857683` | OPEN | **CONFLICTING** | 348 | 无 |
| #18（本轮新）| `fix/aichat-gap-f5-intent-threshold` | `b81bf1e0` | OPEN | MERGEABLE | 28 | `0125_kpi_catalog_datasource_fk.py` |

**重要校正**（覆盖原 `2026-10-14-aichat-phase01-implementation.md` 里的预判）：

1. **P0 已经在 main 上**（PR #11 = `b81bf1e0`）。memory 里 "必须先合 P0" 的依赖已**不存在**。
2. **6 个 P1/P2 PR 共同 base = `c1857683`**（P0-2 之前的快照），**不是 main head**。它们都是 350 文件、+52k 行级的大块，CONFLICTING 是因为 main 上 P0-2..P0-4、#11 合并、其它人后续改动都未在它们之上 rebase。
3. **`#13` 唯一新增 migration**（`0115_session_token_usage_cost_source.py`）+ **`#16` 唯一新增 migration**（`0116_analysis_hypothesis_verification.py`）—— 这两条都假设 down_revision 接 `0114_multi_step_persist.py`，而 main 上 head 已是 `0124_session_query_state_scope.py`，所以**直接合必撞 alembic 头**。
4. **本轮 #18** 也带 migration：`0125_kpi_catalog_datasource_fk.py`（F2a），与 #13/#16 三个 migration 都得按顺序落。
5. **`b1ea6cb32588_run_state.py`**（research 平台 run_state，down_revision=`0117`）是别人 PR 合入的（六位 hex 是 alembic 默认模板），不与本轮 #18 撞号。

## 1. 4 个开门问题

### 1.1 #18 与 6 个 PR 的代码重叠

我盘上跑了 F1–F5 改动 28 文件、+2029/-60。**但 main 上 P0-3 等价实现**：
- `SessionQueryState`（`app/domain/models.py`）已有 `datasource_id` / `user_id` / `datasource_version`（P0-3 干的）—— 我的 F1/F1.1b 是**等价的另一种写法**，需对照 P0 现有 contract，**确认不重复写同样的列**。
- `kpi_catalog` 在 main 上是 `0023_kpi_catalog.py` 引入——**还没有** `datasource_id` 列。我的 F2a 补这一列到 `0125`，与 P0 路径**不撞**（P0 没动 kpi_catalog）。
- meta 帧合并（F2c）、preamble 单例（F4）、`default=str`（F3）、短追问 CLARIFY（F5）—— 这 4 个是 main 上**没有的**，纯增量。

**行动**：等 #18 squash 落地后，逐个 commit 跑 git blame + grep 看与 main P0 是否真的兼容。

### 1.2 6 个 PR 是否要 rebase 到 #18 之后

**当前 6 个 PR 都基于 `c1857683`**。如果用户先 squash #18，main head 前进 ~12 commit，6 个 PR 必须 rebase 才能合；不 rebase 直接合，CONFLICTING 不会消失。

但 rebase 这 6 个 PR 的成本：
- 每个 +52k 行、350 文件——`git rebase` 跑完要几小时
- rebase 期间还要修 6 套**旧 alembic 文件**（`0114_multi_step_persist.py` / `0114_session_query_state_scope.py` 二选一问题）
- #13 还要把新 migration `0115_session_token_usage_cost_source.py` 改成 `0126_*`（下一个号）
- #16 同样要改 `0127_*`
- P0 期间错号的 `0114_session_query_state_scope.py` 与 `0114_multi_step_persist.py` 要确认谁在 main 上、P0-3 是不是真合了后者

**行动**：在 #18 落地**之前**，先把 6 个 PR 的代码现状摸清——尤其 alembic 链与 model 字段是否与 main P0 后状态有可识别的差异。

### 1.3 「全部补接线」的 P0-3 之后是不是已经接好

本轮 SDD plan 写「Phase 3 = 全部补接线」是 **P0-3 之前** 的裁定，意为 P1/P2 PR 内部"补接线"工作（即不让 YAGNI 砍除）。**但 P0-3 已经合入 main**，P0-3 自身也是补接线工作（SessionQueryState 三元绑定 = 跨用户防串答案）—— **P0-3 与我 PR #18 的 F1/F1.1b 是同一个语义的两条实现**。

这意味着 6 个 PR 里的 P1-1 ~ P1-5、P2-1 大概率已经把 P0-3 当作前置，**不需要再做 P0 的补接线**——那本轮「Phase 3 补接线」实际只剩：
- **#13 的 LlmConfig.capability**（属于能力路由的真接线）
- **#16 的 evidence 回放**（属于研究平台真接线）
- **#14/15/17** 的 CR/LF、依赖、错误分类——这些可能 main 上还没有
- **#12 sqlglot AST 化**——是 refactor，不涉及接线

**行动**：6 个 PR 的 brief 要逐个重读，**找出"接什么线 + 接哪个具体函数/字段"**。memory 笔记说 brief 伪代码系统性不可信——必须 recon。

### 1.4 改号决策

| 计划改动 | 主 branch | 新 revision |
|---|---|---|
| #18 F2a `kpi_catalog.datasource_id` | 已写 | `0125` |
| #13 P1-2 `LlmConfig.capability` 列 | 待合 | `0126` |
| #16 P1-5 `analysis_hypothesis` verification | 待合 | `0127` |
| 别人的 `b1ea6cb32588_run_state` | 已合 | hex（不动） |

不撞号。memory 里 "0126/0127 顺移" 的提法是预判——**实测下来**只有 1 个新号（#13），#16 也占一个（合计 0126+0127）。**这与原 plan 一致**。

## 2. 建议执行顺序

| 阶段 | 做什么 | 依赖 | 谁做 |
|---|---|---|---|
| **A** | squash #18 落地 | 已在 UI | 用户 |
| **B** | 读 6 个 PR 的 brief + 当前 main 代码 + 各 PR 当前分支代码 | A | 我做，1 个 PR 一次 |
| **C** | 输出 6 张「接线图」：每个 PR 真接什么 / 不接什么 / 缺什么 | B | 我做 |
| **D** | 6 张接线图里**有缺口的 PR**（预计 4 个：#13/#14/#15/#16/#17 中至少 4 个）dispatch 修复 implementer | C | SDD loop |
| **E** | 6 个 PR 逐个 rebase 到 #18 落地后的 main，D 的修复分批 cherry-pick 进去 | D | 我做 |
| **F** | 6 个 PR 提 PR → review → 合 | E | 用户 |

## 3. 不在本计划范围

- 性能优化、调参
- chat_service.py 1986 行拆分（memory 已豁免到 2000+）
- pre-existing L1/L4 `"KPI"` / `"AGENT_RUN"` intent 值（独立工单）
- `kpi_catalog.datasource_id` 写入流（独立工单）

## 4. 风险

1. **#18 squash 后 main 出现未期望的 alembic head 错** —— 用 `alembic heads` 在本地 prod-like 5432 库冒烟
2. **6 个 PR rebase 后 350 文件冲突修不完** —— 退路：分批 cherry-pick
3. **6 个 PR 内部已含 P0 重复 commit**，rebase 会被识别为 noop，但 squash 后会消失——是好事
4. **memory 里 #11-#17 P0 重复 diff 假象** —— 已被 P0 已合的事实打破

## 5. 起步动作（下一步）

读完这份 plan，用户**先**：
1. 在 GitHub UI 点 PR #18 的 Squash and merge（commit message 用 PR 标题）
2. 验证 main head 新增 1 commit、28 文件 +2029/-60
3. 验证 alembic head = 0125（不需跑 upgrade，schema 已落）

然后我**继续做**：
- 读 6 个 PR 的 brief 文件（SDD 工作区已有 `task-1-brief.md` 模板，按 plan 名定位）
- 每个 PR 走 1 轮 recon：当前分支代码与 main P0 后的实际 diff、alembic 链、model 字段差异
- 输出 6 张接线图
