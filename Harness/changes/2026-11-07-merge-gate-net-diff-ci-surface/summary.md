# 变更：2026-11-07-merge-gate-net-diff-ci-surface（N6 · merge-gate 的 CI 相关面判定改用「两次 run 实际跑的树」）

- **日期**：2026-11-07
- **作者**：Claude
- **Phase**：Phase 5 工程保障（CI 合并判据工具化）
- **状态**：in-review（待合并）
- ⚠️ **本批含三轮修订**：①「三点 diff」→「净变更」（下称第一版）；
  ② **评审发现的第二个洞**：净变更的**第二棵树**取错了对象（取了 PR 头，
  而 PR 的 CI 实际跑的是 `refs/pull/N/merge`）⇒ 第一版既**系统性假拦**又 **fail-open**
  ⇒ 见 §1.1；③ **第二版（取 `pulls/{n}.merge_commit_sha`）也被否**：
  那个字段是 `refs/pull/N/merge` 的**当前值**（main 一推进就漂移；PR 合并后 404）
  ⇒ 改为**第三版：判定集合 = `A ∪ P`**（`A` = main 的内容推进、`P` = PR 自己的改动）
  ⇒ 见 §1.3 与 §2.7
- **关联变更**：[2026-11-05-defect-priority-plan](../2026-11-05-defect-priority-plan/summary.md)（上一批；§10.5 记录了本缺陷的完整实操与实证）、[2026-11-06-merge-gate-flake-whitelist](../2026-11-06-merge-gate-flake-whitelist/summary.md)（B7 抖动白名单）
- **迁移版本**：无（本批**零 DDL、零生产写、零生产读**）
- **MEMORY**：[qa-system-merge-gate-net-diff](../../../../../.claude/projects/-Users-sunql-Prejectcode-th-MyWiki-wiki-aicode-qa-system/memory/qa-system-merge-gate-net-diff.md)

---

## 1. 缺陷

merge-gate（失败集合对差闸门）判「PR 是否改了 CI 相关面」时，
用 GitHub compare API 的**三点** diff（`compare/{base}...{head}`，merge-base 语义）取改动文件。

**根因**：三点从 **merge-base** 起算。分支若含「**重建 main 的 tree 的合成提交**」
（tree 与 main 的 tip 相同、但不是同一个 commit）⇒ main 的 tip **不是**分支的祖先
⇒ merge-base 退回到**老底座** ⇒ 三点 diff 把 **main 自己的**改动全算成「PR 改的」：

- `.github/workflows/ci.yml`
- `.github/workflows/merge-gate.yml`
- `.github/CODEOWNERS`
- `backend/app/utils/ci_*.py`（判定工具自身）

⇒ **假** `CI_SURFACE_CHANGED`（闸门退出码 6）⇒ 白拦一次，PR 作者还得再走一轮人工仲裁。

### 1.1 实证（2026-11-05，真实 GitHub API）

对触发该缺陷的那次 PR：

| base | head | 三点 diff 文件数 | 树对树净变更文件数 |
|---|---|---|---|
| `b6e809a2` | `97047b8b` | **158** | **11** |

- **158** 个里混着 main 自己的 `.github/workflows/ci.yml`、`merge-gate.yml`、
  `CODEOWNERS`、`backend/app/utils/ci_*.py` ⇒ CI 相关面命中 ⇒ 假拦。
- **11** 个才是 PR 的真实改动 ⇒ CI 相关面命中 **0** ⇒ 不该拦。

⇒ 两种口径差 **14 倍** ⇒ 问题不在「阈值」上，在「口径」上。

### 1.2 第二个洞（2026-11-07 评审发现）：第一版修法的**第二棵树取错了对象**

第一版（§2.1）把判定集合改成「净变更」后，**第二棵树取的仍是 PR 头** ⇒
比的根本不是两次 run 跑过的东西。

**事实**：`ci.yml` 对 `pull_request` 触发，三个 `actions/checkout` **都不带 `with: ref:`**
⇒ 检出的是 `refs/pull/N/merge` ⇒ **PR 的 CI 跑的是 merge(main@run 时点, PR 头)**。

实证（PR #111，run `42281369007`）：

- run 对象的 `head_sha` = **PR 头** `1d226997…`；
- job 日志写着 `HEAD is now at 23db713 Merge 1d226997… into 87f8e8e6…`
  ⇒ **实际执行的是合并 commit `23db713…`**，不是 PR 头。

⇒ 两个后果（第二个更严重）：

1. **系统性假拦**：main 改了 CI 文件、PR 没跟上 ⇒ PR 头树里**没有** main 的改动
   ⇒ 差异被放大成「PR 改了 CI 文件」。
   实测 PR #92/#94/#95/#107 的净变更 CI 命中 **1/29/29/17**，而它们的三点归因命中 **0**。
2. **fail-open（更严重）**：PR 头树与基线树**同样旧** ⇒ 差异为空 ⇒ 判「没改」
   ⇒ 而 PR run 实际跑的是合并后的**新** CI ⇒ 两次 run 的 CI 定义不同
   ⇒ 失败集合不可比 ⇒ **判不出来** —— 这正是本批要消除的那个洞反而没闭上。

### 1.3 第三个洞（2026-11-07 第三版评审）：第二版的「测试合并 commit」**取不到**

第二版的思路是「直接取 `refs/pull/N/merge` 那个 commit 的树当第二棵树」。但：

| 取法 | 问题 |
|---|---|
| `GET /pulls/{n}.merge_commit_sha` | 这是 `refs/pull/N/merge` 的**当前值**——main 一推进它就**漂移**（不是历史 run 当初用的那个）；PR 合并后该 ref 被删 ⇒ **404** ⇒ 历史 run 的第二棵树**根本取不到** |
| `GET /git/commits/{mergeSha}` | 同上：sha 漂移 ⇒ 取到的是**别的**那一次合并 |

