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
| HIGH-1 | `backend/alembic/versions/*.py`（139 个迁移脚本） | `alembic upgrade head` 执行每个 revision 的 `upgrade()` —— pytest 收集前的任意 Python；工程规则要求**每个 ORM 变更都带迁移** ⇒ 这是**常规路径**而非边缘。方向代价（刻意接受）：迁移文件入清单 ⇒ main 侧合了迁移、PR 没跟上也会判退出码 6（**假拦**）—— 本设计明确偏好假拦而不是 fail-open | 清单通配 + CODEOWNERS 通配（管未来文件）+ 正面断言（lever 测试 16 路径）+ **M77** |
| MEDIUM-1 | 仓库根 `uv.toml` | uv 沿工作目录**父链**发现配置：CI 在 `backend/` 跑 uv ⇒ 根级 `uv.toml` 也被读（**父级 `.python-version` 实测不被认**，故只收 uv.toml） | 清单 + CODEOWNERS（`/uv.toml`）+ 正面断言 + **M78** |
| MEDIUM-2 | `backend/seed_ontology.py` / `seed_models.py` | unit/conftest.py:16 **模块级** `import seed_ontology` ⇒ 收集前执行；第五轮的 `backend/scripts/seed_*.py` 通配管不到 `backend/` 根的同族 | seed pattern 扩成 `^backend/(?:scripts/)?seed_[\w.-]+\.py$` + CODEOWNERS `backend/seed_*.py` + 正面断言 + **M79**（回退到窄形态会红） |
| LOW-1 | `backend/scripts/kpi_metric_db_guard.py`。**第七轮复审（§2.10.11）**：增 `backend/scripts/__init__.py` 与 `backend/app/tests/__init__.py` + `backend/app/tests/**/__init__.py`（`**` 通配管未来子目录） | `_pg_support.assertTestDatabase` import 它的 `databaseNameOf` —— 改它 = 改「测试必须连哪个库」的守卫口径 | 清单 + CODEOWNERS + 正面断言 + **M80** |

- **M76 重锚**：第五轮的 M76 锚点是窄 seed 行，本批把该行扩宽 ⇒ M76 的 old 字符串
  同步改成宽行原文（否则锚点找不到 ⇒ 取证失败）；其语义不变（删除 seed 条目仍由
  正面断言转红）。
- **变异总数**：76 → **80**（M77–M80 新批）。锚点核对 **80/80** 齐全；新增 5 组
  （M76–M80，含重锚后的 M76）solo 跑 **5/5 转红、净漏 0**；终跑 80 组净漏 0（§4.2.6）。
- **结构性教训**（第三度同款）：「环境定义杠杆」的边界要按**执行链**画，不能按目录画 ——
  第五轮按 `backend/scripts/` 收 seed，却漏了 conftest 模块级 import 的 `backend/` 根同族；
  按 `alembic.ini`/`env.py` 收了配置与跑器，却漏了被 `upgrade head` 逐个执行的脚本本体。

#### 2.10.11 ⚠️ 收集前执行的包 init + revision 文件名形态（第七轮复审，2026-11-08）

第七轮复审（对第六轮修复后的树，345 行 diff）判 **BLOCK**：2 HIGH + 1 MEDIUM + 1 LOW：

| 级别 | 洞 | 机制 | 修法 |
|---|---|---|---|
| HIGH-1 | `backend/scripts/__init__.py` | 任何 `from scripts.… import …`（unit/integration conftest、`_pg_support`、`test_kpi_metric_backfill_plan.py`）都**先执行包 init**（第 6 行已有模块级 import）⇒ 收集前任意 Python（注册 pytest 钩子 / 改 stdout 都行）；清单与 CODEOWNERS 双 MISS | 清单 + CODEOWNERS + 正面断言 + **M81** |
| HIGH-2 | versions pattern 形态偏窄 | alembic 1.19.1 的 `_only_source_rev_file = (?!\.\#\|__init__)(.*\.py)$` ⇒ 除 `__init__`/`.#` 外**任何**非 `/` 文件名都被接受（`+ ~ 空格 @ ! #` 实测 import），`[\w.-]+` 挡不住 `0141_foo+bar.py`；CODEOWNERS 的 `*` 不受影响（本就不跨 `/` 也不排除 `+`）⇒ **自动化层被绕过、只剩人工层** | pattern 放宽 `[^/]+` + **形态钉**（`0141_probe+shape.py`，文件尚不存在）+ **M82**（改回窄形态须红 —— M77 只钉删除钉不住收窄）+ M77 锚点同步宽行原文 |
| MEDIUM | `backend/app/tests/**/__init__.py` | pytest prepend 导入模式经 `importlib.import_module("app.tests.unit.conftest")` ⇒ 沿途包 `__init__.py`（现在 7 个空文件）收集前执行；塞 import 时代码 = conftest 同级杠杆 | 清单通配 + CODEOWNERS（`__init__` 直配 + `**` 通配未来子目录）+ 正面断言 + **M83** |
| LOW | 文档计数漂移 | lever 测试实际 16 条（`.env` 两条形态钉在 frontend 反查测试）⇒ 「10→18」口径不实；「140 个文件」实为 139（`ls` 把 `__pycache__` 算了） | 更正 §2.10.10 / plan / PR body / 注释 |

- **变异总数**：80 → **83**（M81–M83 新批 + M77 重锚）。锚点 **83/83**；新增 4 组
  （M77 重锚 + M81–M83）solo **4/4 转红、净漏 0**；终跑 83 组净漏 0（§4.2.7）。
- **正面断言口径更正**：lever 测试钉 **20 个路径**（含 5 条**形态钉**——文件尚不存在、
  断言的是 pattern 接受面）；`frontend/.env` 两条形态钉在 frontend 反查测试。

#### 2.10.12 ⚠️ `backend/__init__.py`：pytest prepend 的最高一层杠杆（第八轮复审，2026-11-08）

第八轮复审（对第七轮修复后的树，306 行 diff）判 1 HIGH + 1 LOW：

| 级别 | 洞 | 机制 | 修法 |
|---|---|---|---|
| HIGH-1 | `backend/__init__.py`（**现在不存在**） | pytest prepend 导入模式下，`resolve_package_path` 沿目录父链**向上**找最后一个含 `__init__.py` 的目录作为 pkg_root。现在 conftest 的 pkg_root 停在 `backend/app`（`app/__init__.py` 是边界）⇒ conftest 以 `app.tests.unit.conftest` 导入。但 PR **新增** `backend/__init__.py` 这一个文件，pkg_root 就被顶到**仓库根** ⇒ conftest 改以 `backend.app.tests.unit.conftest` 导入 ⇒ `backend/__init__.py` 在**任何收集之前**执行（里面只需 `sys.path` 注入 `backend/`，现有 `app.*` 导入照常工作）——与 `tests/**/__init__` 同一机制、链上更高一层，且是**全新路径**、此前清单与 CODEOWNERS 双 MISS | 清单 pattern + CODEOWNERS 条目 + lever 测试**形态钉** + **M84** |
| LOW | lever 测试 docstring 过期 | 写着「M70–M80 变异」，第七轮后已是 M70–M83 | 更正为 M70–M84 |

- **关键点**：这条洞**不能靠「盘点现存文件」发现**——文件不存在，盘 100 遍也盘不出来。
  它是「**新增一个文件就改变既有执行链**」的形态 ⇒ 只能像 `vitest.workspace.*` 那样用**形态钉**
  （断言 pattern 对尚不存在的路径的接受面）管住未来。
- **变异总数**：83 → **84**（M84：删除 `backend/__init__\.py` 行 ⇒ lever 测试须红）。
  锚点 **84/84**；M84 solo **1/1 转红、净漏 0**；终跑 84 组净漏 0（§4.2.8）。
- **正面断言口径**：lever 测试现钉 **21 个路径**（**7 条形态钉**：`.env` / `.env.local` /
  `.python-version` / `backend/uv.toml` / 根 `uv.toml` / `0141_probe+shape.py` / `backend/__init__.py`）；
  `frontend/.env` 两条形态钉在 frontend 反查测试（合计口径见 §2.10.10–§2.10.12）。

#### 2.10.13 ⚠️ `backend/app/__init__.py` 链上三层全漏 + conftest import 链**派生式**守卫（第九轮复审，2026-11-08）

第九轮复审（对第八轮修复后的树，224 行 diff）判 1 HIGH + 1 MEDIUM + 4 LOW：

| 级别 | 洞 | 机制 | 修法 |
|---|---|---|---|
| HIGH-1 | `backend/app/__init__.py`（**现存，一直就在链上**） | pkg_root = `backend/app`（`app/__init__.py` 是 prepend 边界）⇒ conftest 以 `app.tests.unit.conftest` 导入 ⇒ `app/__init__.py` 在**每次收集前**执行。实测 `detectCiSurfaceChanges(("backend/app/__init__.py",))` 返回 `()`、CODEOWNERS 无条目 ⇒ 双 MISS。**枚举式修法第三次落空**（第七轮 `scripts/__init__` → 第八轮 `backend/__init__`（不存在）→ 第九轮 `backend/app/__init__`（一直在链上））——第八轮钉了**尚不存在**的、漏了**真在链上**的 | 清单 pattern + CODEOWNERS + lever 正面断言 + **M85**；**结构性收口见 MEDIUM-1** |
| MEDIUM-1 | conftest **模块级 import 闭包**未守卫 | prepend 模式下每个 conftest 的模块级 import 都在收集前执行——能塞 import 时钩子（monkeypatch 被测代码 / 换测试模块 / 改 stdout）的文件就是杠杆。链上除 `app/__init__` 外还有 `config` / `infrastructure/database` / `security/crypto` 等 harness 配置文件；逐个**枚举**已被实证连漏三层 ⇒ 必须**派生** | 新建 `ci_conftest_chain.preCollectionImportPaths`：AST 扫**所有** conftest 的顶层 `app.*` / `scripts.*` 导入并解析为仓库相对文件路径（`from X import name` 解析 `X/__init__.py` **与** `X/name.py` 两种落点；都解析不到 = 符号 ⇒ 跳过；相对导入排除；ast.parse 失败**抛错** fail-closed）+ 新建 `test_ci_conftest_chain.py`：断言每个落点**要么**在 `_CI_SURFACE_PATTERNS`、**要么**在显式豁免清单 + 反向守卫（harness 配置文件不许进豁免、必须命中）+ harness 配置 6 文件入清单/CODEOWNERS + **M91** |
| LOW-1 | summary 变更清单孤儿行 | 第八轮我自己的 Edit 把 `ci_mutation_cases_core.py` 行写残（只留行首、无第二列） | 已修：并入真实行并补 M27 重锚说明 |
| LOW-2 | lever 测试 docstring 过期 | 写着「M70–M84」/「21 路径」 | 更正为 M70–M90 / 27 路径 / §2.10.13 |
| LOW-3 | 注释措辞两处 | 「本文件」指代不明；pkg_root 写成 `resolve_package_path` 结果本身（实际是其**父目录**） | 措辞更正 |
| LOW-4 | `test_ci_surface.py` 979 行超 800 | 既有欠账（评审轮次逐轮加钉所致），不在本轮拆 | §5 记账（第十二轮 +9 行至 988，仍不拆） |

- **豁免清单仲裁（MEDIUM-1 的设计决策）**：5 个产品模块
  （`dependencies.py` / `domain/models.py` / `llm/embedding_provider_factory.py` /
  `agent_binding_cache.py` / `agent_tool_config_registry.py`）**豁免**——它们是**被测产品代码**，
  模块级代码是定义性的（ORM 类声明 / FastAPI 依赖声明 / LRU 缓存类），import 时钩子的攻击面
  显著小于 harness 配置文件（settings / engine / crypto —— 那批**全部**入清单）；且把日常
  ORM / 路由 PR 的必改文件入清单 ⇒ 全判退出码 6 ⇒ 假拦代价不成比例。
  ⚠️ 改豁免清单 = 改守卫口径 ⇒ 守卫文件在 CI 相关面 + CODEOWNERS ⇒ 必须复核。
- **M27 重锚**：utils enum 行加宽（增 `conftest_chain`）⇒ core 批 M27 的锚点同步为
  加宽后的行原文（**第三次同类教训**：M76 / M77 / M27 —— **加宽任何 pattern 行都必须重锚钉着它的变异**）。
- **变异总数**：84 → **91**（M85–M90：逐条删 6 条新 pattern 行 ⇒ lever 须红；M91：掏空派生器
  `return result` → `return {}` ⇒ 派生守卫须红）。锚点 **91/91**；新增 8 组（M85–M91 + M27 重锚）
  solo **8/8 转红、净漏 0**；终跑见 §4.2.9。
- **正面断言口径**：lever 测试现钉 **27 个路径**（仍 **7 条形态钉**）；派生守卫另钉
  harness 5 文件的反向断言（**不许**豁免 + **必须**命中）（合计口径见 §2.10.10–§2.10.13）。

#### 2.10.14 ⚠️ 祖先包 init 链 + 传递闭包**内容不变式**（第十二轮复审收口，2026-11-08）

第十二轮复审（对第九轮修复后的树，595 行 diff）判 **BLOCK：2 HIGH + 2 MEDIUM + 2 LOW** ——
全部打在派生守卫**自身的健全性**上（正是请它攻的方向）：

| 级别 | 洞 | 机制 | 修法 |
|---|---|---|---|
| HIGH-1 | **祖先包 init 链未派生** | `from app.domain.models import X` 会**先执行** `app/domain/__init__.py` —— 派生器只解析叶子模块 ⇒ `app/domain/__init__.py`、`app/services/__init__.py`、`app/models/__init__.py`、`app/infrastructure/llm/__init__.py` 四个中间包 init 全部逃出守卫（枚举式修法第四次落空的同族形态：这次是「派生器自己少看一层」） | `_resolveModuleFiles` 解析**整条祖先链**（存在的才收）+ 4 条 pattern 入清单 + CODEOWNERS + lever 正面断言 + **M92**（祖先链退化 `range(1, len(parts))` → `range(0, 0)` ⇒ 守卫红）+ **M96–M99**（逐条删 4 条新 pattern ⇒ lever 红） |
| HIGH-2 | **传递闭包止于 conftest 顶层** | 落点模块自己的 import 链也在收集前执行：`app.domain.models` 又拉起 `models/rbac`、`wiki_*_models` 等数十个文件（实测 **76** 个文件、其中 **58** 个是传递才到达的产品文件）。全入清单 ⇒ 日常 ORM / 服务 PR 全判退出码 6 ⇒ 假拦代价不成比例 | **内容不变式**取代逐文件枚举：`findImportTimeHooks(path)` —— 模块顶层语句**白名单**（导入/定义/赋值/pass/docstring/受控 If-Try）+ **钩子机制 denylist**（`sys.path` / `sys.modules` / `os.environ` / `importlib` / `setattr` / `eval` / `exec` / `open` / `_pytest` / `monkeypatch` / `subprocess` … 按根链拒绝；顶层裸表达式只放行 `model_rebuild`）。⚠️ 这**不是 purity 宣称**：纯构造（getLogger / frozenset / re.compile / 单例）是常态，全拒会在真实树上假红 **36/76** —— 所以白名单语句形态 + 只拒绝已知钩子机制。守卫：闭包内每个文件要么被 CI 面钉住、要么 `findImportTimeHooks == ()`；实测 **76/76 全过** + **M93**（闭包掏空 ⇒ 正向钉 `len ≥ 60` + `domain/models.py ∈ 闭包` ⇒ 红）+ **M94**（检测器掏空 ⇒ 合成 evil/clean 双向单元钉 ⇒ 红） |
| MEDIUM-3 | **If/Try 包裹的 import 与 `pytest_plugins` 不可见** | `try: import app.config`（分支体在收集前同样执行）与 `pytest_plugins = ["app.plug"]`（插件声明也在收集前加载）都只看 `tree.body` 顶层的派生器看不见 | `_moduleScopeStatements` 对 If/Try/With/AsyncWith/For/While 的 body/handlers/orelse/finalbody **递归展开**；`pytest_plugins` 字符串/清单赋值一并解析（前缀过滤）+ **M95**（递归关闭 `if False:` ⇒ 合成树守卫红） |
| MEDIUM-4 | **豁免清单内容盲** | 豁免理由写的是「模块级代码是定义性的」⇒ 那是**宣称**不是被钉住的事实 ⇒ 有人往豁免文件里塞钩子，守卫照样绿 | 豁免 5 文件**全在传递闭包内** ⇒ `findImportTimeHooks` 检查对它们**同样生效**（守卫测试 docstring 明示；往豁免文件塞 `sys.path` 注入 ⇒ 闭包守卫红） |
| LOW-5 | **CI 面 ⊆ CODEOWNERS 未派生** | 两份人工清单会漂移：文件进了 `_CI_SURFACE_PATTERNS`（改它 = 退出码 6）却没有 owner ⇒ 仲裁通道无人被请求复核 | 派生守卫 `test_every_ci_surface_file_in_the_pre_collection_closure_has_a_codeowner`：闭包 ∩ CI 面 ⇒ `_effectiveOwner` 必须非空 |
| LOW-6 | `parents[4]` 脆性 + 叶子优先顺序 | `Path(__file__).resolve().parents[4]` 在文件移动时静默指错（fail-open 方向）；`.py`-先于-`__init__.py` 与 CPython 相反（CPython 先试包再试模块）——但仓库内**不存在**同一模块名同时有 `.py` 与包两种形态 ⇒ 无实害 | **接受，暂不修**（§5 记账）：`parents[4]` 有测试内 `assert mapping` 非空兜底（指错 ⇒ 派生为空 ⇒ 红）；双形态先例出现时再调 |

**设计决策（HIGH-1 vs HIGH-2 的不同处置）**：4 个包 init 走**清单**（它们是罕见编辑的
harness 杠杆，退出码 6 代价低）；58 个传递产品文件走**内容不变式**（它们是日常 ORM /
服务 PR 的必改文件，入清单 = 假拦洪灾）。两条腿合起来把 HIGH-1 的洞类从「靠枚举」
变成「靠派生 + 内容约束」，且约束被变异取证钉死（M92–M99）。

**附带收口**：`unit/conftest.py` 的 `import seed_ontology` 从**模块级**移入 `seedEngine`
**夹具体**（HIGH-2 的廉价收口）—— conftest 模块级 import 在任何测试收集之前执行 ⇒
模块级写它会把 seed_ontology 及其整条链（neo4j_client / ontology_service / …）全变成
收集前执行的代码；移入夹具体后实测 seed 链掉出闭包。conftest 本体已在 CI 面清单
（任意深度 conftest pattern）⇒ 改动本身有授权落点。

**实测口径**（守卫运行时派生，非枚举）：level-1 落点 **18**（4 个 conftest）、传递闭包
**76**（58 个传递产品文件）、钩子 **0**。⚠️ `app/models/__init__.py` **没有** level-1 落点
（无 conftest 直接 import `app.models.*`）—— 它经 `dependencies.py` 的传递链进入闭包，
钉法与前三者不同（M92 钉 3 个 level-1 祖先 init；`models/__init__` 由闭包断言 + M98 pattern
删除钉住）。

- **变异总数**：91 → **99**（M92–M95 打派生器/闭包/检测器/递归四个实现点；M96–M99 逐条删
  4 条新 pattern）。锚点 **99/99**；solo **8/8 转红、净漏 0**；终跑见 §4.2.10。
- **正面断言口径**：lever 测试现钉 **31 个路径**（仍 **7 条形态钉**）；
  `pytest -k ci_` **375 passed / 19 skipped**（+5 条新守卫，净新增失败 0）。

#### 2.10.15 ⚠️ 第十三轮复审收口：位置全覆盖 / 别名 / 派生边（第十四轮修复，2026-11-08）

第十三轮安全复审（对 round-13 fix `aae372ee`）判 **BLOCK 3H+3M+2L**，且复审者用**真实
import 实验端到端证明**了每个洞（守卫全绿而恶意模块真实执行）。修复（`ci_conftest_chain.py`
结构性重写）：

