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
| `M_T` | **PR run 事件派发那一刻 main 的 tip**（= 那次合并的 main 侧底座） | 见下面的「`M_T` 怎么取」（**两条腿**：run 腿 + 第一父链腿） |
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

**`M_T` 怎么取**（`ci_gh.fetchMainTipShaAt` + `ci_gh.fetchMainFirstParentTipAt`，**两条腿**）：

1. 上界 = **PR run 的 `created_at`（事件派发时刻）**，**原值、**不加宽限**
   （第五版起 `+120s` 宽限已删除 ⇒ 见 §2.10.1）。
   ⚠️⚠️ **必须用 `created_at` 而不是 `run_started_at`**（2026-11-07 代码评审 CRITICAL-1）：
   `pull_request` 事件的**合并 commit 在事件派发那一刻就被冻结**了
   ⇒ 复跑（re-run）**不会**重新计算它 ⇒ 复跑检出的仍是**同一个**合并 commit
   ⇒ 它的 main 侧底座是**派发那一刻**的 main ⇒ 上界必须锚**派发时刻**。
   （早期版本误判「复跑会在新时刻重新检出 ⇒ 该用 `run_started_at`」——
   **实测推翻了这个前提**，见 §2.7.1。）
   ⚠️ **`created_at` 缺失 ⇒ 判定不出来（fail-closed）**，**不回退 `run_started_at`**
   （第五版；闸门侧同样 fail-closed ⇒ 两边上界必须一致 ⇒ 见 §2.10.1）。
2. 取 **main 的 push run 列表**（`GET /repos/{o}/{r}/actions/runs?branch=main&event=push`），
   逐条过**三条硬条件**（缺一不可）：
   - `event == "push"`（查询串可能被服务端忽略 ⇒ 工具侧独立复核）；
   - `head_branch == "main"`；
   - `head_repository == 本仓库`（**大小写不敏感**比较——GitHub 仓库名大小写不敏感，
     严格相等会把**合法基线**判成不可信 ⇒ 误判退出码 3 ⇒ 白拦）。
3. 再过时间窗：`created_at <= cutoff` ⇒ 取**最新**的一条的 `head_sha`。
4. 一条都不合格 ⇒ **报错**（判定不出来）⇒ **不许随便返回一个 sha**。
5. **第一父链腿**（第五版新增，`fetchMainFirstParentTipAt`）：从 **main 的当前 tip** 出发，
   沿 **`parents[0]`** 回溯，直到 commit 的 committer 日期 ≤ 上界 ⇒ 得到 main **主线**
   在该时刻的 tip ⇒ **侧提交永远不在主线**上 ⇒ 天然排除「侧提交日期早于上界、
   但它进 main 的时刻在上界之后」这类假拦。⚠️ **不许用 `commits?sha=main&until=<上界>`**
   —— 它返回的可能是**侧提交** ⇒ 它的 diff 不是 main 的推进 ⇒ **假拦 + 归因错误**。
6. `A` 取**两条腿的并集**（第五版；并集只会**偏大**不会偏小 ⇒ 方向是「假拦」而非 fail-open）。

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

⚠️ **`mainTipCutoff` 的时间戳必须 keyword-only 传**：
这次的缺陷正是「**位置传参 + 锚点写反**」⇒ 两个时间戳**都是合法值**
⇒ 传反了**不会报错** ⇒ 判定静默用错锚点。
⇒ 第五版后签名是 **`mainTipCutoff(*, createdAt: str)`**（**只剩一个**参数）
⇒ 顺序再也没法搞错（守卫：`test_mainTipCutoff_requires_explicitly_named_timestamps`）；
⇒ 传旧的 `runStartedAt=` ⇒ **直接 `TypeError`**（不许悄悄忽略）。

⚠️⚠️ **`created_at` 缺失 ⇒ 判定不出来（fail-closed）**，**不再回退 `run_started_at`**
（第五版，代码评审 MEDIUM-3）：闸门侧**没有**这个回退（`PR_RUN_CREATED_AT` 为空 ⇒
直接 fail-closed）⇒ 工具侧若回退 ⇒ 两边上界不同 ⇒ 基线树 ≠ 执行树 ⇒ **fail-open**。
⇒ **两边必须同时 fail-closed**。

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
  ⇒ 报告里必须**提示 + 给出处置办法**：「**推一个新 commit 到 PR 分支**」
  （或 rebase 到最新 main / 重开 PR）以产生一次**新的 CI 事件派发」。
- ⚠️⚠️ **不许提示「重新运行 PR 的 CI」**（第五版，代码评审 HIGH-1）：
  `pull_request` 事件的**合并 commit 在派发那一刻就被冻结**了
  ⇒ 复跑（re-run）**不会**重新计算它 ⇒ 复跑检出的仍是**同一个**合并 commit
  ⇒ 判定依旧不成立 ⇒ 照着这句话做会**白跑一轮**还拿不到新结论。
  ⇒ 反向守卫：`test_the_report_does_not_advise_a_bare_rerun`。
- ⚠️ **事实与处置必须分成两行**（第五版）：`ci_failure_set._mainTipMismatchNote`（事实）
  与 `_mainTipRemedyNote`（处置）各占一条 note。理由：报告的每个字段都被
  `sanitizeForReport` 截到 **200 字符** ⇒ 事实 + 处置塞进同一个字符串 ⇒ 处置被截掉
  ⇒ 操作者只看到「拦」⇒ 看不到该做什么 ⇒ 白跑一轮。
  ⇒ 同理，**底座不一致的事实不许拼进 reason 字符串**（`reason` 本身也被截到 200 字符
  ⇒ 拼进去必然把「⇒ 对差不成立」截掉 ⇒ 反而更难读）。
- ⚠️ 这条提示**只在判出 CI 相关面变更时**出现（第三版里 `M_base ≠ M_T` 是**常态**
  —— main 一直在推进 ⇒ 无条件提示就是噪音 ⇒ 提示一旦变成噪音就会被无视 ⇒ 提示就失效）。
  ⇒ 反向守卫：`M_base == M_T` 时**不得**出现该提示。

### 2.9.1 ⚠️ 第五版：祖先关系核对（`M_base` 必须是 `M_T` 的祖先）

`ci_surface._baselineAncestorError`：**`merge_base(M_base, M_T) == M_base`** 才算通过。

- **为什么必须查**（安全评审 HIGH-2）：基线 run 挑的是「派发之前完成的 main push run」
  ⇒ 正常情况下它的 commit 一定在 main 主线上、且早于底座 ⇒ 是底座的祖先。
  ⇒ **不是**祖先只有两种可能，两种都致命：
  ① **main 被 force-push / 改写** ⇒ 基线树与执行树分属两条历史 ⇒ 不可比；
  ② 基线挑错了（挑到派发**之后**才创建的 run）⇒ `A` 腿算出「main 回退了」
  ⇒ 漏掉 main 的真实推进 ⇒ **fail-open**。
- ⇒ 判不出 ⇒ **fail-closed（退出码 3：基线不可信 ⇒ 须复跑）**，不许「算个差集凑合」。
- 守卫：`test_resolveCiSurface_fails_closed_when_the_base_is_not_an_ancestor`。

### 2.10 ⚠️ 第五版（2026-11-07 第二轮评审后的修订）