⇒ 第二版在「回溯历史 run」这件事上**根本不可实现** ⇒ 必须换成**不依赖那个临时 ref** 的算法。

⇒ 第三版：**用「main 的推进」+「PR 自己的改动」在算法上合成**出第二棵树（§2.7）。

## 2. 修法（最终设计）

### 2.1 判定集合 = 「两次 run 的净变更」∩「CI 相关面清单」

| 集合 | 回答的问题 | 采集方式 | 用途 |
|---|---|---|---|
| 两次 run 代码的**净变更** | 两次 run 跑的那两份代码**哪里不同** | Trees API 树对树比（`git/trees/{sha}?recursive=1`） | **判定** |
| PR 相对分叉点的改动（**三点**） | **谁**改的 | compare 只取 `merge_base_commit`，差异自己用树对树算 | **归因措辞** |

**为什么判定必须落在净变更上**（2026-11-07 两路评审共同定稿）：

「失败集合对差」的前提是**两次 run 的失败集合可比**。而「main 在 PR 分叉之后改了 CI 文件、
PR 分支没跟上」这一情形 ⇒ 两次 run 跑的 **CI 定义本身就不同** ⇒ 失败集合**不可比**
⇒ 必须拦。若用「三点归因 ∩ 净变更」做判定，这种情形归因集为空 ⇒ **被放过去**
⇒ **fail-open 洞**。⇒ 判定集合必须是「两次 run 跑的东西是不是同一套 CI 定义」
= **净变更 ∩ CI 相关面**。

**为什么归因集还要保留三点**：净变更不区分「谁改的」。⇒ 报告要能说清是
「PR 自己改了 CI 文件」（PR 的责任）还是「**归因不到本 PR**」（通常 = main 改了而 PR 没跟上）
⇒ 两种情形的处理与措辞不同（见 §3 的 `_ciSurfaceReason`）。

### 2.2 为什么不用 compare 的 `files` 数组

GitHub REST 的 compare **只支持 `base...head`**（`base..head` 实测 **404**）⇒ 两点语义必须**自己算**。
且 compare 的 `files` 数组**最多 300 项**、**静默截断**（`--paginate` 只对 `commits` 分页）
⇒ 大 PR 的改动清单不完整 ⇒ 真改的 CI 文件被排除 ⇒ **fail-open**。

⇒ 改成：**compare 只取 `merge_base_commit.sha`**（分叉点），差异一律走 Trees API 树对树
⇒ **无 300 项上限**（Trees API 自己的上限是响应里的 `truncated`，已 fail-closed）。
⇒ 三点语义由此**自己**算出来（`diff(merge_base, head)`），与 compare 的三点 diff 等价。

### 2.3 基线可信性（2026-11-07 安全评审 HIGH-1）

GitHub 的 `runs?branch=<name>` 过滤匹配的是 `head_branch`、**不区分事件**
（实测：`branch=feat/merge-gate-flake-whitelist` 返回的是 `event=pull_request` 的 run）
⇒ 一个**名叫 `main` 的 fork 分支**发起的 PR 也会命中 `branch=main`
⇒ 它的 run 可能被当成「main 的基线」⇒ **基线 = 攻击者自己的代码**
⇒ 失败集合对差被中和 ⇒ 闸门失去意义。

⇒ 三条硬条件（缺一不可），**工具侧独立复核**（不能只靠 workflow 的查询串）：

1. `event == "push"`；
2. `head_branch == "main"`；
3. `head_repository == 本仓库`。

⇒ 不满足 ⇒ **退出码 3**（运行错误/须复跑，**不是** 6）：
「PR 改了哪些 CI 文件」根本没判 ⇒ 语义应是「判定不出来」；
且**在采集 diff 之前**就 bail ⇒ 不用不可信基线对差。

⇒ workflow 侧同时收紧：基线查询串加 `event=push`，jq 再筛一次 `.event == "push"`
（双保险：查询参数可能被服务端忽略 ⇒ jq 侧独立把关）。

### 2.4 fail-closed 纪律

| 情形 | 行为 |
|---|---|
| Trees API 响应 `truncated=true` | **报错** ⇒ 判定不出来 ⇒ 退出码 6 |
| Trees API 响应**缺** `truncated` 键 | **报错**（缺失 ≠ 未截断）⇒ 退出码 6 |
| 递归树**没有展开**（目录条目下无子条目） | **报错** ⇒ 退出码 6 |
| 条目字段畸形 / 出现未知 `type` | **报错** ⇒ 退出码 6 |
| 净变更或归因集拉不到（网络/权限/gh 失败） | **报错** ⇒ **不退回三点 diff** ⇒ 退出码 6 |
| 基线 run 不可信 | **退出码 3**（见 §2.3） |

⇒ 任何一步拿不到完整清单 ⇒ 「不知道」≠「没有」⇒ 保守拦下。

### 2.5 条目类型过滤

递归树里混着三种条目：`blob`（普通文件）、`tree`（目录）、`commit`（子模块）。

- `blob` / `commit` ⇒ **收**（子模块是一个路径级的指针变化）；
- `tree` ⇒ **排除**（目录不是文件；「一边多一个空目录」不是改动）。

⇒ 递归树里本仓实测 **2946** 条目（其中 **368** 个目录条目被排除）、
`truncated=false`、types `{tree: 368, blob: 2577, commit: 1}`；
**368 个目录条目全部有子条目**（0 违例）⇒ 「递归确实生效」可写成 fail-closed 断言。

### 2.7 ⚠️ 第三版（定稿）：判定集合 = `A ∪ P`（不依赖 `refs/pull/N/merge` 那个临时 ref）