| 级别 | 洞 | 机制 | 修法 |
|---|---|---|---|
| HIGH-1 | **表达式位置从未拒绝扫描** | `if sys.path.insert(0,'x') or True:`、`except <expr>:`、装饰器参数（`@d(__import__('app.evil'))`）、默认值（`def f(x=sys.path.insert(0,'x'))`）、注解 —— `_iterImportTimeNodes` 会 yield 这些节点但拒绝扫描只匹配裸 Name/Attribute 根 ⇒ 全部逃出 | **结构白名单与根链拒绝解耦**：白名单只决定「语句形态是否定义性」，根链拒绝对**所有**语句的 import 期表达式统一跑（新 `_iterExecNodes`：全子树、函数/Lambda body 除外、装饰器/默认值/注解**递归**进子树） |
| HIGH-2a | **拒绝清单可被别名绕过** | `import sys as s; s.path.insert(...)`、`from os import environ as e; e['X']=1`、`from sys import path; path.insert(...)`；任意 sink（`os.system` / `runpy` / `ctypes` / `builtins` / `shutil` / `socket`）不在清单 | 根链先过 `_aliasMap`（`import a as b` / `from m import n as p` / **无别名 from-import 也映射**）再匹配；拒绝清单加宽执行族 sink；`pathlib` **不**拒（顶层 `Path(...)` 是常态，拒了全是假红）。**Load/Store 分治**：裸 Name 只在 Load（真引用）时判拒 + Store 只认「import 进来的别名」——否则 Pydantic 字段注解 `input: str`（第十四轮校准实测）假红 3 文件 |
| HIGH-2b | **类创建钩子不可见** | `class X(metaclass=M)`、类体 `__init_subclass__`/`__set_name__`/`__class_getitem__` 在 class 语句执行（import 期）时就会跑 | `_classCreationHookMessages`：`metaclass=` 关键字 + 类体钩子名清单；描述符 `__get__/__set__` 不算（实例访问期执行） |
| HIGH-3 | **派生边缺失** | 相对导入被 `node.level == 0` 过滤整条丢弃；`from X import *` 未展开 `__all__`（CPython 会连带 import）；顶层前缀硬编码 app/scripts ⇒ repo 根其它包逃出（复审者合成 3 恶意模块端到端执行、守卫绿） | 相对导入按所在包拼绝对名（`_absoluteModuleName`）；star 展开目标模块 `__all__` 字符串（`_allEntries`）；**「能解析到 backend/ 下文件」即派生**（解析不到的名字自然空集，不再前缀过滤）；`pytest_plugins +=` 同样算插件声明 |
| MEDIUM-1 | **检测器零判别** | `_DENIED_ROOTS` 清空 / 扫描循环整段删除 ⇒ 6/6 守卫仍绿（evil fixture 是裸 Expr，被结构分支抓住、不经拒绝扫描） | Assign 位置 evil fixture（结构允许、只有拒绝扫描能抓）+ **M100**（扫描循环删除 `for node in ()` ⇒ 红）+ **M101**（`_isDeniedChain` 恒 False ⇒ 红） |
| MEDIUM-2 | **`_moduleScopeStatements` 逐臂零判别** | try_only/no_if/no_loop/no_with/no_class/no_handlers 全部 6/6 绿 | 逐臂结构问题测试（13 臂 × `logging.basicConfig` 裸表达式——不在拒绝清单，唯一抓手是臂展开）+ **M95 重锚**（分支 elif）+ **M111–M114**（match / handler / orelse / finalbody 各自单独杀 ⇒ 各自参数红） |
| MEDIUM-3 | **测试模块的模块级产品 import 不检测** | 闭包止于 conftest 链；`test_chat_domain_actor_contract.py:35` 这类测试模块顶层产品 import 的执行在钩子检测范围外（实测扩展闭包 **465** 文件） | **接受弱解**（§5 记账）：扩展到 465 文件会把 `mcp_server.py`（活产品文件）的 `os.environ.get` 读、十余个一次性脚本的 `sys.path.insert` 全拖进仲裁面 ⇒ 假拦代价不成比例；**修 docstring 的过度声明**（「收集前会执行的全部仓库文件」→「conftest 收集前 import 链的闭包」）+ §5 残余条目。该残余与「普通测试文件削弱」同类 |
| LOW-1 | Match/TryStar 不递归 + AugAssign plugins | `_moduleScopeStatements` 只认 6 种分支；`pytest_plugins += [...]` 不可见 | `_BRANCH_TYPES` 加 Match/TryStar（getattr 兼容）；`_pytestPluginsEntries` 认 AugAssign（M110） |
| LOW-2 | `model_rebuild` 接收者不限 | `get_module().model_rebuild()` 动态接收者放行 | 结构白名单要求接收者是**裸 Name**（点分/调用接收者是结构问题；M118） |

**校准（第十四轮，真实树实测）**：新实现在真实树上一层落点 **18**、传递闭包 **76**、钩子 **0**
（与第十二轮持平 ⇒ 派生边泛化没有引入新落点，机制由合成树测试 + 变异钉住）；扩展闭包（测试
模块作根）**465** 文件、0 条 star 边、~12 个真实钩子文件（全部由测试模块的模块级产品 import
带入）—— 这是 MEDIUM-3 弱解决策的实测依据。

**变异总数**：99 → **121**（M100–M121，22 组，清单独立成 `ci_mutation_cases_r14.py`
保 800 行上限；M95 因 `_moduleScopeStatements` 加 Match/TryStar **重锚**）。锚点 **121/121**
齐全（.py 落点全部核过、变异后文本可解析）；solo 见 §4.2.11；终跑见 §4.2.10 后记。
**守卫双向补强**：M104（Load-ctx 判定删除 ⇒ `input: str` 字段假红 ⇒ clean 测试红）、
M105（函数体跳过删除 ⇒ 方法体内钩子假红 ⇒ clean 测试红）—— 两个**假红方向**变异钉住
「检测器收紧过头」的回归。

#### 2.10.16 ⚠️ 第十五轮复审收口：表达式全拒绝扫描 / 类创建钩子 / 被调解析 / 双形态（第十六轮修复，2026-11-08）

第十五轮安全复审（对象 = aae372ee..48016aee 的 diff）：**0 CRITICAL / 5 HIGH / 2 MEDIUM / 3 LOW**，
全部带攻击构造实测（每个 HIGH 都给了能绕过当时检测器的具体源码）。第十六轮逐条修复：

| 编号 | 发现（攻击构造） | 修复 |
|---|---|---|
| H-1 | `def f(*args: sys.path.insert(0,'x'))` —— vararg/kwarg 的注解不求值（首轮 `_iterExecNodes` 只扫 posonly/args/kwonly） | argNodes 改条件式追加 `args.vararg` / `args.kwarg`（M122/M123） |
| H-2 | 别名链被**模块根级重绑定**绕过：`_t = sys` 后 `_t.path.insert`；`sys.__dict__["modules"]`；`vars(sys)`；`from os import *` | 裸 Name 单赋值的别名传播（`aliases[t.id] = source`，M124）；`__dict__` 在别名解析**之前**短路（M125）；denylist 增 `vars` / `object.__setattr__` / `object.__getattribute__`；拒绝根模块（`_DENIED_STAR_ROOTS`）的星号导入**整个拒绝**（M126 —— 动态 `__all__` 展开不可判 ⇒ 与 M-1 的 fail-closed 配套） |
| H-3 | 类创建钩子逃逸：`class X(**{"metaclass": M})`（kw.arg is None）；钩子 def 藏在分支臂；`Base.__init_subclass__ = …` 属性再绑定；`class Y(H())` 基类 Call（`__mro_entries__` 途径）；`type.__setattr__` 在 Assign 位置改类 | `kw.arg is None` 一律拒（metaclass 静态不可判，M127）；钩子 def 扫描走 `_moduleScopeStatements`（类体**任意嵌套深度**，M128）；Store 位置 `attr ∈ _REBIND_DENIED_ATTRS`（钩子名 + 允许方法名都算，M129）；基类 `Call/Starred` 拒绝（M130）；denylist 增 `type.__setattr__`（M131） |
| H-4 | 被跳过的函数体在 import 期执行：同模块调用 `_x = m()`；IIFE `(lambda: …)()`；分支内 def 的装饰器；PEP 562 模块级 `__getattr__`（包 init 定义 + 别处 `from pkg import missing` 即执行） | `findImportTimeHooks` 重写：`scanExec`/`scanCallee` 两段式 —— 同模块顶层 def（含分支内、**不含**类体方法）+ 模块级 lambda 绑定组成 `defs`，Call 到 `defs` 里的名字/裸 Lambda ⇒ 解析其体（visited 去重 + `_MAX_CALLEE_DEPTH = 8`；**嵌套 def 的调用不解析** = 文档化局限，见模块 docstring）（M132 被调解析 / M133 IIFE）；模块级 `__getattr__`/`__dir__` 直接拒（M134） |
| H-5 | `.py`/包**双形态遮蔽**：CPython `FileFinder` 同目录**先试包目录再试 `.py`**，派生器此前优先 `.py` ⇒ 攻击者保留无害 `foo.py`、新增恶意 `foo/__init__.py` 即让真实执行点逃出闭包 | 双形态**两个叶子都收**（superset，fail-closed 方向；M135 + 合成树测试 `test_dual_form_module_derives_both_leaves`） |
| M-1 | 动态 `__all__` 的星号展开 fail-open（CPython 实测会连带 import `__all__` 里的子模块） | `__all__` 非静态字符串字面量清单 ⇒ **RuntimeError**（fail-closed；真实闭包 61 处 `__all__` 全部静态 ⇒ 零校准代价；M136 + `test_dynamic_all_raises_fail_closed`） |
| M-2 | **假红方向**：match guard 被当伪语句塞进结构白名单 ⇒ 任何带 guard 的模块级 match 假红；且 AugAssign / Global / TypeAlias 不在白名单 | guard 不再进结构白名单（guard 是表达式，其内容由拒绝扫描经 `_iterExecNodes` 全子树覆盖；`match_guard` fixture 改用拒绝轴 evil、`match_guard_benign` 新增放行钉）；白名单补 `AugAssign` / `Global` / `Nonlocal` / `Pass` / `TypeAlias` |

**架构口径变更（本轮定稿）**：守卫对表达式**只做拒绝扫描**（`_iterExecNodes` 全子树：所有语句、
分支臂、装饰器、默认值、**全部参数注解**），结构白名单只管**语句形态** —— 两条轴彻底解耦。
`match_body` 等分支臂测试保留结构轴（`logging.basicConfig` 不在拒绝清单，唯一抓手是结构白名单），
`match_guard` 改拒绝轴 —— 逐臂判别性由「守卫双向 + 变异独占落点」保持。

**校准（真实 76 文件闭包，防假红）**：`__dict__` 0 处、`vars(` 0 处、模块级 `__getattr__`/`__dir__` 0 处、
基类 Call/Starred 0 处、星号导入 0 处；`object.__setattr__` 仅 1 处且在 `__post_init__` 方法体内
（intent_service.py:536，非 import 期）⇒ **零假红**。

**修出的真雷（顺手收获）**：M133 solo 取证暴露 `scanCallee` 的 Lambda 分支直接传 body 节点
而非经 `_iterExecNodes` 展开 ⇒ Attribute 子节点根本没被扫 —— IIFE 检测此前是空转的。

#### 2.10.17 ⚠️ 第十七轮复审收口：dunder 通道 / import 期执行路径 / 语句序被调解析 / 接收者感知白名单 / PEP 562 全形态（第十八轮修复，2026-11-08）

第十七轮安全复审（对象 = 第十六轮收口后的 diff）：**0 CRITICAL / 5 HIGH / 1 LOW**，
全部带攻击构造。第十八轮逐条修复：

| 编号 | 发现（攻击构造） | 修复 |
|---|---|---|
| H-1 | 模块 dunder 属性链绕过 `__dict__` 短路与拒绝清单：`sys.__getattribute__('path').insert(...)`、`from sys import __getattribute__`（Name 位置）、`os.__getattribute__('environ')[...] = ...`、`sys.__class__.__getattribute__(sys,'__dict__')`、裸名 `__builtins__['exec']`（`__main__` 里它是 dict 本体） | 统一拒绝判定 `_isForbidden(chain)` = `_isDeniedChain` **或**链上任一段 ∈ `_FORBIDDEN_DUNDER_SEGMENTS`（`__dict__`/`__getattribute__`/`__class__`/`__globals__`/`__code__`/`__subclasses__`/`__mro__`/`__bases__`）；Name(Load/Store)/Attribute 三处全走它；`__builtins__` 进 `_DENIED_ROOTS`（根级拒，不进段集 —— 段集若含它会让 M138 失去独占落点）。段集**刻意不含** `__setattr__`：`__post_init__` 里合法的 `object.__setattr__` 由根级拒绝兜住（校准证实） |
| H-2 | import 期执行路径族：`_x = X()` 跑 `__init__`；`X.m()` / `X().m()` 实例方法调用；`@D.deco` 装饰器；`with C():` / `for _ in C():` / `_x += 1` 的隐式 dunder（`__enter__`/`__iter__`/`__iadd__`）；类体钩子名的**赋值/注解形态**（`__init_subclass__ = classmethod(lambda …)`）；嵌套类 `Outer.Inner()`；类体描述符 `attr = D()` 触发 `__set_name__`；模块级三参 `type()` 动态建类 | `findImportTimeHooks` 第二次重写：`localClasses`（模块级类，含分支体）+ `instanceVars`（`X()` 的赋值）+ `_instantiationHooks`（`__new__`/`__init__`/`__post_init__` 成员体扫描）；Call→类名 ⇒ 实例化钩子、`X().m()` ⇒ `_receiverMember` 成员体扫描、三参 `type()` 一律拒；With/For/AugAssign 用 `_usesLocalInstance` fail-closed；类创建钩子检查从 FunctionDef 扩到 Assign/AnnAssign 双形态；类体 Assign 值为本地类实例 ⇒ 扫其 `__set_name__`；装饰器 `@D.deco` ⇒ 扫 D 的 `deco` 成员体。⚠️ 不能用 `ast.walk(FunctionDef)` 粗扫 —— 校准实测假红 34（`intent_service.py` 的 `__post_init__` 等合法体），必须沿 import 期执行路径精扫 |
| H-3 | 被调解析不按**语句序**：`defs` 是 last-wins 全字典 ⇒ `def m: evil / _x = m() / def m: pass` 解析到第二个 def；名字别名 `_f = m`、`_g = _h` 不注册 | `callTargets` 改为**语句序**在主循环里逐语句注册（`applyBinding(stmt)` 先于 `scanExec`）；Name-value 绑定沿 `callTargets` 传播（def → lambda → 名字 → 实例四类绑定） |
| H-4 | `model_rebuild` 白名单是任意调用通道：本地类重定义 `model_rebuild`（classmethod 体里 `sys.path.insert`），`X.model_rebuild()` 走白名单放行 | 接收者感知：接收者是**本地类** ⇒ `_receiverMember` 扫其同名成员体；接收者是导入类 ⇒ 维持白名单；`type()` 建的接收者由 H-2 的三参 `type()` 检查 fail-closed |
| H-5 | PEP 562 模块 `__getattr__`/`__dir__` 的 **Lambda 赋值形态**（`__getattr__ = lambda …`）与注解形态逃出（M134 只钉 def 形态；pytest 自身收集即触发 `pytest_plugins` 等名字） | Assign/AnnAssign 位置的目标名 ∈ `_MODULE_GETATTR_NAMES` ⇒ 直接拒（绑定本身即收集前钩子，体良性也拒） |
| L-1 | M136 的变异 fixture 不独占：`__all__ = [name for name in ('x',)]` 在被变异的副本上 `value.elts` 抛 AttributeError ⇒ 收集失败被三态判据排除 ⇒ 锚不到 fail-closed 条件 | fixture 改集合字面量 `__all__ = {'x'}`（ast.Set 有 `.elts` ⇒ 变异副本静默继续 ⇒ 转红落在被删的 fail-closed 条件上） |

**架构取舍**：禁止 dunder 段集（8 段）与拒绝根（`__builtins__`）分离 —— 段集是「抹掉静态链/任意执行
通道」的形态钉，根集是「本体危险」的名字钉；两者判据不同，合并会让 M138 失去独占判别点。

**行数**：`ci_conftest_chain.py` 649 → **923 行**（超 800 上限；拆分会打断 ~50 条逐字文本锚点
⇒ 记 §5 行债，下次触碰该文件时顺手拆）。

**校准（真实 76 文件闭包，防假红）**：旧实现（HEAD）vs 新实现逐文件对比 `findImportTimeHooks` 输出
⇒ **DIFFS = 0**（严格性提升对产品闭包不可见 —— 第十七轮复审的校准结论一致）。

**测试**：`test_ci_conftest_chain.py` 65 → **84 条**（+19 evil fixture，每个附独占落点注释；
含 LOW-1 的 `__all__` fixture 修正）。

#### 2.10.18 ⚠️ 第十九轮复审收口：解析不了 = fail-closed（第二十轮修复，2026-11-09）

第十九轮安全复审（对象 = 第十八轮收口后的 diff）：**0 CRITICAL / 7 HIGH / 3 MEDIUM / 1 LOW**，
全部带攻击构造。第二十轮逐条修复，设计原则一句话：**静态解析不了接收者/成员 ⇒ 一律拒绝（fail-closed），
`model_rebuild` 豁免只对「确定是导入类」的接收者生效**。

| 编号 | 发现（攻击构造） | 修复 |
|---|---|---|
| H-1 | 动态根属性链：`(lambda: 0).__globals__` / `_d['f']().__class__` —— 接收者不是 Name，`_receiverMember` 形态不覆盖 | 新增 `_dynamicRootForbiddenSegment`：动态根（Lambda/Subscript/Call/…）属性链逐段过禁止 dunder 段集（M152）；`__setattr__` 补进段集（M155）；`sys.meta_path` 补进拒绝根（M171） |
| H-2 | `_resolveMember` 接收者解析缺口：实例变量接收者（`_x = X(); _x.m()`）、本地工厂（`make().m()`）、下标取函数（`_d['f']()`）、构造器成员（`X().m()`）各自逃出 | `_resolveMember` 重写为**三态**返回 `(member, status)` ∈ `local` / `unknown` / `dynamic`：实例变量臂（M156）、构造器臂（M157）、下标臂（M154）、动态接收者 fail-closed 臂（M153）、本地未定义成员 fail-closed 臂（M174，`model_rebuild` 等豁免例外检查）。解析不了 = `dynamic` ⇒ 拒绝 |
| H-3 | 类体名字绑定缺口：`__init__ = evil`（Name 值绑定）、`model_rebuild = staticmethod(evil)`、`_t = type; X = _t('X', (), {...})` 别名形态 | applyBinding 增 Name 值绑定解析（M158）、staticmethod/classmethod 解包臂（M159）、`typeAliases` 集合（`X = type` 传播；M160） |
| H-4 | 条件重定义歧义：`if c: def m: evil / else: def m: pass` —— last-wins 解析落到良性分支 ⇒ 逃出 | `ambiguousNames` / `ambiguousClasses`：if/else 同名重定义 ⇒ 名字进歧义集，被调/实例化点一律 fail-closed（M161/M173/M162） |
| H-5 | 装饰器预扫缺失：`def m: evil / @m def m` —— 预扫不跑则 scanExec 阶段解析到新绑定（良性体）；`@lambda_deco` 装饰期执行 lambda | 主循环装饰器**预扫先于 applyBinding**（旧名解析 ⇒ M163）；动态装饰器（Lambda/Subscript/Starred）直接拒绝（M169） |
| M-1 | 实例协议路径：`_x[0]`（`__getitem__`）、`if _x:`（`__bool__`/`__len__`）、`list(X())`（`__iter__`）、`_x.p`（@property getter） | 形态元组（Subscript/BinOp/Compare/UnaryOp/BoolOp ⇒ M164）、test 表达式检查（M165）、实参扫描（M166）、实例属性读臂（M172，**调用路径对 func Attribute 的读取有决定权**——`resolvedAttrIds` 在 Call 入口先于 func 子节点登记，先序遍历保证调用线不被读取通则罩住假绿） |
| M-2 | 被调链深超上限 ⇒ 静默截断（cap 还在、只是不报）⇒ 深处 evil 逃出 | 报错行保留且**必须报**（M170：删掉报错行 ⇒ fixture 红）；PEP 562 元组解包 walk（M167）与 from-import 别名（M168）各钉一条 |
| L-1 | `pep = _pep562BindingProblem(stmt)` 旧锚被重写吞掉（行已不存在） | M145 重锚 + M140/M141/M146/M150/M133（r16）一并按新行形态重锚；**substring 锚雷**：16 空格条件行是 20 空格 scanExec 行的子串 ⇒ `src.count()==2` ⇒ 跑者替换错处 ⇒ M163 首锚 solo 证伪；**orphan-else 雷**：多行锚只换 if 不换 else ⇒ 留孤儿 `else:` ⇒ SyntaxError ⇒ 非法红。教训固化进 M163 注释：锚必须带 `for dec in stmt.decorator_list:` 前缀行 + 整块 if/else 一起换 |

**架构取舍**：`_decoratorMember` **单点**辅助函数——`@D.deco` 成员解析原本在主循环预扫与
scanExec 两处同文 ⇒ 单锚变异只杀一处、另一处罩住 fixture（假绿）；收敛为单点后两处共用一份
解析，M150 锚死两处。同理，`_ALLOWED_EXPR_METHODS = ("model_rebuild",)` 豁免把「成员解析线」
的变异判别买了出来——成员解析被变异关闭后 fixture 走豁免放行 ⇒ 转红，不会被「未定义成员
fail-closed」线罩住。两条设计性依赖写在 `ci_mutation_cases_r20.py` 文件头，评审时别当巧合。