> 第四版之后又跑了一轮 `code-reviewer`（WARNING）与 `security-reviewer`（**BLOCK**）。
> 本节记录那一轮关掉的洞。

#### 2.10.1 上界**不加宽限**、且**不回退**（代码评审 MEDIUM-1 / MEDIUM-3）

- **旧**：工具侧上界 = `created_at + 120s`（`_MAIN_TIP_GRACE_SECONDS`），
  且 `created_at` 缺失时**回退** `run_started_at`。
- **问题**：
  ① 闸门侧（`merge-gate.yml`）取基线用的上界是 `PR_RUN_CREATED_AT`（**无宽限**）
  ⇒ 工具侧多给 2 分钟 ⇒ 工具侧的 `M_T` 比闸门侧的基线**更新**
  ⇒ `A` 腿把「派发之后 main 才推进的 CI 改动」也算进来 ⇒ **判定集偏大 ⇒ 假拦**
  （main 在 PR 派发后 2 分钟内推进过就中招）；
  ② 闸门侧**没有**回退 ⇒ 工具侧回退 ⇒ 两边上界不同 ⇒ 基线树 ≠ 执行树 ⇒ **fail-open**。
- **现在**：工具侧上界 = `created_at` **原值**；`created_at` 缺失 ⇒ **fail-closed**。
  ⇒ **两边必须同一个上界、同一个比较符、同一种失败方式。**

#### 2.10.2 `M_T` 的**第一父链腿**（安全评审 HIGH-1）

- **旧**：只有 run 腿（main 的 push run 列表）。main 的那次 push **没触发** workflow、
  或它的 run **创建得太晚** ⇒ run 腿偏旧 ⇒ `A` 漏算 main 的推进 ⇒ **fail-open**。
- **现在**：`fetchMainFirstParentTipAt` —— 从 **main 的当前 tip** 出发，沿 **`parents[0]`**
  回溯，直到 commit 的 committer 日期 ≤ 上界。
  ⇒ 得到的必然是 **main 主线**在该时刻的 tip ⇒ **侧提交永远不在主线**上。
- ⚠️ **不许用 `commits?sha=main&until=<上界>`**（第四版曾用它，叫「commit-date 腿」）：
  它返回的是「从 main 可达、日期 ≤ 上界的最新提交」⇒ **可能是侧提交**
  ⇒ 它的 diff 不是 main 的推进 ⇒ **假拦 + 归因错误**。
- ⚠️ 回溯跳数有上限（`_MAX_FIRST_PARENT_HOPS`）⇒ 超限**报错**（fail-closed）。
- ⚠️ 历史条目畸形（日期 / sha / parents 形态异常）⇒ **报错**，不静默跳过
  ⇒ 静默跳过 = 那条提交从历史里消失 ⇒ 回溯链断掉 ⇒ 拿到的 tip 是错的 ⇒ **fail-open**。
- **`A` = 两条腿的**并集** ⇒ 谁漏掉的那一段，另一条兜住
  ⇒ 并集只会**偏大**不会偏小 ⇒ 方向是「假拦」而不是 fail-open。

#### 2.10.3 祖先关系核对（安全评审 HIGH-2）

见 §2.9.1。

#### 2.10.4 ⚠️ 前端 CI 面纳入判定（安全评审 **CRITICAL-1**）

- **问题**：`_CI_SURFACE_PATTERNS` 里**没有任何前端路径** ⇒ 一个 PR 可以把
  `frontend/vitest.config.ts` / `package.json` / `package-lock.json` / `tsconfig.json` /
  `src/setupTests.ts` 全部改掉，而**前端 job 仍然进入失败集合对差**
  （它以 `job:Frontend / typecheck + coverage` 这个不透明 ID 出现）
  ⇒ 前端 CI 的口径（收集哪些用例、覆盖率阈值）被换掉 ⇒ 失败集合不可比
  ⇒ **且 CODEOWNERS 没有 `frontend/` 条目 ⇒ 没人复核 ⇒ 完整绕过链**。
- **现在**：
  ① `_CI_SURFACE_PATTERNS` 增前端配置路径；
  ② `.github/CODEOWNERS` 增 `frontend/` 前缀条目（含 `frontend/vite.config.ts`
     与 `frontend/vite.config.js`——后者是**被提交进仓库的编译产物**，
     但 vite 的配置解析顺序里 `.js` **优先于** `.ts` ⇒ 它随时可能被真正加载
     ⇒ 改它 = 改前端的构建口径）；
  ③ 新增**反向守卫** `backend/app/tests/unit/test_ci_surface_frontend_reverse.py`
     （它自己也必须有 CODEOWNER ⇒ 否则 `test_ci_codeowners.py` 的反向守卫会红）。

#### 2.10.5 变异取证的失败标记必须**属于这条用例**（代码评审 MEDIUM-4）

- **旧**：退出码为 1 时只判「输出里含 `case` 字符串」。
- **问题**：① 文件级用例只判「输出里含文件名」⇒ 那是 pytest 的**路径前缀**，
  **任意**上下文（收集进度行、warning）都满足 ⇒ 别的用例失败会被算成「这条守卫转红」
  ⇒ **假取证**；② 用例级只判「含用例名」⇒ `test_x` 是 `test_x_extra` 的**子串**
  ⇒ 同名兄弟用例失败会被算成命中 ⇒ 同样是假取证。
- **现在**：要求 pytest 的**短摘要行**里出现 `FAILED <nodeid>` / `ERROR <nodeid>`
  （**整行匹配**：nodeid 后面必须跟空白或行尾 ⇒ 不能是更长 nodeid 的前缀），
  文件级则要求 `FAILED <file>::…`。

#### 2.10.6 其它小修

- workflow 侧的 jq **独立复核** `head_branch == "main"` / `status == "completed"`
  ⇒ 不信任服务端已过滤（新增守卫 `test_merge_gate_base_run_query_rechecks_the_run_fields_in_jq`）。
- 报告与文档里的「开跑时刻」措辞统一改成「**事件派发时刻**」（代码评审 MEDIUM-5）。
  ⚠️ 第三轮评审 L-1 复核发现三处残留（`test_ci_merge_gate_workflow.py` 两条 docstring、
  `ci_mutation_cases.py` 批注）⇒ 第六版已一并收口。

#### 2.10.7 ⚠️ vitest workspace 自动发现文件（第三轮安全评审 **C-1**，2026-11-08）

- **问题**：vitest 2.x 在配置目录根**自动探测** `vitest.workspace.*` / `vitest.projects.*`
  （扩展名 ts/mts/cts/js/mjs/cjs/json；`WORKSPACES_NAMES` 实测于
  `node_modules/vitest/dist/chunks/constants.*.js`）。存在时 `resolveWorkspace`
  **不再走**根 `vitest.config.ts` 的项目解析 ⇒ project 配置里的
  `test.exclude` 能把失败用例**整个排除掉**。
  ⇒ PR 作者只新增**一个**文件 `frontend/vitest.workspace.ts`（不碰任何现有文件）
  ⇒ 前端 job 假绿 ⇒ 失败集合与基线相同 ⇒ 假「净新增 0」
  ⇒ 而这批名字**既不符合** `*.config.*` 也**不符合** `tsconfig*` ⇒
  ② 第五版的四条前端模式全 MISS；③ CODEOWNERS 无条目；④ 反查守卫的
  `_CONFIG_NAME_PATTERN` 同样看不见 ⇒ **第五版堵住的同一条绕过链换了个文件名又通了**。