> 下面 §2.1–§2.6 是**第一版**；§2.7 是 2026-11-07 第三轮评审后的**定稿**。
> 第二版（取 `pulls/{n}.merge_commit_sha` 的树）已被**否**（见 §1.3）。

**三个记号**：

| 记号 | 含义 | 取法 |
|---|---|---|
| `M_base` | 基线 run 的 head sha | 基线 run 的 `head_sha` |
| `M_T` | **PR run 开跑那一刻 main 的 tip**（= 那次合并的 main 侧底座） | 见下面的「`M_T` 怎么取」 |
| `P` | **PR 自己**改的文件 | `diff(merge_base(M_T, PR 头), PR 头)`（三点语义） |

**判定集合**：

| 集合 | 内容 | 用途 |
|---|---|---|
| `A` | main 在 `[M_base, M_T]` 之间的**内容推进**（树对树） | 参与判定 |
| `P` | PR 自己改的文件 | 参与判定 + 措辞 |
| **判定集合 = `A ∪ P`** | | ∩「CI 相关面」⇒ 命中 ⇒ 退出码 6 |

**为什么 `A ∪ P` 就是「两次 run 实际跑的那两份代码的差异」**：

PR 的 CI 跑的 commit 是 `merge(M_T, PR 头)`，其**树**逐路径看是：

> 对每个路径 `p`：**PR 动过 `p` 就取 PR 的版本**，否则**取 `M_T` 的版本**。

⇒ 「基线树 vs 实际执行的树」的**路径级差异** = 「基线树 vs `M_T` 树」的差异（`A`）
∪ 「PR 动过的路径」（`P`）⇒ **判定集合 = `A ∪ P`**。
⇒ 这样**完全不需要**去取那个临时 ref `refs/pull/N/merge` ⇒ §1.3 的洞自动消失。

**为什么不能只算一边**（两条都是实测过的失败模式）：

| 只算 | 漏掉的情形 | 后果 |
|---|---|---|
| 只算 `P` | main 改了 CI 文件、PR 没跟上 | **fail-open**（两次 run 的 CI 定义不同却判「没改」） |
| 只算 `A` | PR 自己改了 CI 文件 | **假放行**（PR 改判据却看不见） |

⇒ 必须算 **`A ∪ P`**。

**`M_T` 怎么取**（`ci_gh.fetchMainTipShaAt`）：

1. 上界 = **PR run 的 `created_at`（事件派发时刻）+ 120 秒宽限**
   （`_MAIN_TIP_GRACE_SECONDS = 120`）。
   ⚠️⚠️ **必须用 `created_at` 而不是 `run_started_at`**（2026-11-07 代码评审 CRITICAL-1）：
   `pull_request` 事件的**合并 commit 在事件派发那一刻就被冻结**了
   ⇒ 复跑（re-run）**不会**重新计算它 ⇒ 复跑检出的仍是**同一个**合并 commit
   ⇒ 它的 main 侧底座是**派发那一刻**的 main ⇒ 上界必须锚**派发时刻**。
   （早期版本误判「复跑会在新时刻重新检出 ⇒ 该用 `run_started_at`」——
   **实测推翻了这个前提**，见 §2.7.1。）
2. 取 **main 的 push run 列表**（`GET /repos/{o}/{r}/actions/runs?branch=main&event=push`），
   逐条过**三条硬条件**（缺一不可）：
   - `event == "push"`（查询串可能被服务端忽略 ⇒ 工具侧独立复核）；
   - `head_branch == "main"`；
   - `head_repository == 本仓库`（**大小写不敏感**比较——GitHub 仓库名大小写不敏感，
     严格相等会把**合法基线**判成不可信 ⇒ 误判退出码 3 ⇒ 白拦）。
3. 再过时间窗：`created_at <= cutoff` ⇒ 取**最新**的一条的 `head_sha`。
4. 一条都不合格 ⇒ **报错**（判定不出来）⇒ **不许随便返回一个 sha**。

⚠️ **`M_T` 取不到 ⇒ 判定不出来 ⇒ 退出码 6，不许退回 PR 头**
—— 退回 PR 头 = 把 §1.2 的两个洞带回来。

⚠️ **`M_T` 由工具侧独立取**，**不由闸门代取后传入**
—— 否则工具就失去了「基线 = fork 上同名 main 分支」那个洞的独立复核机会
（`test_merge_gate_leaves_the_main_tip_to_the_tool` 钉住这条契约）。

### 2.7.1 ⚠️ 第四版（2026-11-07 代码评审 CRITICAL-1）：时间锚点 = **事件派发时刻**

**前一版的前提是错的。** 第三版写「复跑（re-run）会在新的时刻重新检出
`refs/pull/N/merge` ⇒ 该用 `run_started_at`」——**实测推翻了这个前提**：

> `pull_request` 事件的**合并 commit 在事件派发那一刻就被冻结**了
> ⇒ 复跑**不会**重新计算它 ⇒ 复跑检出的仍是**同一个**合并 commit
> ⇒ 它的 main 侧底座是**派发那一刻**的 main。

**实测（PR #105 run `41908498247`，attempt=2）**：

| 事实 | 值 |
|---|---|
| run 的 `created_at`（**事件派发时刻**） | `2026-11-04T20:38:19Z` |
| run 的 `run_started_at`（attempt 2 开跑时刻） | `2026-11-04T22:24:11Z` |
| 合并 commit `57a267a93f97cf54ac4bf98ea469576530a96f61` 的 `committer.date` | `2026-11-04T20:38:18Z` |
| 该合并 commit 的**第一父**（= main 侧底座） | `83581e10ae18aafb379682a234de6fd77e3d94db` |
| attempt-2 的 job 日志 | `git fetch … +57a267a93f…:refs/remotes/pull/105/merge` + `HEAD is now at 57a267a Merge a4196c2c… into 83581e10…` |