**校准（真实 76 文件闭包，防假红）**：每次检测器改动后跑 `/tmp/n6_round14_calibration.py`
⇒ **DIFFS = 0**（三态解析的 fail-closed 提升对产品闭包不可见）。

**行数**：`ci_conftest_chain.py` 923 → **~1030 行**（§5 行债更新；拆分打断 ~60 条逐字文本锚点
⇒ 仍在「下次触碰该文件时顺手拆」的既定策略内）。

**测试**：`test_ci_conftest_chain.py` 84 → **113 条**（+24 evil fixture 覆盖 lambda_globals_spine /
dynamic_receiver_local_factory / subscript_call / ctor_member_call / setattr_segment /
instance_method_call / init_name_assign / staticmethod_member / type_alias_three_arg /
cond_redef / class_redef / decorator_prev_binding / lambda_redef / instance_subscript /
instance_truthiness / instance_as_arg / pep562_tuple_unpack / pep562_import_alias /
lambda_decorator / callee_depth_cap / meta_path_install / instance_property_read /
missing_member_call；+6 clean fixture 含 `imported_ctor_chain` / `imported_result_call`
——**裸顶层调用被拒是设计**，clean fixture 必须写成赋值形态）。

#### 2.10.19 ⚠️ 第二十一轮复审收口：unknown 放行洗白链 + 语句级检查盲区（第二十二轮修复，2026-11-09）

第二十一轮安全复审（对象 = 第二十轮收口后的 diff）：**2 CRITICAL / 9 HIGH / 2 MEDIUM / 1 LOW**，
全部带攻击构造。第二十二轮逐条修复，设计原则两句话：**「unknown 放行」不再吃表达式级的洗白**
（不透明绑定 + 守卫模块根 + 动态根危险段三路封堵）；**语句级检查从「只看模块级」扩到
类体 / 被调体 / 函数体作用域**（类体真值、被调体内实例协议、fn 局部类覆盖层）。

| 编号 | 发现（攻击构造） | 修复 |
|---|---|---|
| C-1 | 守卫自身可再绑定：闭包文件 import 期 `import app.utils.ci_conftest_chain as _c; _c.findImportTimeHooks = _spy` —— 选择性跳过自己的文件 ⇒ 内容不变式整体空转 | `_GUARD_MODULE_ROOTS` 四根（conftest_chain / ci_surface / ci_failure_set / ci_gh）入 `_isForbidden`（M175）；真实闭包 0 处 import 这些模块（校准 0 假红） |
| C-2 | 动态根洗白：`[os][0].environ['X'] = ...` / `(lambda: sys).path.insert(...)` —— 静态根清单只对点分 Name 链生效，洗进动态包装后取属性整条逃出 | `_DYNAMIC_DENIED_SEGMENTS`（environ/path/modules/meta_path/path_hooks/path_importer_cache/settrace/setprofile/audit/system/popen/argv）对动态根属性链逐段拒绝（M176） |
| H-1 | 类别名洗白：`_c = X; _c()` / `@_D` / `@_D.deco` —— 类别名不在 localClasses ⇒ unknown 放行 | `classAliases` 登记（applyBinding Name 链）+ 三路解析：Call 实例化臂（M177）、裸名装饰器臂（M178）、`_decoratorMember` 成员回退（M203） |
| H-2 | 可调用实例：`_x = X(); _x()`（fn 体内）/ `@_x` —— Name 分支此前不查 instanceVars | Call-Name instanceVars 臂（M179，`__call__` 体扫描）+ 装饰器 instanceVars 臂（M180） |
| H-3 | 实例化结果属性读：`X().p`（@property getter 在 import 期跑） | scanExec Attribute-Load 的 `_usesLocalInstance(node.value)` 臂（M181） |
| H-4 | `super().pwn()` / `type(self).pwn()` 返回对象静态不可判 | `_resolveMember` Call 分支 `super`/`type` 接收者 → dynamic fail-closed（M182） |
| H-5 | 实例属性赋值 / 协议节点：`_x.attr = 1`（`__setattr__`）、f-string 插值（`__format__`）、dict 键（`__hash__`）、推导/解包（`__iter__`） | scanExec 五个新块：Attribute-Store 臂（M183）、协议节点元组（JoinedStr/Dict/推导/Starred，**协议位置谓词** `_usesLocalInstanceInProtocolPosition`——dict 只查键不查值、f-string 只查 FormattedValue，M184）、元组解包臂（M185） |
| H-6 | 成员/钩子绑定条件臂歧义：`if c: def m: evil / else: def m: pass` 经 `_classMember` 落到末位 def | `_classMember`/`_instantiationHooks`/`_decoratorMember` 三态歧义收集（ambiguity 参数）+ 四个 fail-closed 落点（M187 实例化钩子 / M188 嵌套类 / M189 装饰器成员）；Call-Attribute 成员歧义经 `_resolveMember` 内部歧义→dynamic 转换统一 fail-closed（M186 钉 Name 接收者臂的转换行——scanExec 侧原 `if memberAmbiguity:` 块与 `status == "dynamic"` 检查**全冗余**，重复线拿不到独占变异取证 ⇒ 删除，注释固化） |
| H-7 | `type` 别名链：`_t = type; class X(metaclass=_t)` / `_t('X', (), {...})` | applyBinding 的 typeAliases 链传播（M190） |
| H-8 | 类体语句级检查缺失：`class C: _x = X(); if _x: ...` —— 类体执行但语句检查只在模块级 | 类体 pass：cbVars/cbInstances 记账 + 逐语句 `_instanceExecProblem` + If/While/Match 真值（M191） |
| H-9 | 被调体语句级检查缺失：evil 藏在被调函数体内、依赖 fn 局部类/局部 import 别名 | scanCallee 重构：fnClasses 覆盖层（遮蔽=确定性 fn-local；**fn 内重定义**=None fail-closed，M194）、`scopeAliases = {**aliases, **_aliasMap(callee.body)}`（M195）、逐语句 fnProblem（M192）、fn 局部类实例化臂（M193）、类创建钩子消息 |
| M-1 | 不透明绑定登记缺口：`_g = f1 if True else f2`（IfExp 值）⇒ `_g()` 逃出 | applyBinding 兜底 else 登记不透明名（Dict/List/Set 字面量**不**算不透明——真实闭包大量模块级 dict + `.keys()`，校准实证；M196）+ 不透明名调用臂（M197）/装饰器臂（M198）/容器下标赋值臂（M199） |
| L-1 | 深嵌套 AST / 超大文件 ⇒ RecursionError 炸穿守卫（fail-open） | `_MAX_AST_NODES = 100_000` 迭代预算（超限整文件 fail-closed，M201）+ RecursionError 包装为 hook 行（M202）；本地类直接装饰器臂（M200） |

**判别点独占性（solo 取证前三轮实证的教训固化）**：4 个 fixture 首版被别的线罩住 ⇒ 重设计——
`callable_instance_call` / `opaque_ifexp_call` / `opaque_name_call` 的调用**必须藏进被扫函数体**
（装饰器函数体），顶层裸调用会被「顶层表达式」结构白名单线兜住；`instance_setattr_store` 的类体
**必须全干净**（载荷放 `__init__` 会被实例化钩子线兜住）；`class_alias_bare_decorator`（裸名 `@_D`）
与 `class_alias_decorator`（成员 `@_D.deco`）拆成两个 fixture——`_decoratorMember` 只处理 Attribute
形态，裸名只走 scanExec 装饰器臂（M178 判别点独占的前提）。M186 首锚（scanExec 的 memberAmbiguity 块）
被 M153 的 dynamic 臂罩住 ⇒ 判别点移到 `_resolveMember` 内部转换行。

**校准两轮收敛**：首版协议节点块用全子树 `_usesLocalInstance` ⇒ 真实闭包 DIFFS=2（agent_tools.py
的 dict 值、supplier_risk_service.py 的 `_Rule(...)` 值）⇒ 改协议位置谓词（dict 键/f-string 插值/
推导 iter/Starred value 才算）+ 容器字面量不算不透明 ⇒ **DIFFS = 0**。

**装饰器臂去重**：主循环与 scanExec 两份装饰器臂同文 ⇒ 双份同文锚不死两处（r20 M150 的教训）。
第二十二轮把主循环兄弟臂**整体删除**（`_iterExecNodes` 会 yield FunctionDef ⇒ scanExec 副本覆盖
模块级装饰器），只保留 M163 的预扫块 ⇒ 四个装饰器臂全部 count==1 可独占锚。

**行数**：`ci_conftest_chain.py` ~1030 → **1755 行**（§2.10.19 落定后第二轮删冗余 deny 块
（M172 实证：与 H3 块条件严格超集，见 §4.2.15），1762 → 1755；§5 行债更新；仍是「下次触碰顺手拆」策略）。

**测试**：`test_ci_conftest_chain.py` 113 → **148 条**（+30 evil fixture 对准 M175–M203 判别点 +
5 clean fixture 防假红；两条 L1 独立用例钉 AST 预算与递归包装——深嵌套向量用 5000 段属性链：
缩进嵌套被 CPython 100 层硬限拦在 Parse 阶段，不可用）。

#### 2.10.20 ⚠️ 第二十三轮复审收口：夹带路径 + sink 赋值形态 + 装饰器/被调 fail-closed（第二十四轮修复，2026-11-09）

第二十三轮复审（对象 = 第二十二轮收口后的 diff）：**6 CRITICAL / 2 HIGH / 2 MEDIUM / 3 LOW**。
修复前先独立复验（评审员的 H-1 顶层形态指控被探针全部证伪——IfExp/Walrus/Dict-get/map/lambda-arg
作被调在顶层全被「顶层表达式」结构线拦住；真实逃逸位在被扫函数体内经 `_r = entry()` 可达），
再按实测设计修复。校准口径（真实 76 文件闭包盘点）：模块/类级 Attribute Store = **0**、
装饰器 = Call ×57（根全为 pytest/dataclasses/functools/abc 等外部模块）+ Name ×69
（classmethod/staticmethod/property 内置 + abstractmethod/lru_cache from-import）⇒ 全部
fail-closed 加宽零假红。

| 编号 | 发现 | 修复 |
|---|---|---|
| C-1 | `.pyc`/`__pycache__` 字节码夹带：`git add -f` 绕过 .gitignore；CHECKED_HASH 头只校验源文件哈希、marshal 正文可任意伪造 | `_CI_SURFACE_PATTERNS` 增 `.*\.pyc$` 与 `(?:^|.*/)__pycache__/`（M204/M205）⚠️ 首版写成 `r"\.pyc$"` / `r"(?:^|/)__pycache__/"` —— **`detectCiSurfaceChanges` 用 `re.match`（锚定开头）⇒ 两条 pattern 对深层路径零命中（死代码）**，探针实证后改 `.*` 前缀 / `(?:^|.*/)` 惯用法（与 conftest 行同款） |
| C-2 | 同名包目录遮蔽：`backend/app/utils/ci_shard/__init__.py` 在 `python -m` 下先于 `ci_shard.py` 执行 | pattern 增 `^backend/app/utils/ci_[^/]+/`（M206） |
| C-3 | 父包 init 先执行：`-m` 目标前先 import `app` / `app.utils` 并执行其 `__init__.py` | pattern 增 `^backend/app/utils/__init__\.py$`（M207） |
| C-4 | import 期 fs sink **赋值形态**逃逸（`_r = os.remove('x')`；顶层裸调用被结构线拦但赋值式只查拒绝根） | 三层扩展：`_DENIED_ROOTS` +~35 条（os.chdir/remove/…、sys.exit/stdout、pytest.TerminalReporter、ast/io/tempfile；判别 M208=os.chdir——不在 call-attrs ⇒ 独占）+ `_DENIED_CALL_ATTRS`（unknown 接收者 sink 成员，判别 M209=write_text / M210=unlink——**fixture 接收者必须落 unknown**：Call 接收者会先撞「动态接收者 fail-closed」既有线）⚠️ `_DYNAMIC_DENIED_SEGMENTS` 的同族扩展**实证冗余后回退**：动态根调用走「动态接收者」既有线、`Path(...)` 实测落 unknown ⇒ 段集扩展拿不到独占判别（死代码 ⇒ 删，注释固化来由） |
| C-5 | pytest reporter 再绑定假绿：`pytest.TerminalReporter = _Fake` + 抑制 FAILED 行 + `os._exit(0)` | `pytest.TerminalReporter` / `sys.stdout` 等入 `_DENIED_ROOTS`；**模块根属性再绑定** deny 块（scanExec Store 分支：root 在 aliases 或 `_isForbidden` ⇒ 拒，判别 M211=logging.getLogger 再绑定）。junitxml 证据通道重构记 §5 |
| C-6 | 分析器自盲：payload 先执行再进程内换掉 `ast.parse`/`Path.read_text` | 上游逃逸已被 C-4 deny 扩展关闭 + 腰带断言 `test_analyzer_primitives_not_swapped_in_process`（`ast.parse.__module__ == "ast"` 等，独立于扫描逻辑） |
| H-1 | Call 分派只认 Name/Attribute/Lambda/Subscript 四形态，其余静默放行（真实逃逸位：被扫函数体内 `(m if True else None)()`） | func-form 链补 fail-closed else（判别 M212=fn_callee_ifexp）；**例外臂**：func 本身是 Call（`f()(...)` 注册式）时内层 Call 经同一分派递归判定 ⇒ 外层不重复拒——首版无例外 ⇒ 校准假红 4 处（schemas.py 的 `field_validator(...)（...)`）⇒ 加例外后 DIFFS=0 |
| H-2 | 跨文件装饰器：`from app.evil import hook; @hook` —— 装饰器在 def 语句执行期运行、此前 Name 装饰器只在 callTargets 内解析 | 装饰器 fail-closed 三臂：内置白名单 {classmethod, staticmethod, property}（M215=未解析裸名）；别名解析到项目根（app./scripts.）或拒绝链 ⇒ 拒（M214=from-import 项目根）；Call/Attribute 装饰器解析根，项目根 ⇒ 拒（M213=项目属性装饰器）。**Name 装饰器只在第二段 if 链判**（第一段臂限定非 Name）——两臂同拦会让 M214 拿不到独占判别 |
| M-1 | `_GUARD_MODULE_ROOTS` 只盖 4 个守卫模块 | **接受**（§5）：扩到全 utils 会让 `ci_mutation_evidence.py` 等脚本自身 import 的合法形态假红；四根已盖「守卫自身可再绑定」主通道，残余同「普通测试文件削弱」类 |
| M-2 | CODEOWNERS 漂移：r21 行重复、`test_ci_codeowners.py` 缺 r21 且 r20 重复 | 删重复行；mutation-owner 循环与 controlledPaths 补 r21/r23（r23 重复入面问题不会再犯——两清单同源更新）；r21 docstring「M175–M202」更正为「M175–M203」（L-1） |
| L-2/L-3 | 锚 (file,original,occurrence) 唯一性未断言 / 跑者标记不钉行号 | **接受**（§5）：锚点核验步骤已含 count 核对 + 探针前置；行号钉死与重锚规矩（M69 十次重锚）成本不成比例 |

**判别点独占性（本轮新增教训）**：① `re.match` 锚定语义下「后缀类」pattern 必须带 `.*`/`(?:^|.*/)` 前缀
——首版两条死代码被探针的「基线没入面」档当场暴露；② C-4 的 sink 成员在**根条目 / call-attrs /
动态接收者 / 动态根段**四层间重叠 ⇒ 每条判别 fixture 必须选定唯一一层（os.chdir 选根条目层、
`_p.unlink()` 选 call-attrs 层——Call 接收者版 fixture 被动态接收者线罩住）；③ `__pycache__` 判别
路径必须用**非 .pyc** 文件名（否则 `.*\.pyc$` 也命中，M205 变异后仍入面）。

**校准一轮收敛**：唯一一轮假红 = H-1 无例外臂（schemas.py field_validator 链 ×4）⇒ 加嵌套 Call
例外 ⇒ **DIFFS = 0**。

**行数**：`ci_conftest_chain.py` 1755 → **1932 行**（§5 行债更新；「下次触碰顺手拆」策略不变）。
**测试**：`test_ci_conftest_chain.py` 148 → **164 条**（+9 evil +3 clean + C-6 腰带断言；
`os_chdir_assign` fixture 专配 `_DENIED_ROOTS` 根条目层的独占判别）；`test_ci_surface.py` +1 条
round23 夹带路径 lever（6 路径）。**变异**：新增 `ci_mutation_cases_r23.py`（M204–M215，12 组），
共 **214** 组；M69 **第十次重锚**（pattern 行 `|_r23`）。

## 3. 变更清单