- **现在**（四处，缺一不可）：
  ① `_CI_SURFACE_PATTERNS` 增
     `frontend/(?:.*/)?vitest\.(?:workspace|projects)\.(?:ts|mts|cts|js|mjs|cjs|json)$`（任意深度）；
  ② `.github/CODEOWNERS` 增 `frontend/vitest.workspace.*` / `frontend/vitest.projects.*`
     两条**通配**条目 —— 仓库里**现在没有**这类文件 ⇒ 通配管的是**未来文件**；
  ③ 反查守卫增 `_WORKSPACE_NAME_PATTERN`（扫描器认得出 workspace 名字形态）+
     `test_the_vitest_workspace_entries_are_covered_by_codeowners`
     （对**还不存在的名字**直接断言 `_effectiveOwner` ⇒ 有人删通配条目立刻红）
     ⇒ 为此前端 CODEOWNERS 守卫从「路径字面出现过」升级为**生效归属**判定
     （复用 `test_ci_codeowners._effectiveOwner`）；
  ④ 变异取证新增 M66（删清单条目）/ M67（扫描器认不出）。
- **同批顺带收口**（第三轮评审 L-1 / L-2，防御纵深）：
  `*.tsbuildinfo`（composite 项目的增量编译产物 ⇒ 决定哪些文件要重新类型检查）
  与 `frontend/.npmrc`（npm 安装行为）也进清单与 CODEOWNERS。

### 2.6 抖动豁免的前置条件也改用判定集合

豁免的「该用例所在文件**没被改过**」这个前置条件，若只用三点归因集
⇒ main 侧改过的测试文件会被当成「没改过」⇒ **对称豁免吃掉真回归**。
⇒ 前置条件同样用**判定集合**（`A ∪ P`）。

#### 2.10.8 ⚠️ npm-shrinkwrap 优先于 lockfile（第四轮代码评审 **HIGH-1**，2026-11-08）

- **洞**：npm 的 arborist 取依赖数据用 `sw || lock` ⇒ **有 `npm-shrinkwrap.json` 则
  `package-lock.json` 整个被忽略**（npm 11 官方文档同义）。⇒ PR 只新增**一个**
  `frontend/npm-shrinkwrap.json`（不在 .gitignore ⇒ 能提交）钉住 `^2.0.0` 范围内的旧
  vitest/jsdom ⇒ 前端 job 换了依赖树跑 ⇒ 失败集合与基线不同却判「净新增 0」。
  实测：`detectCiSurfaceChanges(("frontend/npm-shrinkwrap.json",))` 返回空集、
  反查扫描器不认、`_effectiveOwner` 为 `None` ⇒ **四层全 MISS**（与 C-1 同构的绕过链）。
- **修法**（四处，同 C-1 结构）：① 清单 lockfile 交替组加 `npm-shrinkwrap\.json`；
  ② CODEOWNERS 增 `frontend/npm-shrinkwrap.json`；③ 反查守卫 `_LOCK_NAMES` 加名；
  ④ 变异 **M68**（删清单条目 ⇒ `test_the_frontend_ci_files_are_actually_flagged`
  正面断言 10→11 转红）。
- **同批顺带收口**（第四轮 LOW-2/LOW-3）：跑者 docstring 的「58 组」过期引用改成 68 组
  并点名两个数据文件；`test_ci_codeowners.py` 的 `controlledPaths` 补上
  `ci_mutation_cases_core.py`（否则「追加宽泛 pattern 抢走归属」这类改动守卫看不见）。
- **结构性教训**（评审原话采纳）：反查守卫的枚举照抄清单 = C-1 这类洞的**复发机制**
  ⇒ 后续立项把锁文件名从 npm/yarn/pnpm 的真实语义**推导**（而非镜像）。

#### 2.10.9 ⚠️ CI 真正执行/加载的「环境定义杠杆」文件（第五轮安全评审，2026-11-08）

第五轮安全评审（对第四轮修复后的树）判 **BLOCK**：1 HIGH + 2 MEDIUM + 3 LOW，全部实证
（`detectCiSurfaceChanges` 对每个路径都返回空集）。共通形态：**CI 在 unit / integration
两个 job 前真正执行或加载、却不在 CI 相关面的文件** —— 只新增/改写一个文件就能换掉
测试环境 ⇒ 失败集合不可比 ⇒ 却判「净新增 0」。

| 级别 | 洞 | 机制 | 修法 |
|---|---|---|---|
| HIGH-1 | `backend/alembic/env.py` | CI 两个 job 前都跑 `alembic upgrade head`，env.py 是**真正建 engine、跑迁移**的任意 Python：可在 `run_migrations()` 后追加 DDL 塑形测试库（pytest 的 `_ensureSchema` 还会再执行一次），甚至能在 pytest 收集前改写盘上的测试文件。只有 `alembic.ini` 在清单 ⇒ 完整绕过链 | 清单 + CODEOWNERS + 正面断言 + **M70** |
| MEDIUM-2 | `backend/.env` | conftest `load_dotenv(override=True)` ⇒ 被 `git add -f` 塞进来的 `.env` 覆盖 CI 注入的全部变量（`DATABASE_URL` / `TEST_DATABASE_URL` / `MILVUS_TEST_DB_NAME` / `MINIO_*`…）；`APP_ENV` / `SECRET_KEY` / `SESSION_BUDGET` 是 load **之后**才 `setdefault` ⇒ 文件也赢 | 清单（`.env` 家族，`.env.example` 一并认）+ CODEOWNERS + 正面断言 + **M71** |
| MEDIUM-3 | `backend/.python-version` / `uv.toml` | uv 按项目内这两个文件选解释器（实测 0.11.16：`>=3.12,<3.15` + `.python-version` 3.12 选中 CPython 3.12.14，盖过 setup-python 的 3.13）⇒ 只新增一个文件就把两个 pytest job 换到 3.12 + 按 3.12 解析的 marker 依赖 | 清单 + CODEOWNERS + 正面断言 + **M72** |
| LOW-4 | `frontend/.env` / `.env.test` | vitest 以 mode `test` 解析 Vite 配置 ⇒ 加载这两个文件；现在唯一被读的是 `VITE_API_BASE_URL` ⇒ 影响潜伏，提前入清单 | 清单 + CODEOWNERS 通配 + 反查扫描器 `_ENV_NAME_PATTERN` + 正面断言（11→13）+ **M73/M74** |
| LOW-5 | `backend/app/tests/_neo4j_support.py` / `_testapp.py` / `backend/scripts/seed_*.py` | 与已入清单的 `_pg_support` / `milvus_isolation` 同族：autouse 夹具逐个 import ⇒ 换成假的 = 换掉测试库初始状态与 registry ⇒ PR 引入的失败不出现。且这批**此前连 CODEOWNER 都没有**（`_pg_support` 也是） | supports 行扩名 + seed 通配入清单 + CODEOWNERS 补齐（含 `_pg_support`/`milvus_isolation` 补授权）+ 正面断言 + **M75/M76** |
| LOW-6 | `ci_mutation_cases_core.py` 无正向用例 | M-3 拆分后清单正则里的 `|cases_core` 没有任何用例钉住（从 enum 删掉它不会红）。不可静默利用（改 ci_surface.py 本身就是 CI 面 + 有 CODEOWNER），但取证缺口要补 | 反向真实文件守卫的枚举加 `cases_core` + **M69** |