⇒ 合并 commit 的 `committer.date`（`20:38:18Z`）与 run 的 `created_at`（`20:38:19Z`）
**几乎同时** ⇒ 它就是**派发那一刻**算出来的 ⇒ 复跑不动它。
（第二个数据点：PR #111 run `42281369007`：`created_at=run_started_at=2026-11-06T19:38:13Z`，
合并 commit `23db713` 的 `committer_date=19:38:11Z`，第一父 `87f8e8e6` = 派发时的 main。）

**错锚的失败场景（fail-open）**：main 先后推了两次 ——
`83581e10` → run `41900595777`（created `19:49:02Z`）；`8411697e` → run `41924916158`
（created `22:23:53Z`，**只改了 `.github/workflows/merge-gate.yml`**）。
用旧的 `run_started_at` 上界 ⇒ `M_base` 与 `M_T` **双双落到 `8411697e`** ⇒ `A = ∅`
⇒ 闸门判「可合并」—— 而**两次 run 实际跑的 CI 定义不同** ⇒ **fail-open**。

⇒ **工具侧（`ci_gh.mainTipCutoff`）与 workflow 侧（`PR_RUN_CREATED_AT`）必须同时换锚**：
一边换一边不换 ⇒ 同样 fail-open（基线树 ≠ 执行树）。

⚠️ **`mainTipCutoff` 的两个时间戳必须 keyword-only 传**：
这次的缺陷正是「**位置传参 + 锚点写反**」⇒ 两个时间戳**都是合法值**
⇒ 传反了**不会报错** ⇒ 判定静默用错锚点。
⇒ 改成 `mainTipCutoff(*, createdAt: str, runStartedAt: str)`
⇒ 顺序再也没法搞错（守卫：`test_mainTipCutoff_requires_explicitly_named_timestamps`）。

⚠️ **`created_at` 缺失时回退 `run_started_at`**（偏保守 ⇒ 宁可多仲裁一次）；
两者都没有 ⇒ **判定不出来**（抛错 ⇒ 工具侧转成退出码 3）。

### 2.8 基线取法同步收紧（workflow 侧）

基线必须是 **PR 的 CI 事件派发之前** main 上最新完成的 push run：

- 理由：PR run 执行的是「**PR CI 事件派发那一刻的 main**」⇒ 基线若取了**之后**才推的 main
  ⇒ 两次 run 跑的代码天然不同 ⇒ 又是**系统性假拦**。
- 实现：workflow 从 `github.event.workflow_run` 里取 **`created_at`（事件派发时刻）**
  作为 `created_at <=` 的**上界**筛 main 的 push run。
- ⚠️ `PR_RUN_CREATED_AT` **为空 ⇒ 显式 fail-closed**（空串会让 jq 的
  `.created_at <= ""` **恒真** ⇒ 基线退化成「main 上最新 run」⇒ 又变回系统性假拦）。

### 2.9 底座核对（`M_base` vs `M_T`）

- 若 **基线 run 的 commit ≠ `M_T`** ⇒ main 在基线之后推进过 ⇒ 两次 run 的 CI 定义可能不同
  ⇒ 报告里必须**提示 + 给出处置办法**：「**重新运行 PR 的 CI**」（让它合上最新的 main），
  而不是让操作者直接放行。
- ⚠️ 这条提示**只在判出 CI 相关面变更时**出现（第三版里 `M_base ≠ M_T` 是**常态**
  —— main 一直在推进 ⇒ 无条件提示就是噪音 ⇒ 提示一旦变成噪音就会被无视 ⇒ 提示就失效）。
  ⇒ 反向守卫：`M_base == M_T` 时**不得**出现该提示。

### 2.6 抖动豁免的前置条件也改用判定集合

豁免的「该用例所在文件**没被改过**」这个前置条件，若只用三点归因集
⇒ main 侧改过的测试文件会被当成「没改过」⇒ **对称豁免吃掉真回归**。
⇒ 前置条件同样用**判定集合**（`A ∪ P`）。

## 3. 变更清单