| 文件 | 动作 |
|---|---|
| `backend/app/utils/ci_gh.py` | 重写：新增 Trees API 链路（`fetchTreePathShas` / `_parseTreePathShas` / `_buildTreeFileMap` / `_assertTreeIsRecursive` / `_diffTreePathShas` / `fetchNetChangedFiles`）、`fetchMergeBaseSha` / `_parseMergeBaseSha`；`fetchChangedFiles` 改为「merge-base 树 vs head 树」；**删除** `_parseChangedFiles` 与 300 项上限逻辑；所有外部输入加形态校验（repo / run id / sha / branch）。**第三版**：`RunHeadInfo` 增 `event` / `createdAt` / `runStartedAt` 三字段；新增 `mainTipCutoff` / `fetchMainTipShaAt` / `_parseMainTipSha` / `_considerMainRun`（`M_T` 的取数链路）与 `_MAIN_TIP_GRACE_SECONDS = 120`。**第四版（CRITICAL-1）**：`mainTipCutoff` 的**锚点从 `run_started_at` 换成 `created_at`（事件派发时刻）**、参数改成 **keyword-only**（`createdAt=` / `runStartedAt=`）⇒ 杜绝「位置传参写反」；`created_at` 缺失才回退 `run_started_at`，两者皆缺 ⇒ 抛错（判定不出来）；**删除**第二版整条「测试合并 commit」链路（`PullMergeInfo` / `fetchPullMergeInfo` / `_findOpenPullNumber` / `_parseOpenPullNumbers` / `_fetchPullMergeCommitSha` / `_fetchCommitTreeAndParent`）——`refs/pull/N/merge` 是临时 ref，**回溯历史 run 时取不到**（§1.3）。**第五版**：**删除** `_MAIN_TIP_GRACE_SECONDS`（宽限 ⇒ 与闸门侧不对称 ⇒ 假拦）；`mainTipCutoff` **只接 `createdAt`**（**删掉** `runStartedAt` ⇒ 缺失即 fail-closed，不回退）；新增 `fetchMainFirstParentTipAt` / `_parseCommitEntries`（**第一父链腿**）+ `_MAX_FIRST_PARENT_HOPS`（超限报错） |
| `backend/app/utils/ci_surface.py` | `CiSurfaceInfo` 增 `netChangedFiles` / `unattributedCiFiles` / `baseTrustError` / `mainAdvanceFiles` / `mainTipSha`；新增 `_baseTrustError`（三条硬条件）。**第三版**：判定集合改为 `A ∪ P`（`_buildSurface` 里 `judgment = set(mainAdvance) | set(prChanged)`）；新增 `_fetchMainTipSha` / `_fetchMainAdvanceFiles` / `_fetchPrChangedFiles`；**删除**第二版的 `_fetchMergeInfo` / `_fetchAttributionFiles` / `_fetchComparedFiles` 与 `prMergeSha` / `prMergeBaseSha` 两字段。**第四版（CRITICAL-1）**：`_fetchMainTips` 的 cutoff 改用 `mainTipCutoff(createdAt=prInfo.createdAt)`（**具名传**）⇒ 锚点与工具侧一致。**第五版**：`_CI_SURFACE_PATTERNS` 增**前端 CI 定义**路径（package / lock / `*.config.*` / `tsconfig*.json` / setup 文件）⇒ 堵住「前端绕过链」；新增 `_baselineAncestorError`（祖先核对 ⇒ 否则 fail-closed 退出码 3）与 `_fetchMainTips` 的**两腿并集**（run 腿 + 第一父链腿）。**第六版（评审 C-1）**：`_CI_SURFACE_PATTERNS` 再增 `vitest.workspace.*` / `vitest.projects.*`（vitest 2.x **自动发现**、存在时取代根配置的项目解析）+ `*.tsbuildinfo` + `frontend/.npmrc`（§2.10.7）；**第四轮评审（HIGH-1）**：lockfile 交替组加 `npm-shrinkwrap.json`（npm 语义下**优先于** package-lock ⇒ 只新增一个文件就能换掉前端依赖树）（§2.10.8）。**第五轮安全评审（§2.10.9）**：增 `backend/alembic/env.py`（CI 真正跑迁移的任意 Python）、`backend/.env` 家族（conftest `override=True` 覆盖 CI 注入变量）、`backend/.python-version` / `uv.toml`（uv 选解释器盖过 setup-python）、`frontend/.env` 家族、`backend/scripts/seed_*.py` 通配；supports 行扩为 `_pg_support|_neo4j_support|_testapp|milvus_isolation`。**第六轮复审（§2.10.10）**：增 `backend/alembic/versions/*.py` 通配（`upgrade head` 逐个执行的脚本本体）、仓库根 `uv\.toml`（uv 父链发现）、seed pattern 扩成 `backend/(?:scripts/)?seed_…`（conftest 模块级 import 的 `backend/` 根同族）、`backend/scripts/kpi_metric_db_guard\.py`（`assertTestDatabase` 的依赖）。**第七轮复审（§2.10.11）**：versions pattern 放宽 `[^/]+`（alembic 文件名接受面）、增 `backend/scripts/__init__.py` 与 `backend/app/tests/(?:[^/]+/)*__init__\.py$` 通配。**第八轮复审（§2.10.12）**：增 `backend/__init__\.py$`（现在不存在，但新增它就把 pytest prepend 的 pkg_root 顶到仓库根 ⇒ 收集前执行 —— 形态钉管未来文件）。**第九轮复审（§2.10.13）**：增 `backend/app/__init__\.py$`（prepend 链上**现存且每次执行**的最高一层包 init —— 第八轮钉了不存在的、漏了在链上的）与 conftest 收集前 import 的 harness 配置文件（config / infrastructure 包 init 与 database / security 包 init 与 crypto）；utils enum 增 `conftest_chain`（新派生器模块入清单）。**第十二轮复审（§2.10.14）**：增 4 条**祖先包 init** pattern（`app/domain/__init__\.py$` / `app/models/__init__\.py$` / `app/services/__init__\.py$` / `app/infrastructure/llm/__init__\.py$`）—— `from app.domain.models import X` 会先执行祖先 init 链，落点由派生器整链解析、各自一条 pattern ⇒ 删除型变异可逐个钉（M96–M99）。**第十三轮复审（§2.10.15）**：mutation 清单 pattern 放宽为 `cases(?:_core|_r14)?`（新数据文件 `ci_mutation_cases_r14.py` 入清单；M69 重锚）。**第十六轮（§2.10.16）**：pattern 再放宽 `|_r16`（`ci_mutation_cases_r16.py` 入清单；M69 第六次重锚）。**第十八轮（§2.10.17）**：pattern 再放宽 `|_r18`（`ci_mutation_cases_r18.py` 入清单；M69 第七次重锚）。**第二十轮（§2.10.18）**：pattern 再放宽 `|_r20`（`ci_mutation_cases_r20.py` 入清单；M69 第八次重锚）。**第二十二轮（§2.10.19）**：pattern 再放宽 `|_r21`（`ci_mutation_cases_r21.py` 入清单；M69 第九次重锚）。**第二十四轮（§2.10.20）**：pattern 再放宽 `|_r23`（M69 第十次重锚）+ 新增**四类夹带路径** pattern——`.*\.pyc$` / `(?:^|.*/)__pycache__/`（C-1 字节码）、`^backend/app/utils/ci_[^/]+/`（C-2 同名包目录）、`^backend/app/utils/__init__\.py$`（C-3 父包 init）；⚠️ 首版两条写错 `re.match` 锚定语义（后缀类 pattern 无 `.*`/`(?:^.*/)` 前缀 ⇒ 深层路径零命中死代码），探针「基线没入面」档当场暴露后修正 |
| `backend/app/utils/ci_failure_set.py` | 判定集合改为 `detectCiSurfaceChanges(surface.netChangedFiles)`；`_ciSurfaceReason` 分「PR 改了」/「归因不到本 PR」两种措辞；豁免前置条件改用 `netChangedFiles`；基线不可信 ⇒ 退出码 3；报告 notes 增「归因不到本 PR」条目。**第三版**：`_mergeBaseMismatchNote` 改用 `surface.mainTipSha`（不再是 `prMergeBaseSha`）。**第五版**：底座不一致的**事实与处置拆成两条 note**（`_mainTipMismatchNote` / `_mainTipRemedyNote`）⇒ 处置不被 200 字符截断；**处置改为「推一个新 commit 到 PR 分支」**（不复跑 ⇒ 复跑无效，见 §2.9）；底座不一致的**事实不再拼进 reason 字符串** |
| `.github/workflows/merge-gate.yml` | 基线 run 查询限定 `event=push` + jq 复核 `.event == "push"`。**第二版**：基线取法加**时间上界** —— 只认 `created_at <= PR run 的时间戳` 的 main push run（用**独立 jq 二进制**的 `--arg` 传时间戳；`gh --jq` 不支持 `--arg`）；**`PR_RUN_CREATED_AT` 为空 ⇒ 显式 fail-closed**。**第三版**：时间戳取 `run_started_at`。**第四版（CRITICAL-1）**：改取 **`created_at`（事件派发时刻）** —— `PR_RUN_CREATED_AT: ${{ github.event.workflow_run.created_at }}`（合并 commit 在派发时冻结 ⇒ 复跑不动它 ⇒ 上界必须锚派发时刻，详见 §2.7.1） |
| `.github/CODEOWNERS` | 新增 `backend/app/tests/unit/test_ci_main_tip_note.py` / `test_ci_main_tip.py` / `test_ci_mutation_evidence.py` / `test_ci_failure_set_surface.py` 与 `backend/scripts/ci_mutation_cases.py` 的 CODEOWNER（新增守卫文件**必须**有 CODEOWNER ⇒ 否则 `test_ci_codeowners.py` 的反向守卫会红 ⇒ 缺口本身会挡住合并）。**第五版**：新增 **`frontend/` 前缀条目**（`frontend/vite.config.ts` / `frontend/vite.config.js` 等）⇒ 堵住「前端 CI 定义改了没人复核」的绕过链（安全评审 CRITICAL-1）；并给新守卫 `backend/app/tests/unit/test_ci_surface_frontend_reverse.py` 加 CODEOWNER。**第六版（评审 C-1）**：增 `frontend/vitest.workspace.*` / `frontend/vitest.projects.*` **通配**条目（管**未来文件**）与 `frontend/tsconfig.node.tsbuildinfo` / `frontend/.npmrc`（§2.10.7）；**第四轮评审（HIGH-1）**：增 `frontend/npm-shrinkwrap.json`（§2.10.8）。**第五轮安全评审（§2.10.9）**：增 `backend/alembic/env.py`、`backend/.env` / `backend/.env.*`、`backend/.python-version` / `backend/uv.toml`、`frontend/.env` / `frontend/.env.*`、`backend/app/tests/_pg_support.py` / `_neo4j_support.py` / `_testapp.py` / `milvus_isolation.py` 与 `backend/scripts/seed_*.py`（这批此前**无授权落点**）。**第六轮复审（§2.10.10）**：增 `backend/alembic/versions/*.py`（通配管未来迁移）、`/uv.toml`、`backend/seed_*.py`（根级同族）与 `backend/scripts/kpi_metric_db_guard.py`。**第七轮复审（§2.10.11）**：增 `backend/scripts/__init__.py`、`backend/app/tests/__init__.py` + `backend/app/tests/**/__init__.py`。**第八轮复审（§2.10.12）**：增 `backend/__init__.py`（现在不存在 —— 形态钉管未来文件）。**第九轮复审（§2.10.13）**：增 `backend/app/__init__.py`（链上现存却被漏的最高一层包 init）与 `backend/app/config.py` / `backend/app/infrastructure/__init__.py` / `backend/app/infrastructure/database.py` / `backend/app/infrastructure/security/__init__.py` / `backend/app/infrastructure/security/crypto.py`（conftest 收集前 import 链上的 harness 配置文件）；增 `backend/app/utils/ci_conftest_chain.py`（它决定守卫看到哪些落点 ⇒ 改它就能让守卫漏报）与 `backend/app/tests/unit/test_ci_conftest_chain.py`（派生守卫本体）。**第十二轮复审（§2.10.14）**：增 4 条祖先包 init 条目（`backend/app/domain/__init__.py` / `backend/app/models/__init__.py` / `backend/app/services/__init__.py` / `backend/app/infrastructure/llm/__init__.py`）+ 新派生守卫 `test_every_ci_surface_file_in_the_pre_collection_closure_has_a_codeowner`（闭包 ∩ CI 面 ⇒ 必有生效 owner，堵两份人工清单漂移）。**第十三轮复审（§2.10.15）**：增 `backend/scripts/ci_mutation_cases_r14.py`（新数据文件必须同进授权面；对应反向守卫循环与 controlledPaths 各 +1）。**第十六轮（§2.10.16）**：增 `backend/scripts/ci_mutation_cases_r16.py`（同上；反向守卫循环与 controlledPaths 各 +1）。**第十八轮（§2.10.17）**：增 `backend/scripts/ci_mutation_cases_r18.py`（同上；反向守卫循环与 controlledPaths 各 +1）。**第二十轮（§2.10.18）**：增 `backend/scripts/ci_mutation_cases_r20.py`（同上；反向守卫循环与 controlledPaths 各 +1）。**第二十二轮（§2.10.19）**：增 `backend/scripts/ci_mutation_cases_r21.py`（同上；反向守卫循环与 controlledPaths 各 +1）。**第二十四轮（§2.10.20）**：增 `backend/scripts/ci_mutation_cases_r23.py`（同上）；**修复 M-2 漂移**——r21 条目在本文件重复一行（删）、`test_ci_codeowners.py` 的 mutation-owner 循环缺 r21 且 controlledPaths 里 r20 重复（补齐去重） |
| `backend/scripts/ci_mutation_cases_core.py`（**第六版收尾新建**，评审 M-3） | M1–M36（继承批 + 模块拆分批 + workflow 批 + 净变更批）的纯数据；与 `ci_mutation_cases.py` **同族** ⇒ 同进 `ci_surface.py` 清单（enum 增 `cases_core`）与 CODEOWNERS、`test_ci_codeowners.py` 的按名断言。**第九轮复审**：M27 锚点同步为加宽后的 utils enum 行原文（行加 `conftest_chain` ⇒ 不改则锚点找不到） |
| `backend/scripts/ci_mutation_evidence.py` | M27 锚点对齐现状；新增 M29–M36 八组；**第二版新增 M37–M47**；**第三版重锚 M37–M47 并新增 M48–M55**；**第四版重锚 M46/M48/M49/M50/M39 并新增 M56–M58**；**第五版重锚 M48/M49/M50/M57/M58 并新增 M59–M65**（共 **65** 组）。**第六版新增 M66–M67（共 67 组，§2.10.7）**；**第四轮评审新增 M68（共 68 组，§2.10.8）**；**第五轮安全评审新增 M69–M76（共 76 组，§2.10.9）**；**第六轮复审新增 M77–M80（共 80 组，§2.10.10）**。并把 `run_case` 的判据从**布尔**改成**三态**（`red` / `green` / `inconclusive`）——只有 pytest 退出码 **1** 才算「转红」，2/3/4/5 一律判「没跑起来」⇒ 取证失败（不是转红）。**第五版**：新增 `_failureMarkerFound` ⇒ 退出码 1 时**还必须**在输出里找到**这条用例自己的**失败标记（`FAILED <nodeid>` 整行匹配）⇒ 否则「别的用例失败」会被算成「这条守卫转红」（代码评审 MEDIUM-4）。**收尾拆分**：清单（55 组数据）移到 `backend/scripts/ci_mutation_cases.py`，本文件只留**跑者**（改坏 → 跑用例 → 判三态）——拆分前 826 行 ⇒ 超 800 行上限 ⇒ 拆后 159 + 695 行 |
| `backend/scripts/ci_mutation_cases.py` | **收尾新建**：变异的**纯数据**清单（与跑者**同族** ⇒ 同进 CI 相关面 + CODEOWNERS）；**第四版**增到 58 组，**第五版**增到 **65** 组，**第六版**增到 **67** 组（M66/M67，§2.10.7）。**第六版收尾拆分（评审 M-3）**：M1–M36 移到 `ci_mutation_cases_core.py`（`MUTATIONS = MUTATIONS_CORE + [...]`），本文件留 M37 起 ⇒ 拆后 499 + 393 行（拆前 893 行）；拆分不碰任何变异锚点 ⇒ 取证无需重锚。**第四轮评审**：新增 **M68**（§2.10.8，共 **68** 组）。**第五轮安全评审**：新增 **M69–M76**（§2.10.9，共 **76** 组）。**第六轮复审**：M76 锚点同步为扩宽后的 seed 行原文（行本身扩宽 ⇒ 不改则锚点找不到）+ 新增 **M77–M80**（§2.10.10，共 **80** 组）。**第七轮复审**：M77 锚点同步为放宽后的 versions 行原文 + 新增 **M81–M83**（§2.10.11，共 **83** 组）。**第八轮复审**：新增 **M84**（§2.10.12，共 **84** 组）。**第九轮复审**：新增 **M85–M91**（§2.10.13，共 **91** 组）。**第十二轮复审**：新增 **M92–M99**（§2.10.14，共 **99** 组；M92–M95 打派生器/闭包/检测器/递归四个实现点，M96–M99 逐条删 4 条祖先 init pattern）。**第十三轮复审**：M95 重锚（`_moduleScopeStatements` 增 Match/TryStar ⇒ 派生行原文变了）；MUTATIONS 拼接改为 `MUTATIONS_CORE + R14_MUTATIONS + [...]`，新增 **M100–M121**（§2.10.15，共 **121** 组）来自 `ci_mutation_cases_r14.py`。**第十六轮复审**：重锚 6 条（M95/M100/M111/M112/M116/M117 —— `findImportTimeHooks` 重写后行形态变了）+ M69 第六次重锚（pattern 行 `|_r16`）+ 新增 **M122–M136**（§2.10.16，共 **136** 组）来自 `ci_mutation_cases_r16.py`。**第十八轮复审**：拼接增 `R18_MUTATIONS`，新增 **M137–M151**（§2.10.17，共 **151** 组）来自 `ci_mutation_cases_r18.py`；M69 第七次重锚（pattern 行 `|_r18`）。**第二十轮复审**：拼接增 `R20_MUTATIONS`，新增 **M152–M174**（§2.10.18，共 **174** 组）来自 `ci_mutation_cases_r20.py`；M69 第八次重锚（pattern 行 `|_r20`）。**第二十二轮复审**：拼接增 `R21_MUTATIONS`，新增 **M175–M203**（§2.10.19）来自 `ci_mutation_cases_r21.py`；M69 第九次重锚（pattern 行 `|_r21`）；M157 重锚（`_classMember` 串接 `ambiguity` 参数后行形态变了）。**第二十二轮全量取证收口**：M172 **退休**（其锚线与 H3 块条件严格超集，冗余 deny 块已删，§4.2.15）⇒ 共 **202** 组；四处重锚——M94（裸 `    return tuple(hooks)` 被新函数偷走 occurrence 1 ⇒ **锚漂移**，改双行前置钉 Impl 收尾）、M140/M141/M150（重写后行形态/缩进变化；mut 必须保结构有效，删 if 体/声明 ⇒ IndentationError/NameError 是「炸穿」不是「放行」）；M69 的 pattern 行锚是第九次重锚——加宽 pattern 行必须重锚钉着它的变异（第六轮起已实证四次的规矩）。**第二十四轮复审（§2.10.20）**：拼接增 `R23_MUTATIONS`，新增 **M204–M215**（§2.10.20）来自 `ci_mutation_cases_r23.py`（共 **214** 组）；M69 **第十次重锚**（pattern 行 `|_r23`） |
| `backend/scripts/ci_mutation_cases_r20.py`（**第二十轮复审新建**，§2.10.18） | M152–M174 的**纯数据**清单（23 组，与 `ci_mutation_cases.py` 同族 ⇒ 同进 CI 相关面 + CODEOWNERS）；文件头写明**两条设计性独占依赖**：① `model_rebuild` 豁免买出成员解析线变异的判别（M140/M156/M157/M159）；② `@D.deco` 成员解析收敛进 `_decoratorMember` 单点（双份同文锚不死两处）。M163 锚带 `for dec in stmt.decorator_list:` 前缀行 + 整块 if/else（substring 锚与 orphan-else 两条 solo 取证教训见 §2.10.18 表内 L-1）。**第二十二轮**：M172 退休（锚线为 H3 块的条件子集，独占变异取证不存在 ⇒ 连同冗余 deny 块一起删除，原位留注释），文件实际剩 **22** 组 |
| `backend/scripts/ci_mutation_cases_r21.py`（**第二十二轮复审新建**，§2.10.19） | M175–M203 的**纯数据**清单（29 组，与 `ci_mutation_cases.py` 同族 ⇒ 同进 CI 相关面 + CODEOWNERS）；由生成器从**活源逐锚验证 count==1** 后生成（手工维护 29 组多行锚已不现实）。文件头写明：判别点一律取**独占落点**；三条 solo 取证教训（调用藏被扫函数体 / setattr fixture 类体全干净 / 裸名与成员装饰器拆两 fixture）与 M186 判别点移到 `_resolveMember` 内部转换行（scanExec 侧冗余块已删）的来由 |
| `backend/scripts/ci_mutation_cases_r23.py`（**第二十四轮复审新建**，§2.10.20） | M204–M215 的**纯数据**清单（12 组，与 `ci_mutation_cases.py` 同族 ⇒ 同进 CI 相关面 + CODEOWNERS）。文件头写明本轮三条独占性教训：① `re.match` 锚定语义下后缀类 pattern 必须带 `.*`/`(?:^.*/)` 前缀（M204/M205 首版死代码）；② C-4 sink 成员在根条目/call-attrs/动态接收者三层间重叠 ⇒ 判别 fixture 必须选定唯一一层（`_p.unlink()` 选 call-attrs 层——Call 接收者版被「动态接收者」既有线罩住）；③ `__pycache__` 判别路径须用非 `.pyc` 文件名（否则 `.*\.pyc$` 也命中） |
| `backend/scripts/ci_mutation_cases_r18.py`（**第十八轮复审新建**，§2.10.17） | M137–M151 的**纯数据**清单（15 组，与 `ci_mutation_cases.py` 同族 ⇒ 同进 CI 相关面 + CODEOWNERS）；M147 首锚（setter 扫描行）被 solo 取证证伪 —— fixture 无独占落点（本地类定义 `__set_name__` 本身就被 def 形态抓住）⇒ 改钉 `bound = callTargets.get(value.id)` 的 lambda 别名变体（与 M139 同锚不同 fixture，M100/M101 先例）。**第二十二轮重锚**：M140/M141/M150（重写后行形态与缩进变化；M141 mut 只删钩子扫描循环、保留 `instAmbiguity` 声明——删声明 ⇒ NameError 是「炸穿」非「放行」） |
| `backend/scripts/ci_mutation_cases_r14.py`（**第十三轮复审新建**） | M100–M121 的**纯数据**清单（22 组，与 `ci_mutation_cases.py` 同族 ⇒ 同进 CI 相关面 + CODEOWNERS）；文件头带「变异组件 → 守卫用例」映射表；nodeid 用完整参数化 id（如 `test_hook_positions_are_denied[assign_value]`）。**第十八轮重锚**：M111（`_classBodyStatements` 复制了 for/yield/Match 形态 ⇒ 锚点带上 docstring 尾句 `方法体不看` 作独占落点）、M121（拒绝判定统一进 `_isForbidden`） |
| `backend/app/tests/unit/test_ci_gh.py` | 树解析 / merge-base / 三点语义 / 无 300 上限 / 改名 四个小节；树不变式 3 条；`truncated` 的载荷改成**带嵌套路径**。**第三版**：`RunHeadInfo` 增两字段后 `_headInfo` 同步；`test_parseTreePathShas_rejects_a_string_false_truncated_flag` 随文件拆分搬回本文件 |
| `backend/app/tests/unit/test_ci_main_tip.py` | **第三版新建**：`mainTipCutoff`（**第四版**：优先 `created_at` / 回退 `run_started_at` / 两者皆缺则抛错 / **必须 keyword-only 传**）、`_parseMainTipSha`（三条硬条件 + 时间窗 + 分页）、`fetchMainTipShaAt`（查询串 + `--paginate --slurp`）；**第四版新增 PR #105 复跑锚点的离线回放**（证明夹具有鉴别力：错锚给出另一个答案） |
| `backend/app/tests/unit/test_ci_surface.py` | **第三版重写**（初稿 737 行，第五/六版评审后增至 906 行，第九轮复审后 979 行 —— 超 800 上限属既有欠账，见 §5）：判定集合 = `A ∪ P`、`M_T` 的取法与 fail-closed、归因措辞。**第八轮复审（§2.10.12）**：lever 测试增 `backend/__init__.py` 形态钉（现 21 路径 / 7 形态钉）+ docstring 变异范围更正 M70–M84；`test_ci_codeowners.py` 的 `controlledPaths` 增 `backend/__init__.py`。**第九轮复审（§2.10.13）**：lever 测试增 `backend/app/__init__.py` 与 5 个 harness 配置文件（现 **27 路径** / 7 形态钉）+ 变异范围更正 M70–M90；`test_ci_codeowners.py` 的 `controlledPaths` 增这 6 个路径。**第十二轮复审（§2.10.14）**：lever 测试增 4 条祖先包 init（现 **31 路径** / 仍 7 形态钉）+ 变异范围更正 M70–M99；`test_ci_codeowners.py` 的 `controlledPaths` 增这 4 个路径 + 新派生守卫（闭包 ∩ CI 面 ⇒ 必有 owner）。**第二十四轮复审（§2.10.20）**：+1 lever 用例 `test_round23_smuggling_path_classes_are_ci_surface`（6 路径钉 C-1/C-2/C-3 四条新 pattern 的判别；`__pycache__` 判别路径用非 .pyc 文件名——否则 `.*\.pyc$` 也命中 ⇒ M205 判别不独占）
| `backend/app/utils/ci_conftest_chain.py`（**第九轮复审新建**，§2.10.13） | conftest 收集前 import 链的**派生器**：AST 扫描所有 conftest 的模块级 `app.*` / `scripts.*` 导入并解析为仓库相对文件路径 —— 逐轮枚举文件名已被实证连漏三层 ⇒ 改为派生，conftest 新增模块级 import 必须改守卫（守卫在 CI 相关面 + CODEOWNERS）。同进 utils enum + CODEOWNERS + **M91**（掏空派生器 ⇒ 派生守卫须红）。**第十二轮复审重写（§2.10.14，84 → 277 行）**：`_resolveModuleFiles` 解析**整条祖先包 init 链**（HIGH-1）；新增 `_moduleScopeStatements`（If/Try/With/AsyncWith/For/While 分支递归展开 + 类体）与 `pytest_plugins` 字符串/清单解析（MEDIUM-3）；新增 `transitivePreCollectionPaths`（沿 import 边 BFS 到不动点的传递闭包，实测 76 文件）；新增 `findImportTimeHooks`（语句白名单 + 钩子机制根链 denylist 的 import 期代码检测器，顶层裸表达式只放行 `model_rebuild`；76 文件实测 0 钩子）；M92–M95 分别钉四个实现点。**第十三轮复审重写（§2.10.15，277 → ~480 行）**：HIGH-1 表达式位置全覆盖 —— deny 扫描从「模块级语句列表」改为经 `_iterExecNodes`（跳过函数/Lambda 体，递归装饰器/默认值/注解）**统一覆盖所有语句的 import 期表达式**；HIGH-2 别名链 —— `_aliasMap` 映射 `import x as y` 与 `from m import a as b`，`_denyMessage` 按 Load（解析直呼/别名链）/ Store/Del（只打别名导入再绑定）分治（校准证明 Pydantic `input: str = Field(...)` 会假红）；denylist 扩到 **30 根**（`sys.settrace`/`os.system`/`ctypes`/`socket` 等）+ 类创建钩子（`metaclass=` / `__init_subclass__` / `__set_name__` / `__class_getitem__`）；HIGH-3 派生边泛化 —— 相对 import（`node.level` 任意）、`from X import *` 的 `__all__` 子模块、`import tools.x` 任意顶层前缀都进闭包（删除 `_DERIVED_TOP_PREFIXES` 硬编码）；`_moduleScopeStatements` 增 `ast.Match` / `TryStar`；MEDIUM-3 弱解（闭包止于 conftest 链 76 文件）记 §5。**第十六轮重写（§2.10.16，~480 → 649 行）**：表达式轴改**拒绝扫描单轴**（`_iterExecNodes` 全子树含 vararg/kwarg 注解）；别名传播（模块根重绑定）+ `__dict__` 短路 + 拒绝根星号导入；类创建钩子全深度扫描 + `kw.arg is None` + 基类 Call/Starred + Store 再绑定（`_REBIND_DENIED_ATTRS`）；`scanExec`/`scanCallee` 被调解析（顶层 def/lambda 一层、深度 8；嵌套 def 不解析 = 文档化局限）；PEP 562 模块 `__getattr__`；双形态双叶子；动态 `__all__` RuntimeError；结构白名单 guard 移除 + 补 4 语句形态（denylist 34 根）。**第十八轮重写（§2.10.17，649 → 923 行，超 800 上限记 §5）**：统一 `_isForbidden`（禁止 dunder 段集 8 段 + `__builtins__` 根）；`findImportTimeHooks` 语句序 `callTargets`（def→lambda→名字→实例四类绑定，applyBinding 先于 scanExec）+ 本地类实例化/成员调用/嵌套类/装饰器成员/with/for/就地运算/三参 `type()` 的 import 期执行路径 + 类创建钩子 Assign/AnnAssign 双形态 + 接收者感知白名单（`_receiverMember` 本地类成员体扫描）+ PEP 562 赋值/注解形态。**第二十轮重写（§2.10.18，923 → ~1030 行，§5 行债更新）**：`_resolveMember` 改**三态**返回 `(member, status)` ∈ local/unknown/dynamic —— 实例变量/构造器/下标/本地工厂四臂补齐 + 本地未定义成员 fail-closed（`_ALLOWED_EXPR_METHODS` 豁免例外）；构造器属性链（`logging.getLogger('x').setLevel(...)`）链根 opacity 判定；`_dynamicRootForbiddenSegment`（动态根属性链禁段）+ `__setattr__` 段 + `sys.meta_path` 根；类体 Name 值绑定 / staticmethod 解包 / `typeAliases`（三参 type 别名）；`ambiguousNames`/`ambiguousClasses` 条件重定义歧义；主循环装饰器预扫先于 applyBinding（旧名解析）+ 动态装饰器拒绝 + `_decoratorMember` 单点；实例协议路径（下标/真值/实参/属性读，Call 入口 `resolvedAttrIds` 先序登记防读取通则罩变异）+ PEP 562 walk/from-import 别名 + 被调链深报错保留。**第二十二轮重写（§2.10.19，~1030 → 1762 行，§5 行债更新）**：`_GUARD_MODULE_ROOTS` 守卫自根 + `_DYNAMIC_DENIED_SEGMENTS` 动态根危险段；`classAliases`/`opaqueNames`/`typeAliases` 三套绑定集贯穿 applyBinding 与解析；`_resolveMember` 三态歧义收集（`ambiguity` 参数，歧义→dynamic 转换在函数内部单点）+ `super`/`type` 接收者臂；`_classMember` matches+unjudgeable 双通道；scanExec 五个新块（实例属性读/实例属性赋值/不透明容器下标/协议节点元组+`_usesLocalInstanceInProtocolPosition` 谓词/元组解包）+ 类体 pass（cbVars/cbInstances）+ scanCallee 重构（fnClasses 覆盖层/`scopeAliases` 函数体别名/fnProblem 逐语句/fn 局部类实例化臂）；装饰器主循环兄弟臂整体删除（scanExec 副本单点覆盖）；`_MAX_AST_NODES` 迭代预算 + RecursionError 包装。**第二十二轮全量取证收口（1762 → 1755 行）**：删除「实例属性读取」独立 deny 块——`_usesLocalInstance` 对 Name 接收者同样命中（`node.id in instanceVars` ⇒ True），其条件是 H3 块的严格子集 ⇒ 双线同拦拿不到独占变异取证（M172 退休）；原位留注释指向 H3 块。**第二十四轮复审（§2.10.20，1755 → 1932 行，§5 行债更新）**：`_DENIED_ROOTS` +~35 条（C-4/C-5/C-6：os.chdir 等 fs/进程 sink、sys.stdout/exit、pytest.TerminalReporter、ast/io/tempfile）；`_DENIED_CALL_ATTRS`（unknown 接收者 sink 成员；write/writelines 刻意排除——太通用会误伤真实闭包）；scanExec Store 分支增**模块根属性再绑定** deny（root 在 aliases 或 _isForbidden ⇒ 拒，C-5/C-6）；Call 分派 func-form 链补 **fail-closed else**（H-1；嵌套 Call 例外臂——内层经同一分派递归判定，无例外时校准假红 4 处）；装饰器 **fail-closed 三臂**（H-2：内置白名单 / 别名解析项目根 / Call-Attribute 解析根；Name 装饰器只在第二段 if 链判以保 M214 独占）；`_DYNAMIC_DENIED_SEGMENTS` 的同族 sink 扩展**实证冗余后回退**（动态接收者线 + unknown 落 call-attrs 全覆盖，段集扩展拿不到独占判别）
| `backend/app/tests/unit/test_ci_conftest_chain.py`（**第九轮复审新建**，§2.10.13） | 派生式守卫：每个派生落点**要么**在 `_CI_SURFACE_PATTERNS`、**要么**在显式豁免清单 `_EXEMPT_PRE_COLLECTION_IMPORTS`（产品代码 5 文件，附仲裁理由；改清单 = 改守卫 ⇒ 必须复核）+ 反向守卫（harness 配置文件不许进豁免、必须在清单）。**第十二轮复审重写（§2.10.14，2 → 6 条守卫）**：`test_derivation_includes_parent_package_inits`（3 个 level-1 祖先 init ∈ 派生集 + `models/__init__` ∈ 闭包且被钉，M92）；`test_transitive_closure_files_are_surface_or_hook_free`（`len ≥ 60` + `domain/models.py ∈ 闭包` 正向钉 + 逐文件「被钉或无钩子」，M93 + MEDIUM-4 豁免内容不变式）；`test_import_time_hook_detector_is_not_vacuous`（evil `sys.path.insert` 必须认出 + clean `getLogger/frozenset` 必须放行，M94）；`test_scope_recursion_and_plugins_are_derived`（合成 backend 树：try 包裹的 import 与 pytest_plugins 都被派生，M95）。**第十三轮复审扩写（§2.10.15，6 → 45 条守卫，+39）**：`test_hook_positions_are_denied`（15 个表达式/别名位置参数化，M100–M103/M116–M121）、`test_definitional_constructs_pass`（9 个放行形态，M104/M105 假红方向）、`test_structural_problem_in_every_branch_arm_is_caught`（13 个分支臂参数化含 `logging.basicConfig` 结构轴 evil，M111–M114）、`test_derivation_covers_relative_star_and_any_top_prefix`（合成树 + Import 边独占落点 `tools/solo.py` 保证 M109 有判别力）、`test_class_body_imports_are_derived`（M115）。**第十六轮扩写（§2.10.16，45 → 65 条）**：`_EVIL_POSITION_SOURCES` +19（vararg/kwarg 注解、模块根绑定、`__dict__`、星号拒绝根、`**{metaclass}`、分支内钩子 def、钩子名 Store 再绑定、基类 Call、`type.__setattr__`、被调 def、IIFE、模块 `__getattr__`、允许方法名再绑定）；`_CLEAN_SOURCES` +4（AugAssign / TypeAlias / Global / 良性 match guard）；`match_guard` fixture 改拒绝轴（M-2）；双形态派生与动态 `__all__` fail-closed 两条显式测试。**第十八轮扩写（§2.10.17，65 → 84 条，+19 evil）**：dunder 链/from-import/`__builtins__` 下标、def 别名/语句序/lambda 别名、本地类重定义 `model_rebuild`、实例化/成员调用/嵌套类/with/for/就地运算/装饰器成员/三参 `type()`、钩子赋值形态、PEP 562 赋值+注解形态、`__set_name__` 描述符；LOW-1 的 `__all__` fixture 改集合字面量（列表推导式在变异副本上抛 AttributeError ⇒ 判别点不独占）。**第二十轮扩写（§2.10.18，84 → 113 条，+24 evil +6 clean）**：`_EVIL_POSITION_SOURCES` +24（lambda_globals_spine、dynamic_receiver_local_factory、subscript_call、ctor_member_call、setattr_segment、instance_method_call、init_name_assign、staticmethod_member、type_alias_three_arg、cond_redef、class_redef、decorator_prev_binding、lambda_redef、instance_subscript、instance_truthiness、instance_as_arg、pep562_tuple_unpack、pep562_import_alias、lambda_decorator、callee_depth_cap、meta_path_install、instance_property_read、missing_member_call 等，逐条对准 M152–M174）；`_CLEAN_SOURCES` +6（str_join、imported_ctor_chain、local_model_rebuild_inherit、imported_result_call、lambda_call_plain、instance_method_clean）——clean fixture 的赋值形态是**必须**：裸顶层表达式调用被拒是设计（顶层规则），写成赋值才能让判别落在本轮修复线上；dict 声明是 AnnAssign（探针/工具 literal_eval 须双形态处理）。**第二十二轮扩写（§2.10.19，113 → 148 条，+30 evil +5 clean）**：`_EVIL_POSITION_SOURCES` +30 逐条对准 M175–M203 判别点（guard_module_import、dynamic_env_launder/path_launder、class_alias_call / class_alias_bare_decorator（裸名 `@_D`）与 class_alias_decorator（成员 `@_D.deco`，M203）、callable_instance_call / instance_decorator、ctor_property_read、super_receiver_call、instance_setattr_store、fstring_format / dict_key_hash、tuple_unpack_iter、member/hook/nested/decorator 四条歧义臂、type_alias_chain、class_body_with_instance / class_body_truthiness、fn_local_class_shadow/call/redef、callee_with_instance、fn_local_import_alias、opaque_ifexp_call / opaque_name_call / opaque_name_decorator / opaque_subscript_store、class_direct_decorator）+ `_CLEAN_SOURCES` +5（container_literal_methods、class_alias_clean、dict_value_instance、fn_local_class_clean 等）+ 两条 L1 独立用例（AST 预算 / 深嵌套 RecursionError——深嵌套向量用 5000 段属性链，缩进嵌套被 CPython 100 层硬限拦在 Parse 阶段不可用）。三条 solo 取证教训写进 fixture 注释：调用藏被扫函数体（顶层裸调用被结构白名单线兜住）、setattr fixture 类体全干净（载荷在 `__init__` 被实例化钩子线兜住）、裸名/成员装饰器拆两 fixture（`_decoratorMember` 只处理 Attribute 形态）。**第二十二轮全量取证收口**：两条 fixture 因**掩蔽路径**重设计——`getattr_via_from_import` 改 List 包裹裸名 Load（直赋值会触发模块根再绑定传播 + Store 分支拒绝；`.insert(...)` 调用链会被动态根洗白检查先拦）、`builtins_subscript` 止步于下标赋值（后续 `_b(...)` 调用触发「不透明名被调用」兜底线）⇒ 两条都会把 M137/M138 的判别线罩成假绿；`_EXPECTED_HOOK_POSITIONS` 位置归位断言（M158/M159 的判别点：兜底 fail-closed 只会拒在调用点 ⇒ 拒绝必须归位到钩子真实位置）。**第二十四轮复审（§2.10.20，148 → 164 条）**：+9 evil（os_chdir_assign / os_remove_assign / path_write_text / unknown_recv_sink / module_root_attr_rebind / sys_stdout_rebind / fn_callee_ifexp / cross_file_project_decorator / unresolvable_bare_decorator / project_attr_decorator）+3 clean（nested_call_callee / builtin_decorators / from_import_external_decorator——H-2 白名单与 H-1 例外臂的反向锚）+ C-6 腰带断言 `test_analyzer_primitives_not_swapped_in_process`；两条探针实证教训写进 fixture 注释：`fn_callee_ifexp` 宿主必须被扫（模块级 `_r = entry()` 进 callTargets，否则函数体不可达 fixture 假绿）、`unknown_recv_sink` 接收者必须落 unknown（Call 接收者被「动态接收者」既有线罩住）；全量取证净漏 5 后补 `_EXPECTED_HOOK_ROOTS` 归位判别（§4.2.18，五条精识臂被新兜底罩住 ⇒ 拒绝文本必须指认钩子根链/形态，148→164 条用例数不变、1384 → 1407 行）
| `backend/app/tests/unit/conftest.py`（**第十二轮复审改**，§2.10.14） | `import seed_ontology` 从**模块级**移入 `seedEngine` **夹具体**（HIGH-2 廉价收口）：conftest 模块级 import 在任何测试收集之前执行 ⇒ 模块级写它会把 seed_ontology 及整条链（neo4j_client / ontology_service / …）全变成收集前执行的代码；移入后实测 seed 链掉出传递闭包。conftest 本体已在 CI 面清单（任意深度 pattern）⇒ 改动有授权落点 |
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