- **变异总数**：68 → **76**（M69–M76 新批，§4.2.5）。锚点核对 76/76 齐全
  （M23 的 `trap` 锚点在 ci.yml 本就双 job 同文、occurrence 1 = unit 侧，
  属「第 occurrence 次存在」口径的合法形态，非 M26 那类歧义）。
- **结构性教训**（与 §2.10.8 同款）：反查守卫认不出「仓库里现在没有的文件」
  ⇒ 这批按路径**正面**钉住（`test_the_round5_environment_lever_files_are_actually_flagged`
  一次断言 10 个路径），每个 pattern 行都有对应变异组证明会红。

#### 2.10.10 ⚠️ 同类杠杆的延续：迁移脚本本体 / 仓库根 uv.toml / backend 根 seed / kpi 守卫模块（第六轮复审，2026-11-08）

第六轮复审（对第五轮修复后的树，585 行 diff）判 **BLOCK**：1 HIGH + 2 MEDIUM + 1 LOW，
全部实证（`detectCiSurfaceChanges` MISS + `_effectiveOwner` = None）。共通形态仍是
**「CI 在收集/执行前真正跑到的文件」**，只是第五轮漏掉了同机制的四处延续：

| 级别 | 洞 | 机制 | 修法 |
|---|---|---|---|
| HIGH-1 | `backend/alembic/versions/*.py`（140 个迁移脚本） | `alembic upgrade head` 执行每个 revision 的 `upgrade()` —— pytest 收集前的任意 Python；工程规则要求**每个 ORM 变更都带迁移** ⇒ 这是**常规路径**而非边缘。方向代价（刻意接受）：迁移文件入清单 ⇒ main 侧合了迁移、PR 没跟上也会判退出码 6（**假拦**）—— 本设计明确偏好假拦而不是 fail-open | 清单通配 + CODEOWNERS 通配（管未来文件）+ 正面断言（10→18）+ **M77** |
| MEDIUM-1 | 仓库根 `uv.toml` | uv 沿工作目录**父链**发现配置：CI 在 `backend/` 跑 uv ⇒ 根级 `uv.toml` 也被读（**父级 `.python-version` 实测不被认**，故只收 uv.toml） | 清单 + CODEOWNERS（`/uv.toml`）+ 正面断言 + **M78** |
| MEDIUM-2 | `backend/seed_ontology.py` / `seed_models.py` | unit/conftest.py:16 **模块级** `import seed_ontology` ⇒ 收集前执行；第五轮的 `backend/scripts/seed_*.py` 通配管不到 `backend/` 根的同族 | seed pattern 扩成 `^backend/(?:scripts/)?seed_[\w.-]+\.py$` + CODEOWNERS `backend/seed_*.py` + 正面断言 + **M79**（回退到窄形态会红） |
| LOW-1 | `backend/scripts/kpi_metric_db_guard.py` | `_pg_support.assertTestDatabase` import 它的 `databaseNameOf` —— 改它 = 改「测试必须连哪个库」的守卫口径 | 清单 + CODEOWNERS + 正面断言 + **M80** |

- **M76 重锚**：第五轮的 M76 锚点是窄 seed 行，本批把该行扩宽 ⇒ M76 的 old 字符串
  同步改成宽行原文（否则锚点找不到 ⇒ 取证失败）；其语义不变（删除 seed 条目仍由
  正面断言转红）。
- **变异总数**：76 → **80**（M77–M80 新批）。锚点核对 **80/80** 齐全；新增 5 组
  （M76–M80，含重锚后的 M76）solo 跑 **5/5 转红、净漏 0**；终跑 80 组净漏 0（§4.2.6）。
- **结构性教训**（第三度同款）：「环境定义杠杆」的边界要按**执行链**画，不能按目录画 ——
  第五轮按 `backend/scripts/` 收 seed，却漏了 conftest 模块级 import 的 `backend/` 根同族；
  按 `alembic.ini`/`env.py` 收了配置与跑器，却漏了被 `upgrade head` 逐个执行的脚本本体。

## 3. 变更清单