| 文件 | 动作 |
|---|---|
| `backend/app/utils/ci_gh.py` | 重写：新增 Trees API 链路（`fetchTreePathShas` / `_parseTreePathShas` / `_buildTreeFileMap` / `_assertTreeIsRecursive` / `_diffTreePathShas` / `fetchNetChangedFiles`）、`fetchMergeBaseSha` / `_parseMergeBaseSha`；`fetchChangedFiles` 改为「merge-base 树 vs head 树」；**删除** `_parseChangedFiles` 与 300 项上限逻辑；所有外部输入加形态校验（repo / run id / sha / branch）。**第三版**：`RunHeadInfo` 增 `event` / `createdAt` / `runStartedAt` 三字段；新增 `mainTipCutoff` / `fetchMainTipShaAt` / `_parseMainTipSha` / `_considerMainRun`（`M_T` 的取数链路）与 `_MAIN_TIP_GRACE_SECONDS = 120`。**第四版（CRITICAL-1）**：`mainTipCutoff` 的**锚点从 `run_started_at` 换成 `created_at`（事件派发时刻）**、参数改成 **keyword-only**（`createdAt=` / `runStartedAt=`）⇒ 杜绝「位置传参写反」；`created_at` 缺失才回退 `run_started_at`，两者皆缺 ⇒ 抛错（判定不出来）；**删除**第二版整条「测试合并 commit」链路（`PullMergeInfo` / `fetchPullMergeInfo` / `_findOpenPullNumber` / `_parseOpenPullNumbers` / `_fetchPullMergeCommitSha` / `_fetchCommitTreeAndParent`）——`refs/pull/N/merge` 是临时 ref，**回溯历史 run 时取不到**（§1.3） |
| `backend/app/utils/ci_surface.py` | `CiSurfaceInfo` 增 `netChangedFiles` / `unattributedCiFiles` / `baseTrustError` / `mainAdvanceFiles` / `mainTipSha`；新增 `_baseTrustError`（三条硬条件）。**第三版**：判定集合改为 `A ∪ P`（`_buildSurface` 里 `judgment = set(mainAdvance) | set(prChanged)`）；新增 `_fetchMainTipSha` / `_fetchMainAdvanceFiles` / `_fetchPrChangedFiles`；**删除**第二版的 `_fetchMergeInfo` / `_fetchAttributionFiles` / `_fetchComparedFiles` 与 `prMergeSha` / `prMergeBaseSha` 两字段。**第四版（CRITICAL-1）**：`_fetchMainTips` 的 cutoff 改用 `mainTipCutoff(createdAt=prInfo.createdAt, runStartedAt=prInfo.runStartedAt)`（**具名传**）⇒ 锚点与工具侧一致 |
| `backend/app/utils/ci_failure_set.py` | 判定集合改为 `detectCiSurfaceChanges(surface.netChangedFiles)`；`_ciSurfaceReason` 分「PR 改了」/「归因不到本 PR」两种措辞；豁免前置条件改用 `netChangedFiles`；基线不可信 ⇒ 退出码 3；报告 notes 增「归因不到本 PR」条目。**第三版**：`_mergeBaseMismatchNote` 改用 `surface.mainTipSha`（不再是 `prMergeBaseSha`） |
| `.github/workflows/merge-gate.yml` | 基线 run 查询限定 `event=push` + jq 复核 `.event == "push"`。**第二版**：基线取法加**时间上界** —— 只认 `created_at <= PR run 的时间戳` 的 main push run（用**独立 jq 二进制**的 `--arg` 传时间戳；`gh --jq` 不支持 `--arg`）；**`PR_RUN_CREATED_AT` 为空 ⇒ 显式 fail-closed**。**第三版**：时间戳取 `run_started_at`。**第四版（CRITICAL-1）**：改取 **`created_at`（事件派发时刻）** —— `PR_RUN_CREATED_AT: ${{ github.event.workflow_run.created_at }}`（合并 commit 在派发时冻结 ⇒ 复跑不动它 ⇒ 上界必须锚派发时刻，详见 §2.7.1） |
| `.github/CODEOWNERS` | 新增 `backend/app/tests/unit/test_ci_main_tip_note.py` / `test_ci_main_tip.py` / `test_ci_mutation_evidence.py` / `test_ci_failure_set_surface.py` 与 `backend/scripts/ci_mutation_cases.py` 的 CODEOWNER（新增守卫文件**必须**有 CODEOWNER ⇒ 否则 `test_ci_codeowners.py` 的反向守卫会红 ⇒ 缺口本身会挡住合并）|
| `backend/scripts/ci_mutation_evidence.py` | M27 锚点对齐现状；新增 M29–M36 八组；**第二版新增 M37–M47**；**第三版重锚 M37–M47 并新增 M48–M55**；**第四版重锚 M46/M48/M49/M50/M39 并新增 M56–M58**（共 **58** 组）。并把 `run_case` 的判据从**布尔**改成**三态**（`red` / `green` / `inconclusive`）——只有 pytest 退出码 **1** 才算「转红」，2/3/4/5 一律判「没跑起来」⇒ 取证失败（不是转红）。**收尾拆分**：清单（55 组数据）移到 `backend/scripts/ci_mutation_cases.py`，本文件只留**跑者**（改坏 → 跑用例 → 判三态）——拆分前 826 行 ⇒ 超 800 行上限 ⇒ 拆后 159 + 695 行 |
| `backend/scripts/ci_mutation_cases.py` | **收尾新建**：变异的**纯数据**清单（与跑者**同族** ⇒ 同进 CI 相关面 + CODEOWNERS）；**第四版**增到 **58** 组 |
| `backend/app/tests/unit/test_ci_gh.py` | 树解析 / merge-base / 三点语义 / 无 300 上限 / 改名 四个小节；树不变式 3 条；`truncated` 的载荷改成**带嵌套路径**。**第三版**：`RunHeadInfo` 增两字段后 `_headInfo` 同步；`test_parseTreePathShas_rejects_a_string_false_truncated_flag` 随文件拆分搬回本文件 |
| `backend/app/tests/unit/test_ci_main_tip.py` | **第三版新建**：`mainTipCutoff`（**第四版**：优先 `created_at` / 回退 `run_started_at` / 两者皆缺则抛错 / **必须 keyword-only 传**）、`_parseMainTipSha`（三条硬条件 + 时间窗 + 分页）、`fetchMainTipShaAt`（查询串 + `--paginate --slurp`）；**第四版新增 PR #105 复跑锚点的离线回放**（证明夹具有鉴别力：错锚给出另一个答案） |
| `backend/app/tests/unit/test_ci_surface.py` | **第三版重写**（737 行）：判定集合 = `A ∪ P`、`M_T` 的取法与 fail-closed、归因措辞 |
| `backend/app/tests/unit/test_ci_main_tip_note.py` | **第三版新建**：基线 ≠ `M_T` ⇒ 报告里必须有提示**与处置办法**；同底座 ⇒ **不**出现该提示（反向守卫）；「归因不到本 PR」必须如实说 |
| `backend/app/tests/unit/test_ci_mutation_evidence.py` | **第三版新建**：钉住 `run_case` 的**三态**语义（退出码 0/1/2/3/4/5 各自的判定）——防止把「用例没跑起来」冒充成「守卫抓到了变异」|
| `backend/app/tests/unit/test_ci_merge_gate_workflow.py` | 「基线查询只取 push run」1 条；基线取「PR CI 事件派发之前」的 run 1 条；`PR_RUN_CREATED_AT` 暴露给 diff 步骤 1 条；空时间戳 fail-closed 1 条；**第四版**：**必须取 `created_at`、不得取 `run_started_at`** 1 条；**`M_T` 必须由工具独立取** 1 条 |
| `backend/app/tests/unit/test_ci_head.py` | `RunHeadInfo` 增 `createdAt` / `runStartedAt` 后 `_headInfo` 同步 |
| `backend/app/tests/unit/_ci_gate_support.py` | `stubCiSurface` 增 `netChangedFiles` / `unattributedCiFiles` / `baseTrustError` / `mainAdvanceFiles` / `mainTipSha` 参数 |
| `backend/app/tests/unit/test_ci_failure_set.py` / `test_ci_failure_set_flakes.py` | **收尾拆分**：新增的三条（「归因不到本 PR ⇒ 退出码 6」「基线不可信 ⇒ 退出码 3」「两侧任一侧改过的文件 ⇒ 不豁免」）移到新文件 `test_ci_failure_set_surface.py` ⇒ 两个原文件回到各自的基线行数（1243 / 782，**前者本就超 800 行上限，属既有欠账，本批不使其更糟**）|
| `Harness/rules/开发流程规范.md` | 第 4 条：判定集合表 + 三条硬纪律 + 基线可信性 + 豁免前置条件 + 「分支以 main 的 tip 为底座起」的过程规则 |
| `Harness/changes/2026-11-05-defect-priority-plan/plan.md` / `summary.md` | N6 行更新为最终设计 |