`pytest app/tests -k 'ci_'`：**482 passed / 19 skipped / 0 failed**（第十六轮实测 414；
第二十轮实测 482。19 skipped 全部是「行为部分需要 bash ≥ 4.4」的本机限制，CI 必跑）。
（第四版基线：337 passed / 19 skipped；第九轮 370；第十二轮 375；第十三/十四轮新增 **39** 条
参数化守卫（15 位置 + 9 clean + 13 臂 + 2 派生）；第十六轮 `test_ci_conftest_chain.py` 45 → 65 条
（19 evil + 4 clean 新 fixture + 双形态/动态 `__all__` 两条显式测试）；第十八轮 `test_ci_conftest_chain.py`
65 → **84 条**（+19 evil fixture）；第二十轮 84 → **113 条**（+24 evil +6 clean，§2.10.18）；
第二十二轮 113 → **148 条**（+30 evil +5 clean + 2 L1，§2.10.19）——本机分两次跑；第二十四轮 148 → **164 条**（+10 evil +3 clean + 1 腰带，§2.10.20）
（conftest_chain + surface 190 passed；其余 ci_ 家族 446+73 passed）合计 **519 passed** ⇒
**净新增失败 0**。）

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

### 4.2.7 第七轮复审新增 3 组 + M77 重锚（M81–M83，2026-11-08）

| # | 变异 | 期望转红的用例 | 实测 |
|---|---|---|---|
| M77（重锚） | old 字符串从窄 versions 行改为**放宽后**的 `[^/]+` 行原文（行为不变：删除该行） | `test_the_round5_environment_lever_files_are_actually_flagged` | **转红** ✔ |
| M81 | 清单删掉 `backend/scripts/__init__\.py`（HIGH-1：scripts 包 init） | 同上 | **转红** ✔ |
| M82 | versions pattern **改回窄形态** `[\\w.-]+`（HIGH-2：`0141_probe+shape.py` 形态钉逃出 ⇒ 删除型 M77 钉不住收窄，必须形态型变异） | 同上 | **转红** ✔ |
| M83 | 清单删掉 `backend/app/tests/(?:[^/]+/)*__init__\.py$`（MEDIUM：测试包 init） | 同上 | **转红** ✔ |

⇒ **新增 4 组（含重锚 M77）先单独跑（2026-11-08）：4/4 转红、净漏 0**；锚点核对
**83/83** 齐全（`/tmp/n6_anchor_check.py`，从 `backend/` 跑）。
⇒ **第七轮复审后终跑（83 组，2026-11-08）：合计 83 组变异，净漏 0（§2.10.11）**

### 4.2.8 第八轮复审新增 1 组（M84，2026-11-08）

锚点核对 **84/84** 齐全（`/tmp/n6_anchor_check.py`，从 `backend/` 跑）。

| 组 | 变异 | 期望转红 | 实测 |
|---|---|---|---|
| M84 | 清单删掉 `backend/__init__\.py$`（HIGH-1：backend 包 init 把 pytest prepend 的 pkg_root 顶到仓库根） | `test_the_round5_environment_lever_files_are_actually_flagged` | **转红** ✔ |

⇒ **M84 单独跑（2026-11-08）：1/1 转红、净漏 0**；
⇒ **第八轮复审后终跑（84 组，2026-11-08）：合计 84 组变异，净漏 0（§2.10.12）**

### 4.2.9 第九轮复审新增 7 组 + M27 重锚（M85–M91，2026-11-08）

锚点核对 **91/91** 齐全（`/tmp/n6_anchor_check.py`，从 `backend/` 跑）。

| 组 | 变异 | 期望转红 | 实测 |
|---|---|---|---|
| M85 | 清单删 `backend/app/__init__\.py$`（HIGH-1：链上现存却漏的最高一层包 init） | lever 测试 | **转红** ✔ |
| M86 | 清单删 `backend/app/config\.py$` 行 | lever 测试 | **转红** ✔ |
| M87 | 清单删 `backend/app/infrastructure/__init__\.py$` 行 | lever 测试 | **转红** ✔ |
| M88 | 清单删 `backend/app/infrastructure/database\.py$` 行 | lever 测试 | **转红** ✔ |
| M89 | 清单删 `backend/app/infrastructure/security/__init__\.py$` 行 | lever 测试 | **转红** ✔ |
| M90 | 清单删 `backend/app/infrastructure/security/crypto\.py$` 行 | lever 测试 | **转红** ✔ |
| M91 | `ci_conftest_chain.py` `return result` → `return {}`（掏空派生器 ⇒ 派生守卫看到空映射） | `test_every_pre_collection_conftest_import_is_ci_surface_or_reviewed` | **转红** ✔ |
| M27（重锚） | 删 utils enum 行（已加宽含 `conftest_chain`；锚点同步为加宽后行原文） | `test_detectCiSurfaceChanges_covers_judgment_and_report_modules` | **转红** ✔ |