| 文件 | 动作 |
|---|---|
| `backend/app/utils/ci_gh.py` | 重写：新增 Trees API 链路（`fetchTreePathShas` / `_parseTreePathShas` / `_buildTreeFileMap` / `_assertTreeIsRecursive` / `_diffTreePathShas` / `fetchNetChangedFiles`）、`fetchMergeBaseSha` / `_parseMergeBaseSha`；`fetchChangedFiles` 改为「merge-base 树 vs head 树」；**删除** `_parseChangedFiles` 与 300 项上限逻辑；所有外部输入加形态校验（repo / run id / sha / branch）。**第三版**：`RunHeadInfo` 增 `event` / `createdAt` / `runStartedAt` 三字段；新增 `mainTipCutoff` / `fetchMainTipShaAt` / `_parseMainTipSha` / `_considerMainRun`（`M_T` 的取数链路）与 `_MAIN_TIP_GRACE_SECONDS = 120`。**第四版（CRITICAL-1）**：`mainTipCutoff` 的**锚点从 `run_started_at` 换成 `created_at`（事件派发时刻）**、参数改成 **keyword-only**（`createdAt=` / `runStartedAt=`）⇒ 杜绝「位置传参写反」；`created_at` 缺失才回退 `run_started_at`，两者皆缺 ⇒ 抛错（判定不出来）；**删除**第二版整条「测试合并 commit」链路（`PullMergeInfo` / `fetchPullMergeInfo` / `_findOpenPullNumber` / `_parseOpenPullNumbers` / `_fetchPullMergeCommitSha` / `_fetchCommitTreeAndParent`）——`refs/pull/N/merge` 是临时 ref，**回溯历史 run 时取不到**（§1.3）。**第五版**：**删除** `_MAIN_TIP_GRACE_SECONDS`（宽限 ⇒ 与闸门侧不对称 ⇒ 假拦）；`mainTipCutoff` **只接 `createdAt`**（**删掉** `runStartedAt` ⇒ 缺失即 fail-closed，不回退）；新增 `fetchMainFirstParentTipAt` / `_parseCommitEntries`（**第一父链腿**）+ `_MAX_FIRST_PARENT_HOPS`（超限报错） |
| `backend/app/utils/ci_surface.py` | `CiSurfaceInfo` 增 `netChangedFiles` / `unattributedCiFiles` / `baseTrustError` / `mainAdvanceFiles` / `mainTipSha`；新增 `_baseTrustError`（三条硬条件）。**第三版**：判定集合改为 `A ∪ P`（`_buildSurface` 里 `judgment = set(mainAdvance) | set(prChanged)`）；新增 `_fetchMainTipSha` / `_fetchMainAdvanceFiles` / `_fetchPrChangedFiles`；**删除**第二版的 `_fetchMergeInfo` / `_fetchAttributionFiles` / `_fetchComparedFiles` 与 `prMergeSha` / `prMergeBaseSha` 两字段。**第四版（CRITICAL-1）**：`_fetchMainTips` 的 cutoff 改用 `mainTipCutoff(createdAt=prInfo.createdAt)`（**具名传**）⇒ 锚点与工具侧一致。**第五版**：`_CI_SURFACE_PATTERNS` 增**前端 CI 定义**路径（package / lock / `*.config.*` / `tsconfig*.json` / setup 文件）⇒ 堵住「前端绕过链」；新增 `_baselineAncestorError`（祖先核对 ⇒ 否则 fail-closed 退出码 3）与 `_fetchMainTips` 的**两腿并集**（run 腿 + 第一父链腿）。**第六版（评审 C-1）**：`_CI_SURFACE_PATTERNS` 再增 `vitest.workspace.*` / `vitest.projects.*`（vitest 2.x **自动发现**、存在时取代根配置的项目解析）+ `*.tsbuildinfo` + `frontend/.npmrc`（§2.10.7）；**第四轮评审（HIGH-1）**：lockfile 交替组加 `npm-shrinkwrap.json`（npm 语义下**优先于** package-lock ⇒ 只新增一个文件就能换掉前端依赖树）（§2.10.8）。**第五轮安全评审（§2.10.9）**：增 `backend/alembic/env.py`（CI 真正跑迁移的任意 Python）、`backend/.env` 家族（conftest `override=True` 覆盖 CI 注入变量）、`backend/.python-version` / `uv.toml`（uv 选解释器盖过 setup-python）、`frontend/.env` 家族、`backend/scripts/seed_*.py` 通配；supports 行扩为 `_pg_support|_neo4j_support|_testapp|milvus_isolation`。**第六轮复审（§2.10.10）**：增 `backend/alembic/versions/*.py` 通配（`upgrade head` 逐个执行的脚本本体）、仓库根 `uv\.toml`（uv 父链发现）、seed pattern 扩成 `backend/(?:scripts/)?seed_…`（conftest 模块级 import 的 `backend/` 根同族）、`backend/scripts/kpi_metric_db_guard\.py`（`assertTestDatabase` 的依赖） |
| `backend/app/utils/ci_failure_set.py` | 判定集合改为 `detectCiSurfaceChanges(surface.netChangedFiles)`；`_ciSurfaceReason` 分「PR 改了」/「归因不到本 PR」两种措辞；豁免前置条件改用 `netChangedFiles`；基线不可信 ⇒ 退出码 3；报告 notes 增「归因不到本 PR」条目。**第三版**：`_mergeBaseMismatchNote` 改用 `surface.mainTipSha`（不再是 `prMergeBaseSha`）。**第五版**：底座不一致的**事实与处置拆成两条 note**（`_mainTipMismatchNote` / `_mainTipRemedyNote`）⇒ 处置不被 200 字符截断；**处置改为「推一个新 commit 到 PR 分支」**（不复跑 ⇒ 复跑无效，见 §2.9）；底座不一致的**事实不再拼进 reason 字符串** |
| `.github/workflows/merge-gate.yml` | 基线 run 查询限定 `event=push` + jq 复核 `.event == "push"`。**第二版**：基线取法加**时间上界** —— 只认 `created_at <= PR run 的时间戳` 的 main push run（用**独立 jq 二进制**的 `--arg` 传时间戳；`gh --jq` 不支持 `--arg`）；**`PR_RUN_CREATED_AT` 为空 ⇒ 显式 fail-closed**。**第三版**：时间戳取 `run_started_at`。**第四版（CRITICAL-1）**：改取 **`created_at`（事件派发时刻）** —— `PR_RUN_CREATED_AT: ${{ github.event.workflow_run.created_at }}`（合并 commit 在派发时冻结 ⇒ 复跑不动它 ⇒ 上界必须锚派发时刻，详见 §2.7.1） |
| `.github/CODEOWNERS` | 新增 `backend/app/tests/unit/test_ci_main_tip_note.py` / `test_ci_main_tip.py` / `test_ci_mutation_evidence.py` / `test_ci_failure_set_surface.py` 与 `backend/scripts/ci_mutation_cases.py` 的 CODEOWNER（新增守卫文件**必须**有 CODEOWNER ⇒ 否则 `test_ci_codeowners.py` 的反向守卫会红 ⇒ 缺口本身会挡住合并）。**第五版**：新增 **`frontend/` 前缀条目**（`frontend/vite.config.ts` / `frontend/vite.config.js` 等）⇒ 堵住「前端 CI 定义改了没人复核」的绕过链（安全评审 CRITICAL-1）；并给新守卫 `backend/app/tests/unit/test_ci_surface_frontend_reverse.py` 加 CODEOWNER。**第六版（评审 C-1）**：增 `frontend/vitest.workspace.*` / `frontend/vitest.projects.*` **通配**条目（管**未来文件**）与 `frontend/tsconfig.node.tsbuildinfo` / `frontend/.npmrc`（§2.10.7）；**第四轮评审（HIGH-1）**：增 `frontend/npm-shrinkwrap.json`（§2.10.8）。**第五轮安全评审（§2.10.9）**：增 `backend/alembic/env.py`、`backend/.env` / `backend/.env.*`、`backend/.python-version` / `backend/uv.toml`、`frontend/.env` / `frontend/.env.*`、`backend/app/tests/_pg_support.py` / `_neo4j_support.py` / `_testapp.py` / `milvus_isolation.py` 与 `backend/scripts/seed_*.py`（这批此前**无授权落点**）。**第六轮复审（§2.10.10）**：增 `backend/alembic/versions/*.py`（通配管未来迁移）、`/uv.toml`、`backend/seed_*.py`（根级同族）与 `backend/scripts/kpi_metric_db_guard.py` |
| `backend/scripts/ci_mutation_cases_core.py`（**第六版收尾新建**，评审 M-3） | M1–M36（继承批 + 模块拆分批 + workflow 批 + 净变更批）的纯数据；与 `ci_mutation_cases.py` **同族** ⇒ 同进 `ci_surface.py` 清单（enum 增 `cases_core`）与 CODEOWNERS、`test_ci_codeowners.py` 的按名断言 |
| `backend/scripts/ci_mutation_evidence.py` | M27 锚点对齐现状；新增 M29–M36 八组；**第二版新增 M37–M47**；**第三版重锚 M37–M47 并新增 M48–M55**；**第四版重锚 M46/M48/M49/M50/M39 并新增 M56–M58**；**第五版重锚 M48/M49/M50/M57/M58 并新增 M59–M65**（共 **65** 组）。**第六版新增 M66–M67（共 67 组，§2.10.7）**；**第四轮评审新增 M68（共 68 组，§2.10.8）**；**第五轮安全评审新增 M69–M76（共 76 组，§2.10.9）**；**第六轮复审新增 M77–M80（共 80 组，§2.10.10）**。并把 `run_case` 的判据从**布尔**改成**三态**（`red` / `green` / `inconclusive`）——只有 pytest 退出码 **1** 才算「转红」，2/3/4/5 一律判「没跑起来」⇒ 取证失败（不是转红）。**第五版**：新增 `_failureMarkerFound` ⇒ 退出码 1 时**还必须**在输出里找到**这条用例自己的**失败标记（`FAILED <nodeid>` 整行匹配）⇒ 否则「别的用例失败」会被算成「这条守卫转红」（代码评审 MEDIUM-4）。**收尾拆分**：清单（55 组数据）移到 `backend/scripts/ci_mutation_cases.py`，本文件只留**跑者**（改坏 → 跑用例 → 判三态）——拆分前 826 行 ⇒ 超 800 行上限 ⇒ 拆后 159 + 695 行 |
| `backend/scripts/ci_mutation_cases.py` | **收尾新建**：变异的**纯数据**清单（与跑者**同族** ⇒ 同进 CI 相关面 + CODEOWNERS）；**第四版**增到 58 组，**第五版**增到 **65** 组，**第六版**增到 **67** 组（M66/M67，§2.10.7）。**第六版收尾拆分（评审 M-3）**：M1–M36 移到 `ci_mutation_cases_core.py`（`MUTATIONS = MUTATIONS_CORE + [...]`），本文件留 M37 起 ⇒ 拆后 499 + 393 行（拆前 893 行）；拆分不碰任何变异锚点 ⇒ 取证无需重锚。**第四轮评审**：新增 **M68**（§2.10.8，共 **68** 组）。**第五轮安全评审**：新增 **M69–M76**（§2.10.9，共 **76** 组）。**第六轮复审**：M76 锚点同步为扩宽后的 seed 行原文（行本身扩宽 ⇒ 不改则锚点找不到）+ 新增 **M77–M80**（§2.10.10，共 **80** 组） |
| `backend/app/tests/unit/test_ci_gh.py` | 树解析 / merge-base / 三点语义 / 无 300 上限 / 改名 四个小节；树不变式 3 条；`truncated` 的载荷改成**带嵌套路径**。**第三版**：`RunHeadInfo` 增两字段后 `_headInfo` 同步；`test_parseTreePathShas_rejects_a_string_false_truncated_flag` 随文件拆分搬回本文件 |
| `backend/app/tests/unit/test_ci_main_tip.py` | **第三版新建**：`mainTipCutoff`（**第四版**：优先 `created_at` / 回退 `run_started_at` / 两者皆缺则抛错 / **必须 keyword-only 传**）、`_parseMainTipSha`（三条硬条件 + 时间窗 + 分页）、`fetchMainTipShaAt`（查询串 + `--paginate --slurp`）；**第四版新增 PR #105 复跑锚点的离线回放**（证明夹具有鉴别力：错锚给出另一个答案） |
| `backend/app/tests/unit/test_ci_surface.py` | **第三版重写**（初稿 737 行，第五/六版评审后增至 906 行）：判定集合 = `A ∪ P`、`M_T` 的取法与 fail-closed、归因措辞 |
| `backend/app/tests/unit/test_ci_main_tip_note.py` | **第三版新建**：基线 ≠ `M_T` ⇒ 报告里必须有提示**与处置办法**；同底座 ⇒ **不**出现该提示（反向守卫）；「归因不到本 PR」必须如实说 |
| `backend/app/tests/unit/test_ci_mutation_evidence.py` | **第三版新建**：钉住 `run_case` 的**三态**语义（退出码 0/1/2/3/4/5 各自的判定）——防止把「用例没跑起来」冒充成「守卫抓到了变异」|
| `backend/app/tests/unit/test_ci_merge_gate_workflow.py` | 「基线查询只取 push run」1 条；基线取「PR CI 事件派发之前」的 run 1 条；`PR_RUN_CREATED_AT` 暴露给 diff 步骤 1 条；空时间戳 fail-closed 1 条；**第四版**：**必须取 `created_at`、不得取 `run_started_at`** 1 条；**`M_T` 必须由工具独立取** 1 条；**第五版**：jq **独立复核** `head_branch == "main"` / `status == "completed"` / `head_repository` 1 条 |
| `backend/app/tests/unit/test_ci_surface_frontend_reverse.py` | **第五版新建**：前端 CI 定义文件的**反查守卫** —— 从 `frontend/` 的实际文件与 `vitest.config.ts` 声明的 setupFiles 反查 ⇒ 新增配置文件必须入 `_CI_SURFACE_PATTERNS` ⇒ 堵住「换掉前端测试口径 ⇒ 失败集合不可比 ⇒ 假净新增 0」的绕过链（安全评审 CRITICAL-1）。**第六版（评审 C-1）**：扫描器增 `_WORKSPACE_NAME_PATTERN`（vitest 自动发现的 workspace/projects）与 `*.tsbuildinfo` / `.npmrc`；CODEOWNERS 守卫从「路径字面出现过」升级为**生效归属**（复用 `test_ci_codeowners._effectiveOwner`）；新增 3 条（workspace 文件判成 CI 相关面 / 对还不存在的名字断言 CODEOWNERS 通配 / 扫描器非空转） |
| `backend/app/tests/unit/test_ci_head.py` | `RunHeadInfo` 增 `createdAt` / `runStartedAt` 后 `_headInfo` 同步 |
| `backend/app/tests/unit/_ci_gate_support.py` | `stubCiSurface` 增 `netChangedFiles` / `unattributedCiFiles` / `baseTrustError` / `mainAdvanceFiles` / `mainTipSha` 参数 |
| `backend/app/tests/unit/test_ci_failure_set.py` / `test_ci_failure_set_flakes.py` | **收尾拆分**：新增的三条（「归因不到本 PR ⇒ 退出码 6」「基线不可信 ⇒ 退出码 3」「两侧任一侧改过的文件 ⇒ 不豁免」）移到新文件 `test_ci_failure_set_surface.py` ⇒ 两个原文件回到各自的基线行数（1243 / 782，**前者本就超 800 行上限，属既有欠账，本批不使其更糟**）|
| `Harness/rules/开发流程规范.md` | 第 4 条：判定集合表 + 三条硬纪律 + 基线可信性 + 豁免前置条件 + 「分支以 main 的 tip 为底座起」的过程规则。**第五版**：上界**不加宽限**、`mainTipCutoff` 只接 `createdAt`、**第一父链腿**的取法与禁忌、**祖先关系核对**、处置办法改为「推一个新 commit」 |
| `Harness/changes/2026-11-05-defect-priority-plan/plan.md` / `summary.md` | N6 行更新为最终设计 |