**本批零 DDL、零生产写、零生产读。**

## 4. 验收证据

### 4.1 测试

`pytest app/tests/unit -k 'ci_'`：**337 passed / 19 skipped / 0 failed**（19 skipped 全部是
「行为部分需要 bash ≥ 4.4」的本机限制，CI 必跑）。

- 第三版新增用例：`test_ci_surface.py`（重写，34 条）、`test_ci_main_tip.py`（新建，17 条）、
  `test_ci_main_tip_note.py`（新建，4 条）、`test_ci_mutation_evidence.py`（新建，7 条）、
  `test_ci_merge_gate_workflow.py`（+2 条）、`test_ci_codeowners.py`（+1 条）。
- **净新增失败 0**（main 基线本身是红的 ⇒ 判据是**失败集合对差**，不是「变绿」）。

### 4.2 变异取证（守卫的验收标准 = 「守卫会红」≠「守卫在测你想测的东西」）

第一版新增 8 组（`backend/scripts/ci_mutation_evidence.py`）：

| # | 变异 | 期望转红的用例 | 实测 |
|---|---|---|---|
| M29 | 判定集合退回三点归因集（`netChangedFiles` → `changedFiles`） | `test_main_returns_six_when_ci_surface_differs_but_is_not_attributable_to_the_pr` | **转红** ✔ |
| M30 | 豁免前置条件退回三点归因集 | `test_main_does_not_exempt_a_test_whose_file_changed_on_either_side` | **转红** ✔ |
| M31 | 去掉基线可信性校验（`trustError = None`） | `test_resolveCiSurface_rejects_a_base_run_that_is_not_a_push_run` | **转红** ✔ |
| M32 | 树解析：缺 `truncated` 键当作未截断 | `test_parseTreePathShas_rejects_a_missing_truncated_flag` | **转红** ✔ |
| M33 | 树解析：去掉「递归确实生效」不变式 | `test_parseTreePathShas_rejects_a_non_recursive_tree` | **转红** ✔ |
| M34 | 树解析：未知条目类型放行 | `test_parseTreePathShas_rejects_an_unknown_entry_type` | **转红** ✔ |
| M35 | compare 端点改成两点（`...` → `..`） | `test_fetchMergeBaseSha_calls_the_three_dot_compare_endpoint` | **转红** ✔ |
| M36 | merge-base 缺失时不报错（返回假 sha） | `test_parseMergeBaseSha_rejects_a_response_without_merge_base` | **转红** ✔ |

另有 M27（工具模块清单漏 `judge`/`report`/`flakes`）——**本批顺带修好了它的过期锚点**
⇒ 重跑后 **转红** ✔。

### 4.2.1 第三版重锚 + 新增 19 组（M37–M55）