⇒ **新增 8 组 solo（2026-11-08）：8/8 转红、净漏 0**；
⇒ **第九轮复审后终跑（91 组，2026-11-08）：合计 91 组变异，净漏 0（§2.10.13）**

### 4.2.10 第十二轮复审新增 8 组（M92–M99，2026-11-08）

锚点核对 **99/99** 齐全（`/tmp/n6_anchor_check.py`，从 `backend/` 跑）。

| 组 | 变异 | 期望转红 | 实测 |
|---|---|---|---|
| M92 | `ci_conftest_chain._resolveModuleFiles` 祖先链 `range(1, len(parts))` → `range(0, 0)`（HIGH-1：只解析叶子 ⇒ 3 个 level-1 祖先 init 消失） | `test_derivation_includes_parent_package_inits` | **转红** ✔ |
| M93 | `transitivePreCollectionPaths` 终行 `return tuple(sorted(closure))` → `return ()`（HIGH-2：闭包掏空 ⇒ 内容不变式对空集空转） | `test_transitive_closure_files_are_surface_or_hook_free` | **转红** ✔ |
| M94 | `findImportTimeHooks` 终行 `return tuple(hooks)` → `return ()`（MEDIUM-4：检测器掏空 ⇒ 钩子全放行） | `test_import_time_hook_detector_is_not_vacuous` | **转红** ✔ |
| M95 | `_moduleScopeStatements` 递归分支 `if isinstance(node, (ast.If, ast.Try, …))` → `if False:`（MEDIUM-3：try 包裹的 import / pytest_plugins 派生消失） | `test_scope_recursion_and_plugins_are_derived` | **转红** ✔ |
| M96 | 清单删 `backend/app/domain/__init__\.py$` 行 | lever 测试 | **转红** ✔ |
| M97 | 清单删 `backend/app/services/__init__\.py$` 行 | lever 测试 | **转红** ✔ |
| M98 | 清单删 `backend/app/models/__init__\.py$` 行 | lever 测试 | **转红** ✔ |
| M99 | 清单删 `backend/app/infrastructure/llm/__init__\.py$` 行 | lever 测试 | **转红** ✔ |

⇒ **新增 8 组 solo（2026-11-08）：8/8 转红、净漏 0**；
⇒ **第十二轮复审后终跑（99 组，2026-11-08）：合计 99 组变异，净漏 0（§2.10.14）**

⚠️ **正向钉防空转的教训**（M93 差点假绿）：对「更小的派生结果」断言守卫，变异后集合变空 ⇒
断言全体通过 ⇒ 假绿。三条内容守卫各带**正向钉**：派生守卫钉 3 个具体祖先 init ∈ 派生集、
闭包守卫钉 `len ≥ 60` + `domain/models.py ∈ 闭包`、检测器钉「evil 必须认出 **且** clean 必须放行」。

### 4.2.11 第十三/十四轮复审新增 22 组 + M95 重锚（M100–M121，2026-11-08）

锚点核对 **121/121** 齐全（.py 落点全部核过出现次数、变异后文本可解析；M21–M26/M46/M56/M65
的 yml/CODEOWNERS 落点沿用历史批实证）。清单独立成 `ci_mutation_cases_r14.py`（保 800 行上限），
`ci_mutation_cases.py` 拼装 `MUTATIONS_CORE + R14_MUTATIONS + [M37–M99]`。

| 组 | 变异 | 期望转红 | 实测 |
|---|---|---|---|
| M100 | 拒绝扫描循环整段删除 `for node in _iterExecNodes(stmt)` → `for node in ()`（MEDIUM-1 点名「删除扫描循环」） | `test_hook_positions_are_denied[assign_value]` | **转红** ✔ |
| M101 | `_isDeniedChain` 恒 `return False` | 同上 | **转红** ✔ |
| M102 | `_aliasMap` 的 Import-asname 映射删除 | `[import_alias]` | **转红** ✔ |
| M103 | `_aliasMap` 的 ImportFrom 映射删除 | `[from_alias]` | **转红** ✔ |
| M104 | Name 的 Load-ctx 判定删除（**假红方向**：`input: str` 字段被误报） | `test_definitional_constructs_pass[field_named_input]` | **转红** ✔ |
| M105 | 函数/Lambda body 跳过删除（**假红方向**：方法体内钩子被误报） | `[method_body]` | **转红** ✔ |
| M106 | 相对导入解析死掉（`_absoluteModuleName` 恒 None） | `test_derivation_covers_relative_star_and_any_top_prefix` | **转红** ✔ |
| M107 | star 的 `__all__` 展开删除 | 同上 | **转红** ✔ |
| M108 | 顶层前缀硬编码重新引入（ImportFrom 边） | 同上 | **转红** ✔ |
| M109 | 顶层前缀硬编码重新引入（Import 边） | 同上 | **转红** ✔ |
| M110 | `pytest_plugins +=` 不再算插件声明 | 同上 | **转红** ✔ |
| M111 | match 臂展开删除 | `test_structural_problem_…[match_body]` | **转红** ✔ |
| M112 | except handler 臂展开删除 | `[handler_body]` | **转红** ✔ |
| M113 | try orelse 臂展开删除 | `[try_orelse]` | **转红** ✔ |
| M114 | try finalbody 臂展开删除 | `[try_final]` | **转红** ✔ |
| M115 | 类体作用域展开删除 | `test_class_body_imports_are_derived` | **转红** ✔ |
| M116 | 类创建钩子名清单清空 | `test_hook_positions_are_denied[init_subclass]` | **转红** ✔ |
| M117 | `metaclass=` 检查删除 | `[metaclass]` | **转红** ✔ |
| M118 | `model_rebuild` 裸 Name 接收者约束删除 | `[dynamic_receiver]` | **转红** ✔ |
| M119 | Attribute 链的别名查找删除 | `[import_alias]` | **转红** ✔ |
| M120 | Name(Load) 的别名查找删除 | `[from_bare]` | **转红** ✔ |
| M121 | Name(Store) 的别名查找删除 | `[alias_store]` | **转红** ✔ |

⇒ **首轮 solo 20/22、净漏 2**（M109 判别点被 `from ..tools import helper` 的同落点掩盖、
M120 的 fixture 走了 Attribute 别名路径）—— **修 fixture 后 solo 22/22 转红、净漏 0**；
⇒ 终跑（121 组）见 §4.2.10 后记。

⚠️ **判别点设计教训**（两处假判别的根因，供后续批复用）：
- 合成树里若两条导入边落点重合，杀掉其中一条边的变异不会红 ⇒ 每个「边类型」变异需要
  **只有那条边可达**的落点（`tools/solo.py` 注释里写明是 M109 判别点）；
- fixture 的钩子位置必须与被钉组件的**检测路径**唯一对应：`path.insert(...)` 同时是
  Attribute 链（M119 路径），裸 Name 判别的 fixture 要用 `path[0]` 这类**无属性**形态。

### 4.2.12 第十五/十六轮复审新增 15 组 + 6 条重锚（M122–M136，2026-11-08）

第十六轮重写了 `findImportTimeHooks`（M95/M100/M111/M112/M116/M117 六条锚点断）⇒ **先程序化重锚**
（脚本核 occurrence=1 + 变异后文本可解析，136/136 齐），再新增 15 组（清单独立成
`ci_mutation_cases_r16.py`，`ci_mutation_cases.py` 拼装 `… + R14_MUTATIONS + R16_MUTATIONS`；
`ci_mutation_` pattern 行放宽 `|_r16` ⇒ **M69 第六次重锚**）。

| 组 | 变异 | 期望转红 | 实测 |
|---|---|---|---|
| M122 | vararg 注解节点追加删除 | `[vararg_annotation]` | **转红** ✔ |
| M123 | kwarg 注解节点追加删除 | `[kwarg_annotation]` | **转红** ✔ |
| M124 | 别名传播行（`aliases[t.id] = source`）删除 | `[module_root_binding]` | **转红** ✔ |
| M125 | `__dict__` 链短路删除 | `[dict_subscript]` | **转红** ✔ |
| M126 | 拒绝根星号导入检查删除 | `[star_from_denied]` | **转红** ✔ |
| M127 | `kw.arg is None`（`**{...}` metaclass）检查删除 | `[metaclass_kwargs]` | **转红** ✔（fixture 去掉类体裸 `...` 后） |
| M128 | 类体任意深度钩子 def 扫描删除 | `[init_subclass_in_branch]` | **转红** ✔ |
| M129 | Store 再绑定检查删除 | `[hook_attr_rebind]` | **转红** ✔ |
| M130 | 基类 Call/Starred 检查删除 | `[base_call]` | **转红** ✔（fixture 改 `class Y(get_x())` 去 `__mro_entries__` def 后） |
| M131 | denylist 删 `"type.__setattr__"` | `[type_setattr_assign]` | **转红** ✔ |
| M132 | 同模块被调解析删除 | `[called_def]` | **转红** ✔ |
| M133 | IIFE lambda 分支删除 | `[lambda_iife]` | **转红** ✔ |
| M134 | 模块级 `__getattr__` 检查删除 | `[module_getattr]` | **转红** ✔ |
| M135 | 双形态叶子改回二选一（`elif`） | `test_dual_form_module_derives_both_leaves` | **转红** ✔ |
| M136 | 动态 `__all__` 抛错条件关闭 | `test_dynamic_all_raises_fail_closed` | **转红** ✔ |

⇒ **首轮 solo 13/15、净漏 2**（M127/M130 的 fixture 里类体裸 `...` 被结构检查顺手抓住
⇒ 判别点不独占 —— 与 §4.2.11 的教训同族：**fixture 里不能有任何与被钉检测路径无关的
违规**）；修 fixture 后 **solo 15/15 转红、净漏 0**。终跑（136 组）见下。

### 4.2.13 第十七/十八轮复审新增 15 组 + 4 条重锚 + M69 第七次重锚（M137–M151，2026-11-08）

第十八轮重写了 `findImportTimeHooks`（统一 `_isForbidden` / 语句序 `callTargets`）与
拒绝清单 ⇒ **先程序化重锚**（脚本核 occurrence=1 + 变异后文本可解析，151/151 齐）：
M121（r14）、M125/M132（r16）三条锚点断 + M111 撞号（`_classBodyStatements` 复制了
for/yield/Match 形态 ⇒ 锚点带 docstring 尾句）。再新增 15 组（清单独立成
`ci_mutation_cases_r18.py`，`ci_mutation_` pattern 行放宽 `|_r18` ⇒ **M69 第七次重锚**）。

| 组 | 变异 | 期望转红 | 实测 |
|---|---|---|---|
| M137 | Name(Load) 的 `_isForbidden` 判定删除 | `[getattr_via_from_import]` | **转红** ✔ |
| M138 | denylist 删 `"__builtins__"` 根 | `[builtins_subscript]` | **转红** ✔ |
| M139 | Name-value 绑定传播行关闭 | `[called_def_alias]` | **转红** ✔ |
| M140 | 本地类成员解析（接收者感知白名单）删除 | `[model_rebuild_local_override]` | **转红** ✔ |
| M141 | 实例化钩子循环关闭 | `[class_instantiation_init]` | **转红** ✔ |
| M142 | 类创建钩子赋值形态检查关闭 | `[init_subclass_assign]` | **转红** ✔ |
| M143 | with 的 context_expr 检查关闭 | `[with_local_instance]` | **转红** ✔ |
| M144 | 三参 `type()` 参数个数判别关闭 | `[type_three_arg]` | **转红** ✔ |
| M145 | PEP 562 赋值/注解形态检查关闭 | `[module_getattr_assign]` | **转红** ✔ |
| M146 | 嵌套类成员分支关闭 | `[nested_class_instantiation]` | **转红** ✔ |
| M147 | Name-value 绑定传播行关闭（lambda 源变体） | `[called_lambda_alias]` | **转红** ✔（首锚 setter 行被 solo 取证证伪 —— fixture 无独占落点，改锚） |
| M148 | AugAssign 臂关闭 | `[augassign_local_instance]` | **转红** ✔ |
| M149 | For-iter 检查关闭 | `[for_local_instance]` | **转红** ✔ |
| M150 | 装饰器成员解析删除 | `[decorator_class_member]` | **转红** ✔ |
| M151 | 主循环 `applyBinding(stmt)` 删除（语句序） | `[called_def_before_redef]` | **转红** ✔ |
| 重锚 M111 | `_moduleScopeStatements` 的 match 臂展开（新锚带 docstring 尾句） | `[match_body]` | **转红** ✔ |
| 重锚 M121 | Store 别名的 `_isForbidden` 判定 | `[alias_store]` | **转红** ✔ |
| 重锚 M125 | `_isForbidden` 的禁止 dunder 段检查 | `[dict_subscript]` | **转红** ✔ |
| 重锚 M132 | 语句序 `callTargets` 的被调解析 | `[called_def]` | **转红** ✔ |

⇒ **首轮 solo 18/19、净漏 1**（M147：`set_name_via_descriptor` fixture 无独占落点 ——
本地类定义 `__set_name__` 本身就被类创建钩子 def 形态检查抓住，setter 扫描分支对本地类
永不可能先于 def 形态触发 ⇒ 纯纵深防御无独立判别点）；M147 改锚 lambda 别名变体后
**solo 19/19 转红、净漏 0**。终跑（151 组）：**151/151 转红、净漏 0**（2026-11-09 终跑，exit 0；输出末行「合计 151 组变异，净漏 0」）。

### 4.2.14 第十九/二十轮复审新增 23 组 + 6 条重锚 + M69 第八次重锚（M152–M174，2026-11-09）

第二十轮重写了 `_resolveMember`（三态 fail-closed）与主循环装饰器预扫 ⇒ **先程序化重锚**：
M140/M141/M145/M146/M150（r18）五条锚点断 + M133（r16）一条（IIFE 臂 if→elif）。再新增
23 组（清单独立成 `ci_mutation_cases_r20.py`，pattern 行放宽 `|_r20` ⇒ **M69 第八次重锚**）。

| 组 | 变异 | 期望转红 | 实测 |
|---|---|---|---|
| M152 | 动态根禁段扫描行关闭 | `[lambda_globals_spine]` | **转红** ✔ |
| M153 | 动态接收者 fail-closed 臂关闭 | `[dynamic_receiver_local_factory]` | **转红** ✔ |
| M154 | 下标取函数调用臂关闭 | `[subscript_call]` | **转红** ✔（fixture 须赋值形态，见下） |
| M155 | `__setattr__` 禁段条目删除 | `[setattr_segment]` | **转红** ✔ |
| M156 | 实例变量接收者臂关闭 | `[instance_method_call]` | **转红** ✔（Call 入口 resolvedAttrIds 前序登记后） |
| M157 | 构造器接收者成员解析关闭 | `[ctor_member_call]` | **转红** ✔（fixture 须赋值形态） |
| M158 | 类体 Name 值绑定臂关闭 | `[init_name_assign]` | **转红** ✔ |
| M159 | staticmethod/classmethod 解包臂关闭 | `[staticmethod_member]` | **转红** ✔ |
| M160 | 三参 type 别名臂关闭 | `[type_alias_three_arg]` | **转红** ✔ |
| M161 | def 条件重定义登记关闭 | `[cond_redef]` | **转红** ✔ |
| M162 | 类重定义歧义臂关闭 | `[class_redef]` | **转红** ✔ |
| M163 | 主循环装饰器预扫整块关闭 | `[decorator_prev_binding]` | **转红** ✔（两次证伪后定锚，见下） |
| M164 | 实例形态元组清空 | `[instance_subscript]` | **转红** ✔ |
| M165 | 实例真值检查关闭 | `[instance_truthiness]` | **转红** ✔ |
| M166 | 实例实参扫描关闭 | `[instance_as_arg]` | **转红** ✔ |
| M167 | PEP 562 walk 臂关闭 | `[pep562_tuple_unpack]` | **转红** ✔ |
| M168 | PEP 562 from-import 别名臂关闭 | `[pep562_import_alias]` | **转红** ✔ |
| M169 | 动态装饰器拒绝臂关闭 | `[lambda_decorator]` | **转红** ✔ |
| M170 | 被调链深报错行关闭（cap 还在、只是不报） | `[callee_depth_cap]` | **转红** ✔ |
| M171 | `sys.meta_path` 根条目删除 | `[meta_path_install]` | **转红** ✔ |
| M172 | 实例属性读臂关闭 | `[instance_property_read]` | **转红** ✔ |
| M173 | lambda 条件重定义登记关闭 | `[lambda_redef]` | **转红** ✔ |
| M174 | 未定义成员豁免例外检查关闭 | `[missing_member_call]` | **转红** ✔ |
| 重锚 M140 | `_classMember` 本地类返回行 | `[model_rebuild_local_override]` | **转红** ✔ |
| 重锚 M141 | 实例化钩子循环（多行锚） | `[class_instantiation_init]` | **转红** ✔ |
| 重锚 M145 | `_pep562BindingProblem` 调用行 | `[module_getattr_assign]` | **转红** ✔ |
| 重锚 M146 | 嵌套类成员 elif 臂 | `[nested_class_instantiation]` | **转红** ✔ |
| 重锚 M150 | `_decoratorMember` 返回行（单点化后） | `[decorator_class_member]` | **转红** ✔ |
| 重锚 M133（r16） | IIFE lambda 臂（if→elif） | `[lambda_iife]` | **转红** ✔ |
| 重锚 M118（r14） | fixture 改构造器接收者形态（锚不变：顶层豁免的裸名接收者约束） | `[dynamic_receiver]` | **转红** ✔（见下文净漏 2） |
| 重锚 M131（r16） | 根条目 `type.__setattr__` → `setattr`（原条目被 `__setattr__` 禁段 supersede） | `[builtin_setattr_call]`（fixture 同步重写） | **转红** ✔（见下文净漏 2） |

⇒ **首轮 solo 23 组净漏 5**（M154/M156/M157/M163/M174），全部经内存探针（`/tmp/n6_r20_probe.py`：
变异不落盘 ⇒ 读出检测器仍命中的 hook 消息行）定位根因后修复：

| 净漏组 | 根因 | 修复 |
|---|---|---|
| M156/M174 | Call 的 func Attribute 被「实例属性读取 ⇒ property/__getattr__」通则先判 ⇒ 成员解析线的变异被罩住 | Call 入口 `resolvedAttrIds.add(id(node.func))`——**先序遍历保证 Call 先于其 func 子节点** ⇒ 调用路径对 func Attribute 的读取有决定权，读取通则让位 |
| M157/M154 | 裸顶层表达式调用被「顶层表达式」规则另判 ⇒ fixture 转红落在别的线上（变异判别点不独占） | fixture 改赋值形态（`_r = X().model_rebuild()` / `_r = _d['f']()`） |
| M163 | **substring 锚雷**：16 空格条件行是 20 空格 scanExec 行的子串 ⇒ `src.count()==2` ⇒ 跑者替换错处；第一次改多行锚又踩 **orphan-else 雷**（只换 if 留孤儿 `else:` ⇒ SyntaxError ⇒ 非法红） | 锚带 `for dec in stmt.decorator_list:` 前缀行 + 整块 if/else 一起换；教训固化进 M163 注释 |

solo 修复后 **28/28 转红、净漏 0**。**首轮全量（174 组）再净漏 2** —— 两条**旧组**被本轮/上轮的
新拦线罩住（检测器变强 ⇒ 旧判别点失去独占性，与 M147「纯纵深防御无独占落点」同类）：

| 净漏组 | 根因（探针实评） | 修复 |
|---|---|---|
| M118 | fixture 接收者 `get_x()`（本地工厂）被第二十轮「动态接收者 fail-closed」线（M153 的线）先拦 ⇒ 顶层豁免行的「裸名接收者」约束失去独占性 | fixture 改构造器接收者形态 `X().model_rebuild()`——本地类实例化不在动态拒绝之列、成员良性 ⇒ 顶层规则的 Name 接收者约束成为唯一拦线（锚不变） |
| M131 | 第十九轮把 `__setattr__` 加进禁止 dunder 段集后，`type.__setattr__`/`object.__setattr__` 根条目被段集线 supersede ⇒ **纯纵深防御无独占判别点** | 改钉 `setattr` 裸名根（不带 dunder 段、赋值形态绕开顶层表达式规则）；fixture 重写为 `builtin_setattr_call` |