**本批零 DDL、零生产写、零生产读。**

## 4. 验收证据

### 4.1 测试

`pytest app/tests/unit -k 'ci_'`：**361 passed / 19 skipped / 0 failed**（19 skipped 全部是
「行为部分需要 bash ≥ 4.4」的本机限制，CI 必跑）。
（第四版基线：337 passed / 19 skipped；第五版新增 **24** 条 ⇒ **净新增失败 0**。）

- 第三版新增用例：`test_ci_surface.py`（重写，34 条）、`test_ci_main_tip.py`（新建，17 条）、
  `test_ci_main_tip_note.py`（新建，4 条）、`test_ci_mutation_evidence.py`（新建，7 条）、
  `test_ci_merge_gate_workflow.py`（+2 条）、`test_ci_codeowners.py`（+1 条）。
- **第五版新增用例**：`test_ci_surface_frontend_reverse.py`（**新建**）、
  `test_ci_main_tip.py`（+5 条：第一父链腿）、`test_ci_surface.py`（+2 条：并集 / 祖先核对）、
  `test_ci_mutation_evidence.py`（+3 条：失败标记必须属于这条用例）、
  `test_ci_merge_gate_workflow.py`（+1 条：jq 独立复核）、
  `test_ci_main_tip_note.py`（+1 条：处置不得是「重新运行 PR 的 CI」）。
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