| # | 变异 | 期望转红的用例 | 实测 |
|---|---|---|---|
| M37 | 判定集合**只算 `P`**（丢掉 main 的推进 ⇒ fail-open） | `test_resolveCiSurface_judges_on_the_union_of_main_advance_and_pr_changes` | **转红** ✔ |
| M38 | 判定集合**只算 `A`**（丢掉 PR 自己的改动 ⇒ 假放行） | `test_resolveCiSurface_flags_a_ci_file_the_pr_itself_changed` | **转红** ✔ |
| M39 | `M_T` 取不到时**退回 PR 头**（不 fail-closed） | `test_resolveCiSurface_fails_closed_when_the_main_tip_is_unknown` | **转红** ✔ |
| M40 | `truncated` 放行**字符串** `"false"` | `test_parseTreePathShas_rejects_a_string_false_truncated_flag` | **转红** ✔ |
| M41 | `truncated` 改**真值判断**（`0` 被当成「未截断」⇒ fail-open） | `test_parseTreePathShas_rejects_a_zero_truncated_flag` | **转红** ✔ |
| M42 | 去掉「**祖先齐全**」不变式 | `test_parseTreePathShas_rejects_a_response_missing_ancestor_entries` | **转红** ✔ |
| M43 | 去掉「**响应含带 `/` 的路径**」不变式 | `test_parseTreePathShas_rejects_a_fully_flat_response` | **转红** ✔ |
| M44 | 不再筛 `event == "push"` | `test_parseMainTipSha_ignores_a_pull_request_run` | **转红** ✔ |
| M45 | 不再筛 `head_branch == "main"` | `test_parseMainTipSha_ignores_a_run_on_another_branch` | **转红** ✔ |
| M46 | workflow 基线取法去掉**时间上界**（`created_at <=`） | `test_merge_gate_base_run_is_bounded_by_the_pr_run_dispatch_time` | **转红** ✔ |
| M47 | 底座不一致的**提示被去掉** | `test_main_does_not_report_a_mismatch_when_the_base_is_the_same_commit` | **转红** ✔ |
| M48 | `M_T` 上界**优先取 `run_started_at`**（⇒ 复跑时取到派发之后的 main ⇒ fail-open） | `test_mainTipCutoff_prefers_created_at` | **转红** ✔ |
| M49 | 去掉 **120 秒宽限** | `test_mainTipCutoff_applies_the_grace_window` | **转红** ✔ |
| M50 | 两个时间戳都缺时**默认放行**（用当前时间） | `test_mainTipCutoff_raises_when_both_timestamps_are_missing` | **转红** ✔ |
| M51 | 不再筛 `head_repository == 本仓库`（⇒ 基线可能是 fork 上的同名分支） | `test_parseMainTipSha_ignores_a_run_from_another_repository` | **转红** ✔ |
| M52 | 不再按**时间窗**筛（取到 PR CI 之后的 main） | `test_parseMainTipSha_ignores_a_run_after_the_cutoff` | **转红** ✔ |
| M53 | 一条 run 都不合格时**随便返回一个 sha** | `test_parseMainTipSha_raises_when_no_run_qualifies` | **转红** ✔ |
| M54 | `run_case` 放宽成「退出码非 0 都算转红」（⇒ 收集失败冒充转红） | `test_run_case_treats_a_non_assertion_exit_code_as_inconclusive` | **转红** ✔ |
| M55 | 「归因不到本 PR」的计算**不减掉 PR 自己改的** | `test_resolveCiSurface_flags_a_ci_file_the_pr_itself_changed` | **转红** ✔ |

⇒ **58 组变异（M1–M58）全部被守卫抓住** ⇒ 净漏 **0**（其中本批新锚/新守卫 = M37–M58）。

### 4.2.2 第四版重锚 + 新增 3 组（M56–M58）

| # | 变异 | 期望转红的用例 | 实测 |
|---|---|---|---|
| M56 | workflow 的 `PR_RUN_CREATED_AT` 改取 `run_started_at` | `test_merge_gate_uses_created_at_not_run_started_at` | **转红** ✔ |
| M57 | 工具侧调用点把两个时间戳**传反**（`createdAt=` / `runStartedAt=` 对调） | `test_resolveCiSurface_derives_the_main_tip_cutoff_from_created_at` | **转红** ✔ |
| M58 | 去掉 `mainTipCutoff` 的 **keyword-only**（`*`）⇒ 位置传参又变得可能 | `test_mainTipCutoff_requires_explicitly_named_timestamps` | **转红** ✔ |

⇒ M56/M57 覆盖「**两边锚点必须同时换**」：任一边单独改坏 ⇒ 对应守卫立刻红。
⇒ M58 覆盖「**防止位置传参把锚点写反**」（本次缺陷的真正成因）。

全量重跑 `backend/scripts/ci_mutation_evidence.py`（**58 组**）：**合计 58 组变异，净漏 0**。

> ⚠️ **全量首跑曾报「净漏 1」（M39）** —— 不是守卫漏网，是**变异锚点过期**：
> `M_T` 改成「两条腿」之后，`_fetchMainTips` 里那段「取不到就退回 PR 头」的源码**已被重写**
> ⇒ 变异脚本在文件里找不到锚点（0 次）⇒ 该组**根本没跑**。
> ⇒ 已把 M39 的变异改到**现存的 run 腿**上（`except _FETCH_ERRORS: runBasedSha = prInfo.headSha`
> —— 语义不变：仍是「取不到 `M_T` 就退回 PR 头」）⇒ 单独重跑 **转红 ✔**。
> ⇒ **教训**：变异脚本自身的锚点也会随实现漂移 ⇒ **锚点找不到必须算「取证失败」，不能算「转红」**
> （跑者已按三态判定，`✗` 行就是这种情况）。

> ⚠️ **变异取证的写法纪律（本批踩过的坑，写下来防止重犯）**：
> 「守卫会红」不等于「守卫在测你想测的东西」。三次被自己的变异骗过：
> 1. **M32**：只删掉 `if "truncated" not in parsed:` 那一行，`raise` 还在 ⇒ 守卫照常抛错 ⇒ 用例仍绿
>    ⇒ 变异必须把**整块 if/raise** 一起换掉（改成「缺失时补 `False` 放行」）。
> 2. **M34**：载荷是**扁平**的（只有 `a.py`）⇒ 新的「响应含带 `/` 的路径」不变式**先**报错
>    ⇒ 未知类型那条根本没被执行到 ⇒ 变异不转红。
>    ⇒ **载荷必须带嵌套路径**，才能保证「期望的那条错误」是唯一可能的来源。
> 3. **退出码 ≠ 1 不是转红**：pytest 的 2/3/4/5 分别是「收尾出错 / 内部错误 / 用法错 /
>    **没收集到用例**」⇒ 若把它们当「转红」，则**目标用例文件没 `git add`** 也会「转红」
>    ⇒ **假取证**。⇒ `run_case` 必须是**三态**（M54）。
>    ⇒ 且退出码 1 时还要确认**输出里有该用例名**（否则是「别的用例失败」）。

### 4.3 真实 API 取证

- Trees API 接受 **commit sha**（不必先查 tree sha）：`GET /repos/{o}/{r}/git/trees/{sha}?recursive=1`。
- 本仓规模可用：**2946** 条目、`truncated=false`、~235KB；**368** 个目录条目全部有子条目。
- `pull_request` run 的 `head_sha` = **PR 分支头 commit**（**不是** run 实际执行的 merge commit）
  ⇒ 实测 PR #111：run `42281369007` 的 `head_sha` = `1d226997…`，而 job 日志写着
  `HEAD is now at 23db713 Merge 1d226997… into 87f8e8e6…` ⇒ **run 跑的是合并 commit**。