重锚后 solo **2/2 转红**。**全锚雷检**：174 组逐一核 `count(锚)==occurrence` + 编号无重复 ⇒ 0 异常
（M163 多行锚的教训由此推广成例行检查）。**校准** DIFFS = 0（三态 fail-closed 对真实 76 文件闭包
不可见）。终跑（174 组）：**174/174 转红、净漏 0**（2026-11-09 终跑，exit 0；输出末行
「合计 174 组变异，净漏 0」）。

**流程教训（第二十轮新增）**：检测器每变强一次，**旧变异组的独占性必须复验** —— 锚点存在
（count==occurrence）≠ 判别点仍独占；「新线罩旧组」的净漏只会暴露在全量终跑，solo 新增组
查不出来。`/tmp/n6_r20_probe.py`（内存变异 + hook 消息行输出）是定位「被哪条线罩住」的最短路径。

### 4.2.15 第二十一轮复审新增 29 组 + 2 条重锚 + M69 第九次重锚（M175–M203，2026-11-09）

清单在 `ci_mutation_cases_r21.py`（生成器从活源逐锚验证 count==1 后产出）；判别点全部取独占落点。
solo 前先跑锚点探针（`/tmp/n6_r21_probe.py`：29 组逐一「原检测器拦 → 内存变异后放行」），
**首轮 6 组不独占**，全数在探针阶段定位、不等全量终跑：

| 组 | 首版判别点被谁罩住（探针实评） | 处置 |
|---|---|---|
| M178 | fixture 用 `@_D.deco`（Attribute 形态）⇒ 走 `_decoratorMember`（M203 的线），scanExec 裸名装饰器臂根本没参与 | fixture 拆两个：裸名 `@_D`（⇒ M178）与成员 `@_D.deco`（⇒ M203）；`_decoratorMember` 只处理 Attribute 形态 ⇒ 裸名只走装饰器臂 |
| M179 | 顶层裸 `_x()` 被「顶层表达式」结构白名单线兜住（与 r20 M154 同族教训） | 调用藏进被扫函数体（装饰器函数 `_d` 的体）——顶层结构白名单只看模块级语句 |
| M183 | 载荷在 `__init__` ⇒ 实例化钩子线（`_instantiationHooks`）先拦 | 类体改全干净，判别点= 赋值语句本身的 fail-closed 拒绝 |
| M186 | scanExec 的 `if memberAmbiguity:` 块与 `if status == "dynamic":` **全冗余**——`_resolveMember` 三处歧义点各自紧跟 `return None, "dynamic"` ⇒ 歧义非空时 status 必为 dynamic | **删除冗余块**（重复线拿不到独占取证）；M186 改钉 `_resolveMember` Name 接收者臂的歧义→dynamic 转换行；fixture 判别从「成员绑定静态不可判」消息改走「动态接收者」消息（同样 fail-closed） |
| M194 | 锚 `if fnCls is None:` 变异后 None 落进 else ⇒ `_classMember(None)` AttributeError（炸穿 ≠ 转红，污染面不可控） | 改锚 fail-closed 的 add 块整体（变异= `pass`）——逃逸路径干净 |
| M196/M197 | 同 M179：顶层裸 `_g()` / `_p()` 被结构白名单线兜住 | 调用藏进被扫函数体 |

重设计后探针 **29/29 独占**。solo 取证：**29/29 转红、净漏 0**（exit 0）。生成器两处发射 bug
（`str.join` 参数形态 / 测试 id 漏 `::` 分隔 / M176 变异尾冒号重复 ⇒ SyntaxError）在聚合加载与
探针阶段暴露，未进入取证。

**流程升级（第二十二轮起）**：探针（内存变异、不落盘）前置于 solo——「新线罩新组」在生成阶段
就地暴露；全量终跑只兜「新线罩**旧**组」。

### 4.2.16 第二十二轮全量取证收口（9 处泄漏诊断修复，202 组净漏 0，2026-11-09）

M175–M203 之后的**首轮全量终跑**净漏 **9**——solo 新增组全绿但旧/次新组在全量里集体暴露，
全部用探针（`/tmp/n6_r22_diag.py` 系：内存变异 + 逐臂 deny 消息输出）定位到「被哪条线罩住」，
修复后全量重跑（202 组）**净漏 0**（exit 0）。三个新泄漏类 + 两条掩蔽路径入档：

**新泄漏类**：

| 类 | 组 | 根因 | 处置 |
|---|---|---|---|
| **锚漂移** | M94 | 新函数（`_findImportTimeHooksImpl`）插入同文收尾行 `    return tuple(hooks)` ⇒ occurrence 1 被偷走；锚点存在 ≠ 锚在正确的行上 | 双行前置锚（add 行 + return 行）钉死 Impl 收尾 |
| **冗余 deny 线**（M183/M186 后第三例） | M172 | 「实例属性读取」独立 deny 块的条件（含 `node.value.id in instanceVars`）是 H3 块 `_usesLocalInstance` 对 Name 接收者判定的**严格子集** ⇒ 双线同拦，删哪条都拿不到独占变异取证 | **删除冗余块**（1762 → 1755 行）+ M172 退休；`_x.p` 形态由 H3 块统一拒绝，fixture 保留 |
| **兜底罩精识线** | M158/M159 | 类体绑定精识臂被关 ⇒ 守卫落到通用 fail-closed（仍然拒绝，但拒在**调用点**而非钩子位置）⇒ `assert hooks` 无法区分精识线死活 | 判别点改**拒绝位置归位**：`_EXPECTED_HOOK_POSITIONS` 断言拒绝消息含钩子真实行位（"line 3"）；取证类型是位置漂移不是放行 |

**掩蔽路径**（fixture 重设计，防御同族复发）：

- M137：直赋值 `_r = __getattribute__` 触发**模块根再绑定传播**（Assign Name→Name 把别名传给 `_r`）+ Store 分支拒绝；改 `.insert(...)` 调用链又被**动态根洗白**检查先拦 ⇒ 最终形态 **List 包裹裸名 Load**（`_r = [__getattribute__]`）：List 内是 Load、无传播、无 Attribute 链。
- M138：下标赋值后追加 `_b(...)` 调用触发「不透明名被调用」兜底线 ⇒ fixture 止步于 `_b = __builtins__['exec']`。

**mut 必须保结构有效**：删除 if 体行或后续仍引用的变量声明 ⇒ IndentationError/NameError =
「炸穿」（inconclusive/非法红）不是「放行」——M94/M140/M141 的 mut 均保留前置结构行。

**终跑前证据链**：锚点核验 202/202（errors 0，含 M172 退休与四处重锚）→ 探针 6/6 拦→放行
（M94/M137/M138/M140/M141/M150；M158/M159 排除——取证类型是位置归位，solo 单独验证）→
套件 190 passed → 校准 DIFFS=0（删块对真实 76 文件闭包零行为变化）→ solo 8/8 转红净漏 0
→ **全量 202 组净漏 0**（exit 0）。

### 4.2.17 第二十三轮复审修复（6C/2H/2M/3L 全收口，202 → 214 组，2026-11-09）

第二十三轮复审（BLOCK，6 CRITICAL / 2 HIGH / 2 MEDIUM / 3 LOW）逐条修复 + 变异取证全链重跑。
修复表与验收依据：

| 级 | 发现 | 修复 | 取证 |
|---|---|---|---|
| C-1 | `.pyc` / `__pycache__` 夹带：编译产物改写守卫语义不进 CI 面 | 2 条新 pattern（`.*\.pyc$`、`(?:^|.*/)__pycache__/`） | M204/M205 |
| C-2 | 同名包目录遮蔽（`ci_x/` 目录顶掉 `ci_x.py` 守卫模块） | `^backend/app/utils/ci_[^/]+/` | M206 |
| C-3 | 父包 `__init__.py` 改动不进面 | `^backend/app/utils/__init__\.py$` | M207 |
| C-4 | 赋值形态 fs sink（`os.chdir = f`）不触发 deny | `_DENIED_ROOTS` +~35 条 + `_DENIED_CALL_ATTRS`（unknown 接收者 sink） | M208/M209/M210 |
| C-5 | `pytest.TerminalReporter` 等 reporter 重绑定逃逸 | 根 deny + 模块根属性再绑定 Store deny | M211 |
| C-6 | 分析器自致盲（`ast.parse = ...`）无法自证 | 腰带断言 `test_analyzer_primitives_not_swapped_in_process`（ast/Path.__module__） | C-5/C-6 的 Store deny 钉 |
| H-1 | func-form 链对 `f()(...)`（ifexp 被调）静默放行 | fail-closed else + **嵌套 Call 例外臂**（内层经同一分派递归判定；无例外时校准假红 4 处——`schemas.py` field_validator 链） | M212 |
| H-2 | 装饰器三臂全 fail-open | 内置白名单 `_DECORATOR_BUILTIN_ALLOW` + 项目根解析 + `_isForbidden`；Name 装饰器只在第二段 if 链判（双臂同罩 fixture ⇒ M214 判别不独占） | M213/M214/M215 |
| M-1 | `_GUARD_MODULE_ROOTS` 作用域收窄争议 | 接受（§5 记残余风险） | — |
| M-2 | CODEOWNERS 漂移（r21 重复行、r23 缺行） | 修 | CODEOWNERS 行内注释 |
| L-1/2/3 | docstring 计数过期 / 锚点唯一性 / 行号钉 | 修 / 接受（§5） | — |

**判别点独占性三课（探针实证后固化）**：

1. **`detectCiSurfaceChanges` 用 `re.match`（start-anchored）** ⇒ 后缀型 pattern 必须带 `.*` / `(?:^|.*/)`
   前缀——首版 `r"\.pyc$"` 与 `r"(?:^|/)__pycache__/"` 是**死代码**（探针「基线没入面」分支暴露）。
   ⚠️ 注释已固化进 `ci_surface.py`。
2. **Call 分派状态链决定判别层**：动态接收者（`get_handler().unlink()`）走既有「动态接收者 ⇒ fail-closed」线；
   `Path(...)` 外部构造接收者与不可解析 Name 落 unknown ⇒ `_DENIED_CALL_ATTRS` 才是判别层
   ⇒ `_DYNAMIC_DENIED_SEGMENTS` 的同族 sink 扩展（12 段）实证冗余 ⇒ **回退**，注释固化来由。
3. **判别路径不得撞既有 pattern**：`__pycache__` 的 M205 判别路径必须是**非 .pyc** 文件
   （`.*\.pyc$` 同时命中 ⇒ M205 变异不翻）；Name 装饰器只在一处 if 链判（双臂同罩 ⇒ M214 不翻）。

**证据链（终跑前）**：锚点核验 214/214（errors 0）→ 探针 12/12 拦→放行
（`/tmp/n6_r24_probe_m204_m215.py`：源码替换 + importlib 独立加载，与跑者同语义）
→ 守卫套件 164 passed → 校准 DIFFS=0（`闭包内非面文件且检出钩子 0`；真实闭包 76 文件、
装饰器 = Call×57（外部根）+ Name×69（内置 + from-import））
→ solo 12/12 转红净漏 0 → **全量 214 组净漏 0**（exit 0，§4.2.18 终跑记录）。

**行数**：`ci_conftest_chain.py` 1755 → **1932 行**（§5 行债更新）；守卫用例 148 → **164 条**；
变异 202 → **214 组**（`ci_mutation_cases_r23.py` 新文件，M69 第十次重锚——pattern 行加宽 `|_r23`）。

### 4.2.18 第二十四轮全量取证收口（净漏 5 诊断 + 锚点残留事故，214 组净漏 0，2026-11-09）

全量首跑净漏 **5**（M133/M154/M178/M180/M200）——solo 12/12 全绿但五条**旧精识臂**在全量里暴露，
全部被第二十四轮新增的 fail-closed 兜底罩住（「冗余 deny 线」**第四例**，M172/M183/M186 同族）：

| 组 | 被罩的臂 | 基线（精识） | 变异后（兜底） |
|---|---|---|---|
| M133 | func-form Lambda 臂 | `顶层引用 sys.path.insert`（钩子位置） | `被调形态静态不可判…fail-closed` |
| M154 | func-form Subscript 臂 | `下标取可调用再调用…` | 同上（报文形态枚举恰含「下标」二字） |
| M178 | classAliases 裸名装饰器臂 | `顶层引用 sys.path.insert` | `装饰器裸名静态不可判…` |
| M180 | instanceVars 装饰器臂 | 同上 | 同上 |
| M200 | localClasses 装饰器臂 | 同上 | 同上 |

**处置 = M158/M159「归位」原则从位置扩展到根链/形态文本**：`_EXPECTED_HOOK_ROOTS`
（5 fixture 钉死拒绝文本必须指认钩子根链 `sys.path` 或可执行目标形态「下标取可调用」；
M154 不能用 `sys.path`——下标臂本身拒在调用点不指认根链，也不能用裸「下标」——兜底报文恰好含该词）。
修后 solo 5/5 转红 → **全量终跑 214 组净漏 0**（exit 0）。

**⚠️ 取证运行纪律两课（run 3 事故入档）**：

1. **SIGINT 停跑者会留残留变异**：run 2 被 TaskStop+SIGINT 停在中途时，`ci_judge.py` 的
   M3 变异（整块删除）未恢复完成 ⇒ run 3 带着已变异文件起跑，M3 报「锚点 0 次」。
   **停跑者后必须用未过滤的 `git status` / `git diff --stat` 核树**——当时的核对命令
   `grep -v "^ M backend"` 恰好把后端修改行滤掉，盲区放行了残留。
2. **绝不与跑者并发跑 pytest**：共享 5434 库上 seed 撞 `uq_ontology_class_name_version`
   （run 3 等待期间的一次并发 pytest 实证）；并发还可能污染跑者判定 ⇒ 证据作废。

**终跑前证据链（run 4 终态）**：树核（`git diff --stat` 恰 10 文件 + M3 锚点 1 次）→
锚点核验 214/214 → 守卫套件 164 passed（含 `_EXPECTED_HOOK_ROOTS` 归位断言）→
solo 12/12 + 5/5 → **全量 214/214 转红、净漏 0**（exit 0）。

### 4.2.19 第二十五轮复审修复（2C/2H/2M/3L，214 → 223 组，2026-11-09）

第二十四轮复审判定 **BLOCK（2 CRITICAL / 2 HIGH / 2 MEDIUM / 3 LOW）**，全部带攻击构造，逐条修复：

| 编号 | 攻击构造 | 修复 | 判别 |
|---|---|---|---|
| C-24-1 | 装饰器整体求值逃逸：`@(hook if True else None)` / `@(lambda f: f)(hook)` / `@(h := hook)` / `@g()`（本地工厂内层闭包写文件）——全部从 `findImportTimeHooks` 返回 `()` | 预扫白名单收窄为 **Name/Attribute/Call**（M218）；Call 装饰器臂判工厂形态（lambda 臂 M220 / 嵌套调用结果 / 链不可判 ⇒ 拒，本地 def 工厂臂 M219，裸名不可解析 ⇒ 拒）；Attribute 臂共用 `_decoratorRootProblem` helper（项目/禁止根 ⇒ 拒） | fixture `decorator_ifexp` / `decorator_lambda_factory` / `decorator_local_factory`（后两者兜底线接住 ⇒ **文本归位**判别） |
| C-24-2 | ①Store 根判定用模块级 `aliases` ⇒ 函数作用域 import 再绑定逃逸（`def _m(): import logging; logging.getLogger = …`）；②Store 基底非静态链（`[sys][0].foo = …`）⇒ 无 deny；③C-6 腰带 in-process 可伪造（换假 + 改 `__module__`） | ①`effAliases` 单点（scanExec 顶部一次合成，deny 全走它，M211 变异=回退 aliases 只放走 fn 作用域形态）；②`isStaticChain` else 臂 fail-closed（M222）；③腰带改**子进程信任锚**（fresh interpreter 打印三原语 `__module__`） | `store_fn_scope_import` / `store_subscript_base` / `test_analyzer_primitives_not_swapped_in_fresh_interpreter` |
| H-24-1 | 嵌套调用工厂内层是本地 def（`pick(os)('/tmp/x')`，pick 返回 `x.remove`）⇒ 例外臂放行；`_f = (lambda a: a.remove)(os)` ⇒ applyBinding 不记不透明 | 例外臂改**可证性**判据 `isProvenRegistration`：内层被调解析到**外部 import** 别名才豁免（M221 变异=臂死）；applyBinding 补 **else 臂**（func 为 lambda/嵌套调用/下标的调用结果 ⇒ 不透明，M223） | `nested_call_local_factory`（放行性变异）/ `opaque_lambda_result`；clean 反向锚 `nested_call_callee` **改真实形态**（内层来自 pydantic import——本地 def 形态本轮起为拒）+ `nested_call_external` |
| H-24-2 | `.so`/`.pyd` 原生扩展遮蔽同名 `.py`（CPython import 顺序 EXT_SUFFIX 优先，实测）且不在 CI 面 | 新 pattern `^backend/app/utils/[^/]+\.(?:so|pyd)$`（仓库现无 `.so` ⇒ 空集）+ 杠杆路径 + CODEOWNERS 两行 | 杠杆用例 +`.so` 路径（M216） |
| M-24-1 | `'a'.replace('a','b')` 被 `_DENIED_CALL_ATTRS` 的 "replace" 误伤 | 字面量接收者豁免（M224 变异=豁免条件死 ⇒ clean fixture 红）；变量绑定字符串仍 fail-closed（需常量传播才可证，文档化局限） | clean `constant_replace` |
| M-24-2 | `ci_mutation_cases_r23.py` 漏出两处反向守卫清单；docstring 过度声明「从文件系统反查」 | 两清单补 r23/r24（M217 变异=pattern 只删 `\|_r23`）；docstring 更正 | `test_detectCiSurfaceChanges_flags_the_mutation_evidence_script` |
| L-24-1 | 校准计数「4 处」实为 **11 处**（schemas.py 596/597/621/622/1572/2926/2927/2939/3039/3040/3271） | 注释与文档更正（本节即更正记录） | — |
| L-24-2 | round-24 deny 用例 docstring 过度声明逐条变异判别 | 更正：只断言非空、os_remove_assign/sys_stdout_rebind 双覆盖、精识臂判别由 round-25 文本归位承担 | — |
| L-24-3 | M209 注释过期（段集扩展已回退，锚=`_DENIED_CALL_ATTRS` 的 "write_text"） | 注释更正 | — |

**新增 M216–M224（9 组）+ 重锚 M169/M211/M213 + M69 第十一次重锚 ⇒ 变异 214 → 223 组。**

**solo 首轮净漏 1（M218）**：变异文本写成 `not isinstance(dec, (Lambda, Subscript, Starred))`
——多了 `not` 语义反而**更宽**（拒掉一切非这三形态）⇒ IfExp 仍被拦。回退目标必须是 round-24
之前的**正向**白名单（无 `not`）。修正后 solo 12/12。

**证据链**：锚点 223/223 → 守卫套件 **224 passed**（3 文件；链文件 164 → **175 条**）→
校准（真实 76 文件闭包）**检出钩子 0** → solo 12/12 → 全量 223/223 净漏 0（exit 0，见
`/tmp/n6_r25_full_run.log` 终态）。

**行数**：`ci_conftest_chain.py` 1932 → **2066 行**；`test_ci_conftest_chain.py` 1407 → **1588 行**（175 条）；
`test_ci_surface.py` 1021 → **1033 行**（§5 行债更新，策略不变）。

### 4.2.20 第二十六轮复审修复（5H/4M，223 → 233 组，2026-11-09）

第二十六轮复审判定 **BLOCK（5 HIGH / 4 MEDIUM）**，全部带攻击构造（探针先行：15 个用例
`/tmp/n6_r26_probe.py` 实测修复前全部逃逸 / 修复后归位），逐条修复：