> ⚠️ **第五版重锚说明**：上表里 M48/M49/M50 的**锚点与目标用例已随第五版过期** ——
> M48 现打「上界不加宽限」（`test_mainTipCutoff_applies_no_grace_window`）、
> M49 现打「不回退 `run_started_at`」（`test_mainTipCutoff_does_not_fall_back_to_run_started_at`）、
> M50 现打「surface 级 fail-closed」（`test_resolveCiSurface_fails_closed_when_the_pr_run_has_no_timestamp`）；
> M57/M58 的锚点同步更新。全量重跑见 §4.2.3。

### 4.2.2 第四版重锚 + 新增 3 组（M56–M58）

| # | 变异 | 期望转红的用例 | 实测 |
|---|---|---|---|
| M56 | workflow 的 `PR_RUN_CREATED_AT` 改取 `run_started_at` | `test_merge_gate_uses_created_at_not_run_started_at` | **转红** ✔ |
| M57 | 工具侧调用点把两个时间戳**传反**（`createdAt=` / `runStartedAt=` 对调） | `test_resolveCiSurface_derives_the_main_tip_cutoff_from_created_at` | **转红** ✔ |
| M58 | 去掉 `mainTipCutoff` 的 **keyword-only**（`*`）⇒ 位置传参又变得可能 | `test_mainTipCutoff_requires_explicitly_named_timestamps` | **转红** ✔ |

⇒ M56/M57 覆盖「**两边锚点必须同时换**」：任一边单独改坏 ⇒ 对应守卫立刻红。
⇒ M58 覆盖「**防止位置传参把锚点写反**」（本次缺陷的真正成因）。

全量重跑 `backend/scripts/ci_mutation_evidence.py`（**58 组**）：**合计 58 组变异，净漏 0**。

### 4.2.3 第五版重锚 + 新增 7 组（M59–M65）

| # | 变异 | 期望转红的用例 | 实测 |
|---|---|---|---|
| M59 | `A` 退回**单腿**（丢掉并集 ⇒ 漏掉另一条腿推进的那段） | `test_the_union_of_the_legs_is_actually_exercised` | **转红** ✔ |
| M60 | 去掉**祖先核对**（`if mergeBase != baseSha:` → `if False:`） | `test_resolveCiSurface_fails_closed_when_the_base_is_not_an_ancestor` | **转红** ✔ |
| M61 | 祖先核对**报错被吞掉**（`return None`） | `test_resolveCiSurface_fails_closed_when_the_ancestor_check_cannot_be_run` | **转红** ✔ |
| M62 | 第一父链腿取 `parents[-1]` ⇒ **侧提交**进主线 | `test_fetchMainFirstParentTipAt_never_returns_a_side_commit` | **转红** ✔ |
| M63 | 回溯跳数上限放大到 `10 ** 9`（⇒ 不报错、无限回溯） | `test_fetchMainFirstParentTipAt_raises_when_the_chain_is_too_long` | **转红** ✔ |
| M64 | 畸形日期被容忍 ⇒ 当成 1970 epoch | `test_fetchMainFirstParentTipAt_raises_on_a_malformed_history` | **转红** ✔ |
| M65 | workflow 的 jq 去掉 `status == "completed"` 复核 | `test_merge_gate_base_run_query_rechecks_the_run_fields_in_jq` | **转红** ✔ |

⇒ 全量重跑（**65 组**）：**合计 65 组变异，净漏 0**。
⇒ M62/M63/M64 覆盖「**第一父链腿不许拿侧提交、不许无限回溯、不许容忍畸形条目**」
⇒ 每一个都是 fail-open 方向的洞。

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

### 4.2.4 第六版新增 2 组（M66–M67，第三轮安全评审 C-1）

| # | 变异 | 期望转红的用例 | 实测 |
|---|---|---|---|
| M66 | 清单删掉 `vitest.workspace/projects` 条目（⇒ 自动发现文件绕过链复活） | `test_vitest_workspace_and_projects_files_are_ci_surfaces` | **转红** ✔ |
| M67 | 反查扫描器认不出 workspace 名字形态（`match` → `False`） | `test_the_workspace_scanner_is_not_vacuous` | **转红** ✔ |

⇒ **第六版全量重跑（67 组，2026-11-08，拆分后清单）：合计 67 组变异，净漏 0**
⇒ **第四轮评审后终跑（68 组，2026-11-08）：合计 68 组变异，净漏 0（§2.10.8）**
（`python3 backend/scripts/ci_mutation_evidence.py`，退出码 0；含 M26 重锚后的复跑）。
⇒ M66 钉「清单条目本身」，M67 钉「反查守卫的扫描能力」——缺一条，C-1 的链就还通着。
⇒ **拆分后复跑是必须的**：M-3 把 M1–M36 挪进 `ci_mutation_cases_core.py`、M26 重锚
（`SHARD_COUNT: "4"` 在 ci.yml 出现两次 ⇒ 用 unit 块特有注释行消歧），
任何一处漂移都会表现为「锚点找不到 ⇒ 取证失败」⇒ 实测 67/67 锚点齐全且全部转红（终跑 68/68，见 §2.10.8）。

### 4.2.5 第五轮安全评审新增 8 组（M69–M76，2026-11-08）

| # | 变异 | 期望转红的用例 | 实测 |
|---|---|---|---|
| M69 | 清单 enum 删掉 `\|cases_core`（LOW-6：正向用例缺口） | `test_detectCiSurfaceChanges_reads_the_mutation_script_pattern_from_the_real_file` | **转红** ✔ |
| M70 | 清单删掉 `backend/alembic/env\.py`（HIGH-1） | `test_the_round5_environment_lever_files_are_actually_flagged` | **转红** ✔ |
| M71 | 清单删掉 `backend/.env` 家族条目（MEDIUM-2） | 同上 | **转红** ✔ |
| M72 | 清单删掉 `backend/.python-version`（MEDIUM-3） | 同上 | **转红** ✔ |
| M73 | 清单删掉 `frontend/.env` 家族条目（LOW-4） | `test_the_frontend_ci_files_are_actually_flagged`（11→13） | **转红** ✔ |
| M74 | 反查扫描器认不出 `.env` 名字形态（`match` → `False`） | `test_the_env_scanner_is_not_vacuous` | **转红** ✔ |
| M75 | supports 行回退成只有 `_pg_support\|milvus_isolation`（LOW-5） | `test_the_round5_environment_lever_files_are_actually_flagged` | **转红** ✔ |
| M76 | 清单删掉 `backend/scripts/seed_*.py` 通配（LOW-5） | 同上 | **转红** ✔ |

⇒ **新增 8 组先单独跑（2026-11-08）：8/8 转红、净漏 0**；锚点核对 **76/76** 齐全
（`/tmp/n6_anchor_check.py` 口径：第 occurrence 次存在 + 目标用例存在；M23 的
`trap` 锚点双 job 同文是**合法形态**，occurrence 1 = unit 侧）。
⇒ **第五轮评审后终跑（76 组，2026-11-08）：合计 76 组变异，净漏 0（§2.10.9）**