- run 对象里的 `pull_requests` 数组**实测是空的** ⇒ PR 号只能靠
  `GET /repos/{o}/{r}/commits/{prHeadSha}/pulls` 反查（第二版用过；第三版不再需要）。
- ⚠️ **`pulls/{n}.merge_commit_sha` 是 `refs/pull/N/merge` 的「当前值」**，不是历史 run
  当初用的那个 ⇒ main 一推进就漂移；PR 合并后 **404** ⇒ **回溯历史 run 不可实现**
  ⇒ 这就是第三版放弃「直接取那棵树」的原因（§1.3）。
- ⚠️ `run_started_at` 与 `created_at` **不是一回事**，且**哪个才是 `M_T` 的上界**取决于
  「合并 commit 什么时候被冻结」。**实测（PR #105 run `41908498247`，attempt=2）**：
  `created_at=2026-11-04T20:38:19Z`、`run_started_at=2026-11-04T22:24:11Z`；
  合并 commit `57a267a9` 的 `committer.date=20:38:18Z`、第一父 = `83581e10`；
  attempt-2 的 job 日志写着 `git fetch … +57a267a93f…:refs/remotes/pull/105/merge`
  ⇒ **合并 commit 在派发时冻结、复跑不动它** ⇒ 上界必须用 **`created_at`**（§2.7.1）。
  ⚠️ 早期版本据「复跑时 `created_at` 不变」推出「该用 `run_started_at`」——**那个推理错了**：
  真正被冻结的是**合并 commit**，不是 `created_at`；锚点要跟**合并 commit 的底座**对齐，
  而不是跟「哪次 attempt 开始跑」对齐。
- `runs?branch=` **不区分事件** ⇒ 基线查询必须自己加 `event=push`（工具侧独立复核）。

## 5. 明确未做的事

- ⚠️ **判定集合是「路径级」的**：`A ∪ P` 与「基线树 vs 实际执行树」在**路径层**完全一致，
  但**内容层**可能不同（PR 若恰好把 main 的某个改动回退掉，那条路径在真实差异里可能「相同」）
  ⇒ 判定集合是该差异的**超集** ⇒ 偏保守（可能多拦一次，**不会漏判**）。
- ⚠️ **没有**逐字核对 `M_T` 与 run 当时用的合并底座是否**同一个 commit** ——
  那个 commit 已被 GitHub 回收（§1.3）⇒ 只能用「不晚于上界的**最新** main push run」作代理
  ⇒ 若 main 在 PR run 的那 120 秒宽限内又推了一次，`M_T` 会**偏新** ⇒ `A` 偏大 ⇒ 偏保守。
- ⚠️ **`M_T` 取两条腿，各自都可能偏旧（2026-11-07 安全评审 HIGH-2）**：
  - **run 腿**（main 的 push run 列表）：main 的那次 push **没触发** workflow、
    或它的 run **创建得太晚**（排队 > 宽限）⇒ 偏旧 ⇒ `A` 漏算 ⇒ fail-open；
  - **commit-date 腿**（commits API）：commit 日期 ≠ push 时刻（rebase / 时钟偏斜）⇒ 也可能偏旧。
  ⇒ `A` 取**两条腿的并集** ⇒ 谁漏掉的那一段，另一条兜住。
  ⚠️ 两条腿都偏旧**且偏到同一处**时仍会漏 ⇒ 残留窗口。
- ⚠️ **前端 CI 面不在本批的判定范围内**（明确裁决，不是遗漏）：
  `ci.yml` 的 `Frontend / typecheck + coverage` 是**非分片 job** ⇒ 本工具**不解析** vitest 的
  失败明细 ⇒ 它在失败集合里只以**一个不透明 ID**（`job:Frontend / typecheck + coverage`）出现。
  ⇒ 「改前端测试配置（`frontend/vitest.config.ts` / `package.json` / `tsconfig.json`）
  ⇒ 哪些用例被收集、覆盖率阈值是多少」**不在**「CI 相关面清单」里 ⇒ 本闸门**不会**因它判退出码 6。
  ⇒ 后果：前端测试配置的改动只会被**失败集合对差**以「job 整体红/绿」的形式体现
  ⇒ 若改配置让前端 job 由红转绿 ⇒ 表现为「**净丢失**」（退出码 5，仍须人工确认）
  ⇒ **不会静默放行**；但若改动让 job 仍红 ⇒ 失败集合不变 ⇒ 判「净新增 0」⇒ **对差看不见它**。
  ⇒ 要根治需要给前端 job 也做「按用例 ID 的失败集合」⇒ 那是**另一个立项**（要写 vitest 失败解析器），
  本批不做。
- **没有**改闸门的**退出码语义**（6 仍是「CI 相关面变了」，3 仍是「运行错误/须复跑」）。
- **没有**放开退出码 6 的人工仲裁流程 ⇒ 退出码 6 仍需 owner 走越权口显式合并
  （见 N7 / N8：个人仓库装不了 users bypass）。
- **没有**实现「分支一律以 main 的 tip 为底座起」的**强制机制**（工具无法强制）
  ⇒ 只写进 `Harness/rules/开发流程规范.md` 作为过程规则。
- **没有**处理 N5（净丢失只能给 `NEEDS_REVIEW`）、N8（收紧 `enforce_admins` 的前置通道）。
- 本批**不部署**（零 DDL、零生产写）。

## 6. 相关条目

- [[qa-system-merge-gate-net-diff]]
- [[qa-system-merge-gate-protection-config]]
- [[qa-system-merge-gate-flake-whitelist]]
- [[qa-system-ci-failure-set-tool]]