| 编号 | 攻击构造 | 修复 | 判别 |
|---|---|---|---|
| H-26-1 | ①通用可调用工厂：`functools.partial(int)('2')` / `operator.methodcaller` / `attrgetter`——内层解析到外部 import 的**工厂**，返回把任意可调用绑好的目标 ⇒ 例外臂放行；②注册式豁免的**实参子树**里绑好 sink 成员引用：`field_validator('x')(Path('y').unlink)`（sink 在外层 Call 的实参，非内层 func） | ①工厂条件加 `_CALLABLE_FACTORY_DENIED`（`functools.partial` / `operator.methodcaller` / `operator.attrgetter`，内层链经 `effAliases` 解析成全链再比对，M225）；②`_callFactorySinkProblem(node)` 扫 `_DENIED_CALL_ATTRS` sink 成员，命中 ⇒ 专属消息（M226）。（⚠️ 本行原写「对**整个 Call 节点**扫描」——**第二十七轮复审证伪**：当时实现只走 args/keywords，func 子树不看（H-27-1），第二十八轮才真正收宽到整个 Call 节点，见 §4.2.21。）**顺序归位**：工厂条件先判、sink 扫描嵌在其内 ⇒ 两条消息互不抢（partial fixture 拿工厂消息、sink fixture 两消息都在） | 文本归位（`nested_call_partial_factory` / `nested_call_sink_arg`）；clean 反向锚 `nested_call_external` 从 `functools.partial(int)('2')`（本轮起为拒）**改真实形态**：pydantic `field_validator('x')(_v)` 注册惯用法 |
| H-26-2 | 被扫函数体内的嵌套 def/class **装饰器**为动态表达式：`def _o(): @(_mk() if True else None) def g()`——模块级预扫只管模块级 ⇒ 整段逃逸 | scanCallee 的 stmt 循环对 FunctionDef/AsyncFunctionDef/ClassDef 的 `decorator_list` 补形态检查（非 Name/Attribute/Call ⇒ 拒，M227） | `fn_scope_dynamic_decorator`（放行性：`_decoratorMember` 对 IfExp 不报歧义） |
| H-26-3 | scanCallee 的 `scopeAliases` 只并模块级映射 ⇒ 调用方作用域别名不进被调 fn-local 类 `__init__` 的作用域：`def _o(): from app.utils import flakes; class _C: __init__ 里 flakes.hook = None` ⇒ 逃逸 | scanCallee 加第三参 `enclosingAliases`，`scopeAliases = {**aliases, **enclosingAliases, **_aliasMap(body)}`（内层 > 调用方 > 模块级）；scanExec 内 **13** 处调用点全部穿透 threading（原写 12 处为计数错漏，第二十七轮复审纠正；模块级 3 处 depth 0 不传） | `two_level_fn_scope_import`（M228=退回不带 enclosingAliases 的旧形态）；M195 同步重锚到 `_aliasMap` 单行（判别 fn_local_import_alias 不变，与 M228 锚/判别不重叠） |
| H-26-4 | 裸名装饰器按模块级 `aliases` 解析 ⇒ fn 作用域的项目 import 遮蔽模块级外部同名别名时解析到外部根放行（装饰期真实执行的是项目 hook） | H2 块内 `decResolvedName = effAliases.get(dec.id)`（M229=退回 aliases） | `fn_scope_project_decorator_shadow`（放行性） |
| M-26-1 | `from . import models; @models.deco`——相对导入在 `_aliasMap` 被静默跳过 ⇒ 根当外部放行；但相对导入只可能指向本仓库 | ImportFrom 分支 `stmt.level > 0` ⇒ 映射到哨兵根 `__relative__`，并入 `_PROJECT_DECORATOR_ROOTS` ⇒ 按项目代码拒（M234=哨兵出元组） | `relative_import_project_decorator`（放行性）；fixture 在 tmp_path 无包上下文也可判 |
| M-26-2 | 四个装饰器 deny 臂无 fixture/无变异（call_result / subscript_factory / attr_base / unknown_bare_factory） | 四个探针实测的 fixture（M230–M233）；call_result/subscript_factory 有兜底线同拦 ⇒ **文本归位**，其余放行性 | 见 `_EVIL_R26_EXPECTED_TEXT` |
| M-26-3 | M212 原变异（`if not isinstance(node.func, ast.Call):` → `if False:`）让 else 臂 AttributeError ⇒ **崩溃红**（退出码 1 + FAILED 行都满足但不证明检测能力） | ①M212 重设计为把该臂的 add 块换 `pass`（臂死 ⇒ IfExp 放行 ⇒ 干净转红）；②跑者 `ci_mutation_evidence.py` 加 `_ANALYZER_FRAME_MARKERS`（findImportTimeHooks/_findImportTimeHooksImpl/scanExec/scanCallee 栈帧）⇒ 输出含分析器内部帧判 `inconclusive`（取证失败，非转红） | M212 转红且无分析器帧；跑者自检由 M 判定本身覆盖 |
| H-26-5 | `.so`/`.pyd` pattern 只盖 `app/utils/[^/]+` 一层：`app/tests/_pg_support*.so`（conftest.py:102 收集前 import）与 `scripts/kpi_metric_db_guard*.so` 都在面外 | pattern 收宽 `^backend/.*\.(?:so\|pyd)$` + CODEOWNERS 四行 `backend/**/*.so\|.pyd` + `backend/*.so\|.pyd`（`**` 匹配零或多层目录，test:168–200 实证）；杠杆路径加两处实测（M216 第二次重锚） | 杠杆用例（`_pg_support.cpython-311-darwin.so` / `kpi_metric_db_guard.abi3.so`） |
| M-26-4 | 变异清单 `*_r26` 漏出 CI 面 pattern 与反向守卫清单 | pattern 加 `\|_r26`（M69 第十二次重锚 + M217 第二次重锚）；`test_ci_codeowners.py` 两清单加 r26。（⚠️ 本行原写「两清单加 r26」已含漏改——`test_ci_surface.py` 的**两个反向清单**当时没加 r26，第二十七轮 M-27-3 抓出、第二十八轮补上，见 §4.2.21） | M217 转红 |

**新增 M225–M234（10 组，新文件 `ci_mutation_cases_r26.py`）+ 重锚 M103/M141/M195/M212/M216/M217/M169/M218 + M69
第十二次重锚 ⇒ 变异 223 → 233 组。** 重锚分两类：

- **重构连带**：M103/M141/M195 的旧锚（ImportFrom 单行条件 / `scanCallee(hook, depth + 1)` /
  scopeAliases 单行 dict）被本轮 H-26-3/M-26-1 重构直接改掉 ⇒ 全量锚点核对抓出 ⇒ 按当前文本重锚、判别不变。
  M141 注释保留「删声明 ⇒ NameError 崩穿」的教训。
- **子串碰撞（全量终跑净漏 2 抓出）**：M169/M218 的单行锚（16 空格 `if not isinstance(dec, …)`) 成为
  本轮新增 scanCallee H-26-2 臂（20 空格）的**子串**（count=2）⇒ occurrence=1 杀错臂（scanCallee 臂
  对模块级 fixture 无关）⇒ 首轮全量 M169/M218 仍然绿。修法：锚带前导 for 行（12 空格缩进）唯一钉住
  模块级预扫臂；**锚跨两行 ⇒ mut 也必须两行**（只换 if 行会留下悬空 for 体 ⇒ IndentationError ⇒
  取证失败 exit 4，solo 即抓出）。教训入 M169/M218 注释：**新 deny 臂与旧臂同判据时，新臂缩进不能让
  旧锚成子串；全量锚点核对要查 `count == occurrence` 而不只是 `≥`**。

**证据链**：锚点 233/233（chain 文件内 `count == occurrence` 精确核对）→ 守卫套件 **250 passed**
（5 文件；链文件 175 → **184 条**，round25+26 用例 20 条全绿）→ 校准（真实 76 文件闭包）**检出钩子 0**
（H3 穿透/相对导入/工厂+sink 新 deny 臂零误报）→ solo 16/16（M103/M141/M195/M212/M169/M218 +
M225–M234，净漏 0、零崩溃红）→ 全量首轮净漏 2（M169/M218 子串碰撞，见上）→ 重锚后
**全量终跑 233/233 转红、净漏 0**（`/tmp/n6_r26_full_rerun2.log` 终态）。

**行数**：`ci_conftest_chain.py` 2066 → **2156 行**；`test_ci_conftest_chain.py` 1588 → **1765 行**（184 条）；
`test_ci_surface.py` 1033 → **1039 行**；`ci_surface.py` **690 行**；新文件 `ci_mutation_cases_r26.py` **166 行**
（§5 行债更新，策略不变）。

### 4.2.21 第二十七轮复审修复（3H/5M/1L，233 → 239 组，2026-11-09）

第二十七轮复审判定 **BLOCK（3 HIGH / 5 MEDIUM / 1 LOW）**，全部带攻击构造（探针先行：
11 个用例 `/tmp/n6_r28_probe.py` 实测修复前 6 个新攻击全部逃逸、4 个回归基线仍拒、clean 仍过），
逐条修复：

| 编号 | 攻击构造 | 修复 | 判别 |
|---|---|---|---|
| H-27-1 | sink 藏在**外层工厂被调的 func 子树**：`field_validator(Path('y').write_text)(lambda v: v)`——write_text 在 `callNode.func` 的实参里，H-26-1 的扫描面只走 args/keywords ⇒ 整段逃逸（同时证伪 §4.2.20 的「整个 Call 节点」表述） | `_callFactorySinkProblem` 扫描面改为整个 Call 节点（`for subNode in (callNode.func, *args, *kw.values)` 后逐个 `ast.walk`）（M235=func 出扫描面 ⇒ 文本归位） | `nested_call_sink_in_factory_args`（文本归位；实参 sink 的回归 fixture 不受影响 ⇒ 判别独占） |
| H-27-2 | H-26-2 臂只**平扫 callee.body 顶层**：`def _o(): if True: @(_mk() …) def g()`（分支体）与类体内的嵌套 def 动态装饰器整段逃逸 | scanCallee 末尾改 `_moduleScopeStatements(list(callee.body))` 递归（与模块级预扫同一展开语义：If/Try/With/For/While/Match + ClassDef 体；函数体内的装饰器由该 def 自己被 scanCallee 时管）（M227 重锚到新形态两行；M239=递归退回平扫 ⇒ 分支体 fixture 逃、顶层 fixture 仍被平扫接住 ⇒ 与 M227 判别互斥独占） | `fn_scope_dynamic_decorator_nested_block` / `fn_scope_dynamic_decorator_class_body`（M239 判前者；M227 判顶层 `fn_scope_dynamic_decorator`） |
| H-27-3 | IIFE lambda 裸调 `scanExec(_iterExecNodes(callee.body), depth)` ⇒ **丢调用方作用域别名与本地类**：①`def _o(): import sys as s; (lambda: s.path.insert(0,'x'))()`；②`(lambda: _C())()`（_C 为 fn-local 类、`__init__` 携 sink）——双双整段逃逸 | scanCallee 加第四参 `enclosingClasses`；Lambda 臂改 `scanExec(…, classes=enclosingClasses, aliasesMap={**aliases, **(enclosingAliases or {})})`（lambda 体无语句 ⇒ 不可能有内层 import，别名两层并够用）；scanExec 内 **13** 处 scanCallee 调用点全部补第四参 threading（模块级 3 处 depth 0 不传；`effectiveClasses` 覆盖层**不加宽**——第四参只在 Lambda 臂消费 ⇒ 校准零风险）（M237=aliasesMap=None、M238=classes=None ⇒ hooks 变空；M141 第三次重锚补 `, classes)`） | `iife_fn_scope_import` / `iife_local_class`（放行性） |
| M-27-5 | `operator.itemgetter` 不在 `_CALLABLE_FACTORY_DENIED`：`operator.itemgetter(0)(_c)`（`__getitem__` 携 sink）在 import 期执行 ⇒ 逃逸 | 元组加 `operator.itemgetter`（M236=出元组 ⇒ hooks 变空） | `nested_call_itemgetter_factory`（放行性） |
| M-27-2 | 跑者锚点检查 `total < occurrence` 是**假绿灯**：锚成为更深缩进同文本臂的子串时 total 变大（M169/M218 实证 count=2 vs occurrence=1 杀错臂）⇒ 跑者层不设防 | 改 `total != occurrence` fail-closed（多了=锚不独占可能杀错臂、少了=锚不存在，都判取证失败）（判别在跑者契约本身 + M169/M218 教训注释；`test_ci_mutation_evidence.py` 无钉旧判据的用例） | 跑者契约（round-29 复审可查）；全量 239 组跑过即为回归证明 |
| M-27-3 | M-26-4 漏改的另一半：`test_ci_surface.py` 两个反向守卫清单（~204 / ~787）只到 r24 ⇒ r26 数据文件在「真实文件反查」与「接受面」两处都不设防 | 两清单各加 `ci_mutation_cases_r26.py`；`ci_surface.py` pattern 加 `\|_r28`（M69 第十三次重锚 + M217 第三次重锚 + **M240**=删 `\|_r26`） | M240（真实文件反查用例红：r26 存在但不再判进面） |
| M-27-1 | M195/M228 同杀 scopeAliases 的一部分 ⇒ 对方判别 fixture 会被顺带转红，无跨效应备案 | 备案入 M195 注释：**M195 宽效应**（杀 fn-local 行 ⇒ two_level 两级链同样断 ⇒ M228 判别也红）；**M228 窄效应**（只删 enclosingAliases ⇒ fn_local_import_alias 判别不红）；判别归档取窄效应方，全量跑按「期望用例红 + 其余分析」解读 | 注释备案（非 deny 线） |
| M-27-4 | fn 作用域 def/lambda 的调用目标从不进 `callTargets` ⇒ fn 作用域内 `def _h(): …; _h()` 的被调体不被 scanCallee | **既有欠账入 §5**（round-22 重构起即如此，非本轮回归；顶层/类体/装饰器路径均覆盖，fn 作用域命名函数互调是剩余盲区） | §5 欠账（非本轮修复线） |
| LOW×3 | ①r26 文件头写「M225–M233」漏 M234；②`_ROUND26_CLEAN` 未使用；③跑者崩溃红注释说「正常断言栈帧会有分析器入口帧」——错，正常断言发生在分析器调用**返回之后**，栈帧只有测试帧 | ①改 M234 + M-26-4 编号冲突对齐（腰带子进程归 M-26-3 取证基建）；②删未用常量；③注释改「返回之后 ⇒ 只有测试帧」；同款 M-26-4 编号冲突在 `test_ci_conftest_chain.py` 腰带用例 docstring 一并纠正（M-26-4→M-26-3） | 文档/注释（非 deny 线） |

**新增 M235–M240（6 组，新文件 `ci_mutation_cases_r28.py`）+ 重锚 M141（第三次）/M227（第二次）/
M69（第十三次）/M217（第三次）⇒ 变异 233 → 239 组。** `ci_mutation_cases.py` 文件头的组数
（「174 组」自 r14 拆分起未随轮更新）一并改 **239**。

**证据链**：锚点 **239/239**（全量 `count == occurrence` 精确核对）→ 守卫套件 **305 passed**
（5 文件：chain 184 → **190 条**、surface、mutation_evidence、gh、merge_gate_workflow）→
校准（真实 76 文件闭包）**检出钩子 0、一层落点不在面/豁免 0**（H-27-1/2/3 + itemgetter
新 deny 臂零误报）→ solo **8/8**（M141/M227/M235–M240，净漏 0、零崩溃红）→
**全量 239/239 转红、净漏 0**（`/tmp/n6_evidence_run_v17.log` 终态）。

**行数**：`ci_conftest_chain.py` 2156 → **2184 行**；`test_ci_conftest_chain.py` 1765 → **1863 行**（190 条）；
新文件 `ci_mutation_cases_r28.py` **148 行**（§5 行债更新，策略不变）。

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
- ⚠️ **派生器的脆性**（第十二轮复审 LOW-6，其中叶子优先序**已在第十六轮 H-5 修复**：
  `_resolveModuleFiles` 现在双形态两个叶子都收 —— `.py`-先于-包的顺序问题不再存在，
  M135 + 合成树测试钉住；`parents[4]` 仍接受）：`test_ci_conftest_chain.py` 的
  `Path(__file__).resolve().parents[4]` 在文件移动时会静默指错 —— 有测试内「派生结果非空」
  断言兜底（指错 ⇒ 派生为空 ⇒ 红，fail-closed 方向）。
- ⚠️ **钩子检测范围 = conftest 收集前 import 链的闭包，不含测试模块自身的模块级产品
  import**（第十三轮复审 MEDIUM-3，接受弱解，2026-11-08）：守卫的检测闭包锚定在
  conftest 链（实测 76 文件）；若扩到「全部测试模块为根」的 465 文件闭包，
  会把 `mcp_server.py`（`os.environ.get` 读环境）、`research.py`（`socket.gethostname`）
  及约 12 个带真实 `sys.path.insert` 的一次性脚本拖进仲裁面 ⇒ 大量误报而非堵漏
  （那些脚本是显式自包含工具，不参与 pytest 收集）。残余风险：攻击者往某个
  **测试模块**的模块级产品 import 里塞 hook 语句不被本守卫发现 —— 与下方
  「删除/削弱普通测试文件」同类，威胁模型上限=作者主动伪造。
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
- ⚠️ **`ci_gh.py`（839 行）与 `test_ci_surface.py`（第九轮 979 行 → 第十二轮 988 行）超 800 行上限未拆**（第三轮评审 M-3，部分接受）：
  `ci_mutation_cases.py`（纯数据、无锚点落点）已拆；但这两个文件的私有 helper（`_runGh` / `_SHA_PATTERN` /
  `_parseIsoTimestamp` / 各 stub）被多层**紧耦合**引用 ⇒ 批内拆分要动安全闸门的实现与守卫结构
  ⇒ 有**锚点腐烂**风险（每个锚点都逐字钉在现行实现上）⇒ 拆分**另立项**在无评审压力时做。
  参照 `chat_multistep` 853 行豁免先例（`qa-system-chat-multistep-line-cap-exemption`）。
  **第十六轮新增欠账**：`ci_mutation_cases.py`（拼装头 + M37–M99 注释，945 行）—— 第十六轮在 r14/r16
  数据文件已拆的前提下仍超 800，成因是历史批的**行内注释**（每批的威胁模型说明）而非数据本身；
  注释搬去各 `cases_r*.py` = 纯搬移、零锚点风险 ⇒ 下次触碰该文件时顺手拆。
  **第十八轮新增欠账**：`ci_conftest_chain.py`（649 → **923 行**）—— 第十八轮按 §2.10.17 重写后超 800；
  约 50 条变异锚点逐字钉在现行实现的行上（含行内缩进）⇒ 批内拆分会打断锚点、变异取证要整轮重跑
  ⇒ 与 `ci_gh.py` 同等待遇记债，下次触碰该文件时（无评审压力时）拆。
  **第二十轮更新**：`ci_conftest_chain.py` 923 → **~1030 行**（§2.10.18）；锚点数同步涨到 ~60 条，
  记债规则不变。
  **第二十二轮更新**：`ci_conftest_chain.py` ~1030 → **1755 行**（§2.10.19 落定 1762 后第二轮
  删冗余 deny 块 −7 行，§4.2.15）；锚点数 **202 组**（M175–M203 新增 29 组锚在新增行上、
  M157/M69/M94/M140/M141/M150 重锚、M172 退休），记债规则不变——本轮若拆分则 202 组锚全部要
  重生成 + 全量取证重跑，成本已高于任何评审压力，维持「下次触碰顺手拆」。
  **第二十四轮更新**：`ci_conftest_chain.py` 1755 → **1932 行**（§2.10.20/§4.2.17）；锚点数 **214 组**（M204–M215 新增 12 组、M69 第十次重锚、M172 已退休），记债规则不变。
  **第二十五轮更新**：`ci_conftest_chain.py` 1932 → **2066 行**（§4.2.19）；锚点数 **223 组**（M216–M224 新增 9 组、M169/M211/M213 重锚、M69 第十一次重锚），记债规则不变。
  **第二十六轮更新**：`ci_conftest_chain.py` 2066 → **2156 行**（§4.2.20）；锚点数 **233 组**（M225–M234 新增 10 组入新数据文件 `ci_mutation_cases_r26.py`、M103/M141/M195/M212/M216/M217 重锚、M69 第十二次重锚），记债规则不变。
  **第二十八轮更新**：`ci_conftest_chain.py` 2156 → **2184 行**（§4.2.21）；锚点数 **239 组**（M235–M240 新增 6 组入新数据文件 `ci_mutation_cases_r28.py`、M141/M227/M69/M217 重锚），记债规则不变。
  **第二十八轮新增欠账（M-27-4）**：fn 作用域 def/lambda 的调用目标从不进 `callTargets` ⇒ fn 作用域内 `def _h(): …; _h()` 的被调体不被 scanCallee——round-22 scanExec/callTargets 重构起即如此（非本轮回归）；顶层/类体/装饰器路径均覆盖，fn 作用域命名函数互调是剩余盲区。收口成本 = callTargets 作用域化 + 全量取证重跑，独立排期。
  **同批欠账记账**：`test_ci_surface.py` 988 → **1021 行**（二十五轮 → **1033 行**；二十六轮 → **1039 行**）、`test_ci_conftest_chain.py` → **1407 行**（164 条用例；二十五轮 → **1588 行** / 175 条；二十六轮 → **1765 行** / 184 条；二十八轮 → **1863 行** / 190 条）——守卫测试文件的私有 fixture/常量同样被多层复用，拆分亦须逐条核对锚点，同规则记债。
- **没有**处理 N5（净丢失只能给 `NEEDS_REVIEW`）、N8（收紧 `enforce_admins` 的前置通道）。
- 本批**不部署**（零 DDL、零生产写）。

## 6. 相关条目

- [[qa-system-merge-gate-net-diff]]
- [[qa-system-merge-gate-protection-config]]
- [[qa-system-merge-gate-flake-whitelist]]
- [[qa-system-ci-failure-set-tool]]