### 4.2.6 第六轮复审新增 4 组 + M76 重锚（M77–M80，2026-11-08）

| # | 变异 | 期望转红的用例 | 实测 |
|---|---|---|---|
| M76（重锚） | old 字符串从窄 seed 行改为**扩宽后**的 `backend/(?:scripts/)?seed_…` 行原文（行为不变：删除该行） | `test_the_round5_environment_lever_files_are_actually_flagged` | **转红** ✔ |
| M77 | 清单删掉 `backend/alembic/versions/*.py` 通配（HIGH-1） | 同上 | **转红** ✔ |
| M78 | 清单删掉仓库根 `uv\.toml`（MEDIUM-1） | 同上 | **转红** ✔ |
| M79 | seed 行**回退**成第五轮的窄形态（MEDIUM-2：`backend/` 根 seed 逃出） | 同上 | **转红** ✔ |
| M80 | 清单删掉 `backend/scripts/kpi_metric_db_guard\.py`（LOW-1） | 同上 | **转红** ✔ |

⇒ **新增 5 组（含重锚 M76）先单独跑（2026-11-08）：5/5 转红、净漏 0**；锚点核对
**80/80** 齐全（`/tmp/n6_anchor_check.py`，从 `backend/` 跑，口径 = 第 occurrence 次
存在 + 目标用例存在）。
⇒ **第六轮复审后终跑（80 组，2026-11-08）：合计 80 组变异，净漏 0（§2.10.10）**

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
  ⇒ 若 main 的 push run 的 `created_at` **晚于** 派发时刻（排队）但在第一父链上已可见
  ⇒ `M_T` 由**第一父链腿**兜住 ⇒ run 腿的偏差由并集吸收 ⇒ 偏保守。
- ⚠️ **`M_T` 取两条腿，各自都可能偏旧（2026-11-07 安全评审 HIGH-2）**：
  - **run 腿**（main 的 push run 列表）：main 的那次 push **没触发** workflow、
    或它的 run **创建得太晚** ⇒ 偏旧 ⇒ `A` 漏算 ⇒ fail-open；
  - **第一父链腿**（`fetchMainFirstParentTipAt`）：committer 日期 ≠ 进 main 的时刻
    （侧提交进主线）⇒ 单独看也可能偏旧（但它只在主线上 ⇒ 不会混入侧提交的 diff）。
  ⇒ `A` 取**两条腿的并集** ⇒ 谁漏掉的那一段，另一条兜住。
  ⚠️ 两条腿都偏旧**且偏到同一处**时仍会漏 ⇒ 残留窗口。
- ⚠️ **畸形的 `created_at`**（main push run 的时间戳解析失败）⇒ 第五版**报错**（fail-closed），
  不静默跳过 —— 静默跳过 = 那条 run 从候选里消失 ⇒ 可能取到错的 `M_T`。
  （PR 侧 `created_at` 缺失同样 fail-closed，见 §2.10.1。）
- ⚠️ **fork PR 的新鲜度守卫被跳过**（安全评审 MEDIUM-2，遗留）：`_considerMainRun` 的
  `head_repository` 硬条件在 fork PR 场景下照常生效 ⇒ 基线不会落到 fork；
  但 **PR run 本身**若来自 fork，其 `created_at` 与派发时刻的关系**未额外校验** ⇒ 残留窗口极小，暂不处理。
- ⚠️ **前端 CI 面的「按用例」失败集合仍不在本批范围**（第五版已把前端**配置文件**
  纳入 CI 相关面与 CODEOWNERS，堵住绕过链 ⇒ 见 §2.10.4；但 vitest 的**失败明细**
  仍不被解析 ⇒ 前端 job 只以一个不透明 ID 参与对差）：
  `ci.yml` 的 `Frontend / typecheck + coverage` 是**非分片 job** ⇒ 本工具**不解析** vitest 的
  失败明细 ⇒ 它在失败集合里只以**一个不透明 ID**（`job:Frontend / typecheck + coverage`）出现。
  ⇒ 后果：前端测试配置的改动现在**会**判退出码 6（配置文件在 CI 相关面清单里）；
  但**测试代码本身**的改动只会被**失败集合对差**以「job 整体红/绿」的形式体现
  ⇒ 若改配置让前端 job 由红转绿 ⇒ 表现为「**净丢失**」（退出码 5，仍须人工确认）
  ⇒ **不会静默放行**；但若改动让 job 仍红 ⇒ 失败集合不变 ⇒ 判「净新增 0」⇒ **对差看不见它**。
  ⇒ 要根治需要给前端 job 也做「按用例 ID 的失败集合」⇒ 那是**另一个立项**（要写 vitest 失败解析器），
  本批不做。
- **没有**改闸门的**退出码语义**（6 仍是「CI 相关面变了」，3 仍是「运行错误/须复跑」）。
- **没有**放开退出码 6 的人工仲裁流程 ⇒ 退出码 6 仍需 owner 走越权口显式合并
  （见 N7 / N8：个人仓库装不了 users bypass）。
- **没有**实现「分支一律以 main 的 tip 为底座起」的**强制机制**（工具无法强制）
  ⇒ 只写进 `Harness/rules/开发流程规范.md` 作为过程规则。
- ⚠️ **删除/削弱普通测试文件仍是 fail-open 方向**（第三轮安全评审的结构性附注，2026-11-08）：
  基线里通过的用例被 PR **删除 / 改成 skip / 断言削弱** ⇒ PR run 不产生该失败
  ⇒ 净新增与净丢失都为空 ⇒ 对差看不见。普通测试文件**不在** CI 相关面、也**不在** CODEOWNERS
  ⇒ 属于仓库已声明的威胁模型上限（「挡不住作者主动伪造 stdout」，`Harness/rules/测试规范.md`）
  ⇒ 不是本批引入 ⇒ 若要收口需设计「删除/改名测试文件」的检测
  （compare 的 `status` 目前不进 `CiSurfaceInfo`）⇒ **另立项**，本批不做。
- ⚠️ **`ci_gh.py`（839 行）与 `test_ci_surface.py`（906 行）超 800 行上限未拆**（第三轮评审 M-3，部分接受）：
  `ci_mutation_cases.py`（纯数据、无锚点落点）已拆；但这两个文件的私有 helper（`_runGh` / `_SHA_PATTERN` /
  `_parseIsoTimestamp` / 各 stub）被多层**紧耦合**引用 ⇒ 批内拆分要动安全闸门的实现与守卫结构
  ⇒ 有**锚点腐烂**风险（每个锚点都逐字钉在现行实现上）⇒ 拆分**另立项**在无评审压力时做。
  参照 `chat_multistep` 853 行豁免先例（`qa-system-chat-multistep-line-cap-exemption`）。
- **没有**处理 N5（净丢失只能给 `NEEDS_REVIEW`）、N8（收紧 `enforce_admins` 的前置通道）。
- 本批**不部署**（零 DDL、零生产写）。

## 6. 相关条目

- [[qa-system-merge-gate-net-diff]]
- [[qa-system-merge-gate-protection-config]]
- [[qa-system-merge-gate-flake-whitelist]]
- [[qa-system-ci-failure-set-tool]]
