"""「CI 相关面」判定：PR 改了哪些文件 ⇒ 两次 run 的失败集合还可比吗。

背景
----
「失败集合对差」这条合并判据的前提是**两次 run 的失败集合可比**。
但 PR 作者可以通过改「CI 相关面」让失败用例**根本不产生 FAILED 行**：

- `backend/conftest.py` 里挂 `pytest_collection_modifyitems` 剔除用例；
- `.github/workflows/ci.yml` 里加 `--deselect`；
- `backend/app/utils/ci_shard.py` 把含新失败的测试文件踢出分片。

⇒ 失败集合与基线完全相同 ⇒ **假「净新增 0」** ⇒ 而 GitHub 上 CI 还**全绿**
⇒ 没有任何二次防线。

⇒ 本模块判定「PR 是否动了 CI 相关面」；命中 ⇒ 对差工具拒绝给出「可合并」。

⚠️ **判定集合 = 「两次 run 实际跑的那两份代码的差异」∩「CI 相关面」**

⚠️⚠️ **那两份代码分别是什么**（2026-11-07 实测，两路评审独立得出同一结论）：

- `ci.yml` 对 `pull_request` 事件触发，三个 `actions/checkout` **都不带
  `with: ref:`** ⇒ 检出的是 `refs/pull/N/merge`
  ⇒ **PR 的 CI 跑的是「main 与 PR 头合并出来的 commit」，不是 PR 头**。
  实证：PR #111 的 run `42281369007` 的 `head_sha` = PR 头 `1d226997…`，
  而 job 日志写着 `HEAD is now at 23db713 Merge 1d226997… into 87f8e8e6…`。
- ⇒ 判定要落在「基线 run 跑的树」与「PR run 实际执行的树」之间。

⚠️⚠️⚠️ **「PR run 实际执行的那棵树」怎么拿**（2026-11-07 第三版修订）：

前两版各踩一个坑：

1. 取 **PR 头树** ⇒ main 改了 CI 文件时**假拦**（实测 PR #92/#94/#95/#107 的
   净变更 CI 命中 1/29/29/17，而三点归因命中 **0**）；更糟的是 PR 头树与基线树
   **同样旧**时差异为空 ⇒ **fail-open**；
2. 取 `pulls/{n}.merge_commit_sha`（测试合并 commit 的 tree）⇒ 它是
   `refs/pull/N/merge` 的**当前值**，main 一推进就变 ⇒ 判定的「第二棵树」
   可能**不是**那次 run 执行的树（假拦 + fail-open）；PR 合并后该 ref 直接
   **404** ⇒ 历史 run 的合并树无法回溯。

⇒ 第三版：**不取那棵树，改成把「差异」拆成两段自己算**：

| 记号 | 含义 | 取法 |
|---|---|---|
| `M_base` | 基线 run 跑的那棵树（= 基线 run 的 head commit） | 基线 run 的 `head_sha` |
| `M_T` | PR run **事件派发**那一刻 main 的 tip（= 那次合并的 main 侧底座） | **两条腿**（见下）|
| `A` | main 在 `[M_base, M_T]` 之间的**内容推进** | 树对树差 `M_base` ↔ `M_T`（**两条腿取并集**）|
| `P` | PR **自己**改的文件 | 三点语义：树对树差 `merge_base(M_T, PR 头)` ↔ PR 头 |

**判定集合 = A ∪ P**。

⚠️ **`M_T` 取两条腿**（2026-11-07 安全评审 HIGH-2）：
「main 的 push run 列表」这条腿有两个**漏检**方向 ——
main 的那次 push **没触发** workflow、或它的 run **创建得晚**（排队 > 宽限）
⇒ `M_T` 偏旧 ⇒ `A` 腿漏掉 main 的推进 ⇒ 两次 run 的 CI 定义可能其实不同
⇒ 失败集合不可比 ⇒ 却判「可合并」⇒ **fail-open**。
⇒ 第二条腿走 **commits API**（不依赖 run 列表）⇒ 补上那两个洞；
而它自己也会因「commit 日期 ≠ push 时刻」偏旧 ⇒ 两条腿**取并集** ⇒
谁漏掉的那一段，另一条兜住。

**为什么 A ∪ P 就是那棵树的差异**：PR run 实际执行的树里，路径 p 的内容 =
PR 没碰 p 时取 `M_T` 的版本、碰了 p 时取 PR 的版本
⇒ 与基线树 `M_base` 的差异恰好 = 「main 改过的 p」（A）∪「PR 改过的 p」（P）。

⚠️ 为什么不直接比「基线树 ↔ 执行树」：那需要执行树的 tree sha，
而它的获取途径（合并 ref / `merge_commit_sha`）都是**当前值**或**会消失**的
⇒ 上面第 2 条坑。**A ∪ P 用两个各自稳定的 commit 重算出来 ⇒ 结论与
「执行树」一致，却不依赖任何会漂移的 ref。**

⚠️ `M_T` 的实测校验（2026-11-07，PR #111）：PR run `42281369007` 的
`created_at` = `run_started_at` = `2026-11-06T19:38:13Z` ⇒ 按上界取到
`M_T` = `87f8e8e6…` ⇒ 正是 job 日志里那次合并的 main 侧底座。

⚠️⚠️ **上界锚 `created_at` 而不是 `run_started_at`**（2026-11-07 代码评审 CRITICAL-1）：
`pull_request` 事件的**合并 commit 在事件派发那一刻就被冻结**了
⇒ 复跑（re-run）**不会**重新计算它 ⇒ 复跑检出的仍是**同一个**合并 commit
⇒ 它的 main 侧底座是**派发那一刻**的 main，**不是**复跑那一刻的 main。

实测（PR #105 run `41908498247`，attempt=2）：`created_at=2026-11-04T20:38:19Z`、
`run_started_at=2026-11-04T22:24:11Z`；合并 commit `57a267a9` 的
committer.date = `20:38:18Z`、第一父 = `83581e10`；attempt-2 的 job 日志写着
`git fetch … +57a267a93f…:refs/remotes/pull/105/merge` ⇒ **同一个**合并 commit。

⇒ 锚 `run_started_at` ⇒ `M_T` 取到**派发之后**的 main ⇒ `A` 腿把
「派发之后 main 才推进的 CI 改动」也算进来 ⇒ 判定集偏大 ⇒ 假拦；
而闸门侧若同样用它取基线 ⇒ 基线树与执行树都落到那个更晚的 main
⇒ `A` 变成空集 ⇒ 两次 run 实际跑的 CI 定义不同却判「可合并」⇒ **fail-open**。
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass

from app.utils.ci_gh import (
    RunHeadInfo,
    fetchChangedFiles,
    fetchMainTipShaAt,
    fetchMainTipShaByCommitDate,
    fetchNetChangedFiles,
    fetchRunHeadInfo,
    mainTipCutoff,
)
from app.utils.ci_text import sanitizeForReport

# 「CI 相关面」：改了这些文件 ⇒ 两次 run 的失败集合**不再可比**。
#
# ⚠️ 工具自身的模块也在列：改 `ci_failure_set.py` 就能直接改判定逻辑
# ⇒ 失败集合与基线不可比 ⇒ 必须人工确认。
#
# ⚠️ 这份清单必须**覆盖工具的每一个模块**（守卫 `test_ci_surface.py::
# test_every_ci_tool_module_is_in_the_surface_list` 从文件系统反查 ⇒ 漏一个就转红）：
# - `ci_judge.py` 装着**判定逻辑**（什么算「跑完了」/「不可信」）
#   ⇒ 改它就能把「被取消的 job」判成「可用」⇒ 空的失败集合参与对差；
# - `ci_report.py` 装着**报告渲染**
#   ⇒ 改它就能把「净新增失败用例」清单从报告里抹掉 ⇒ 操作者看不到新增了什么。
#
# ⚠️ conftest / pytest 配置必须按**任意深度**判定（不能枚举「今天有哪几个」）：
# pytest 会从 rootdir 往下**逐层**收集 conftest ⇒ `backend/app/conftest.py`
# 对 `backend/app/tests/**` 全部生效 ⇒ 枚举式判定会漏掉新加的那一层
# ⇒ PR 在那里挂 `pytest_collection_modifyitems` 剔除失败用例
# ⇒ 失败集合与基线完全相同 ⇒ 假「净新增 0」⇒ 而 CI 还全绿。
_CI_SURFACE_PATTERNS = (
    r"^\.github/workflows/",
    # 任意深度的 conftest / pytest 配置（basename 判定 ⇒ 新加一层也照样命中）
    #
    # ⚠️ **`pyproject.toml` 必须任意深度**（2026-11-07 安全评审 CRITICAL-1）：
    # pytest 找 ini 文件是**从测试路径往上**找的（`locate_config`：从调用路径
    # 逐级向上找 `pytest.ini` / `pyproject.toml` / `tox.ini` / `setup.cfg`）
    # ⇒ 在 `backend/app/tests/` 或 `backend/app/tests/unit/` 放一个
    # `pyproject.toml`（带 `[tool.pytest.ini_options]`）就会**抢先**成为
    # rootdir 的 ini 文件 ⇒ 它的 `addopts`（例如 `--deselect`）生效、
    # `backend/pyproject.toml` 被**忽略** ⇒ 失败用例根本不产生 FAILED 行
    # ⇒ 失败集合与基线完全相同 ⇒ 假「净新增 0」⇒ 而 CI 还全绿 ⇒ 没有二次防线。
    r"(?:^|.*/)conftest\.py$",
    r"(?:^|.*/)pytest\.ini$",
    r"(?:^|.*/)setup\.cfg$",
    r"(?:^|.*/)tox\.ini$",
    r"(?:^|.*/)pyproject\.toml$",
    # 依赖锁定文件：改了依赖版本 ⇒ 测试环境变了 ⇒ 失败集合不再可比
    r"^backend/uv\.lock$",
    # ⚠️ **compose 文件**（2026-11-07 安全评审 HIGH-1）：integration 分片在 CI 里
    # 用 `docker compose -f docker/docker-compose.yml up milvus-standalone`
    # 起 Milvus/etcd/minio ⇒ 改这份 compose（环境变量、镜像版本、健康检查…）
    # ⇒ **integration 的运行环境变了** ⇒ 两次 run 的失败集合不可比
    # ⇒ 而闸门恰恰要比这两组失败集合。
    # ⇒ 守卫 `test_ci_surface_reverse.py::test_every_compose_file_used_by_the_workflows_is_a_ci_surface`
    # 从 workflow 里反查引用到的 compose ⇒ 新增一份 compose 也会被要求入清单。
    r"^docker/docker-compose[\w.-]*\.ya?ml$",
    r"^backend/app/tests/(?:_pg_support|milvus_isolation)\.py$",
    r"^backend/app/utils/ci_(?:shard|failure_set|text|gh|surface|head|pytest_log|judge|report|flakes)\.py$",
    # ⚠️ **变异取证脚本**（2026-11-07 安全评审 MEDIUM-3）：
    # 它决定「哪些变异能转红」⇒ 改它就能让守卫**恒绿**（把「转红」判据改成
    # 「退出码非 0」即可 ⇒ 收集失败也算「转红」⇒ 取证全绿）
    # ⇒ 与守卫用例同族 ⇒ 必须进 CI 相关面 + CODEOWNERS。
    # ⚠️ 清单（`ci_mutation_cases.py`，纯数据）与跑者（`ci_mutation_evidence.py`）
    # **同族**：改清单可以把某组变异改成「改一个没人守的地方」⇒ 照样恒绿。
    r"^backend/scripts/ci_mutation_(?:evidence|cases)\.py$",
    # 已知抖动白名单**数据文件**：它直接决定「哪些失败用例不作判据」
    # ⇒ 改它 = 改判据 ⇒ 与工具模块同族，必须人工确认。
    r"^\.github/ci-known-flaky-tests\.json$",
    # ⚠️ **守卫用例本身**与**授权面本身**也在 CI 相关面里（2026-11-06 两路 review 共同指出）：
    # - 改守卫用例 = **解除守卫**（把「拒绝文件级 ID」那条断言删掉 ⇒ 白名单就能塞文件级 ID）；
    #   而守卫被改软之后**它自己不会再红** ⇒ CI 全绿 ⇒ 没有二次防线
    #   （CODEOWNERS 的注释早就写了「改守卫 = 解除守卫」，只是没落到这份清单里）。
    # - 改 CODEOWNERS = **换授权人**（把某条 CI 路径的复核人去掉 ⇒ 判据不再有人把关）。
    # ⇒ 个人仓库的 CODEOWNERS 没有强制力（装不了 users bypass）⇒ 这两类文件是
    #   这条链上**仅剩的人工检查点** ⇒ 必须在 CI 相关面里。
    #
    # ⚠️ 守卫用例必须按**任意深度**判定（与 conftest 同款纪律）：
    # 旧清单只认 `backend/app/tests/unit/test_ci_*.py` ⇒ 把守卫挪到
    # `backend/app/tests/integration/test_ci_x.py` 就同时逃出
    # ①「CI 相关面」判定 与 ②CODEOWNERS 反查（后者也只扫 `unit/`）
    # ⇒ **改守卫 = 解除守卫**，而没有人复核。
    r"^backend/app/tests/(?:.*/)?test_ci_.*\.py$",
    # ⚠️ 守卫用例的**共享替身**（`_ci_*_support.py`）与守卫用例**同族**：
    # 替身提供「失败集合」「分片划分」等观测对象 ⇒ 改替身就能让守卫**恒绿**
    #   （例如把 `redLogWith` 改成永远返回空失败集合 ⇒ 全部对差用例看到「净新增 0」）
    # ⇒ 与守卫用例一样进 CI 相关面。
    # 刻意只认 `_ci_*_support.py`（CI 专属）⇒ 不误伤 `_agent_orchestration_runner_support.py`
    # 这类与判据无关的通用替身。
    r"^backend/app/tests/(?:.*/)?_ci_.*_support\.py$",
    r"^\.github/CODEOWNERS$",
)

# 拉取失败时可能被抛出的异常类型（`gh` 调用 + JSON 解析）。
_FETCH_ERRORS = (
    RuntimeError,
    OSError,
    ValueError,
    KeyError,
    IndexError,
    TypeError,
    AttributeError,
    subprocess.SubprocessError,
)


@dataclass(frozen=True)
class CiSurfaceInfo:
    """「CI 相关面」判定结果。

    | 字段 | 含义 |
    |---|---|
    | `changedFiles` | **PR 自己**改的文件（三点语义，`P`）—— 用于**措辞** |
    | `mainAdvanceFiles` | main 在 `[M_base, M_T]` 之间的内容推进（`A`）—— 用于**措辞** |
    | `netChangedFiles` | **判定集合** = `A ∪ P`（两次 run 实际跑的那两份代码的差异） |
    | `unattributedCiFiles` | 判定集合里属于 CI 相关面、但**归因不到本 PR** 的文件 |
    | `mainTipSha` | `M_T` = PR run **事件派发**那一刻 main 的 tip（= 那次合并的 main 侧底座） |
    | `baseTrustError` | **基线 run 不可信**（不是 main 上的 push run）⇒ 对差不成立 |
    | `error` | **判定不出来** ⇒ 失败集合的可比性**未知** ⇒ 未知不等于没有 ⇒ 保守拦下 |

    ⚠️ `baseTrustError` 与 `error` 是**两件事**：
    前者 = 基线本身不是「main 的 push run」（⇒ 对差的前提根本不成立 ⇒ 退出码 3）；
    后者 = 「PR 改了哪些 CI 文件」没判出来（⇒ 退出码 6，仍可人工仲裁）。
    """

    baseSha: str | None
    prSha: str | None
    mainTipSha: str | None
    changedFiles: tuple[str, ...]
    mainAdvanceFiles: tuple[str, ...]
    netChangedFiles: tuple[str, ...]
    unattributedCiFiles: tuple[str, ...]
    baseTrustError: str | None
    error: str | None


def detectCiSurfaceChanges(changedFiles: Iterable[str]) -> tuple[str, ...]:
    """挑出「CI 相关面」的改动文件。

    这些文件一旦被 PR 改动，**失败集合就不再可比**：PR 可以通过
    conftest 的 `pytest_collection_modifyitems`、workflow 的 `--deselect`、
    `ci_shard.py` 的分片清单让失败用例**根本不产生 FAILED 行**
    ⇒ 失败集合与基线完全相同 ⇒ 假「净新增 0」⇒ 而 CI 还全绿 ⇒ 没有二次防线。
    """
    return tuple(
        name
        for name in changedFiles
        if any(re.match(pattern, name) for pattern in _CI_SURFACE_PATTERNS)
    )


def _baseTrustError(repo: str, baseInfo: RunHeadInfo) -> str | None:
    """基线 run 必须是「**本仓库 main 分支上的 push run**」，否则不可信。

    ⚠️ 为什么工具侧要**独立复核**一遍（不能只靠 workflow 的查询串）：
    GitHub 的 `runs?branch=<name>` 过滤匹配的是 `head_branch`，**不区分事件**
    （2026-11-07 实测：`branch=feat/merge-gate-flake-whitelist` 返回的是
    `event=pull_request` 的 run）⇒ 一个**名叫 main 的 fork 分支**发起的 PR
    也会命中 `branch=main` ⇒ 它的 run 可能被当成「main 的基线」
    ⇒ 基线变成**攻击者自己的代码** ⇒ 净变更腿被中和 ⇒ CI 相关面守卫失效。

    ⇒ 三条硬条件（缺一不可）：
    1. `event == "push"`（排除 `pull_request` / `workflow_dispatch` / `schedule`）；
    2. `head_branch == "main"`（基线必须来自 main）；
    3. `head_repository == repo`（排除 fork 上那个「也叫 main」的分支）。

    ⚠️ 第 3 条用**大小写不敏感**比较（GitHub 的仓库名大小写不敏感，
    实测 API 侧会原样返回 `Sunql/QA` 这类形态 ⇒ 严格相等会把**合法的基线**
    判成不可信 ⇒ 误判退出码 3 ⇒ 白拦）。
    """
    if baseInfo.event != "push":
        return (
            f"基线 run 的触发事件是 {sanitizeForReport(baseInfo.event)!r} 而不是 'push'"
            " ⇒ 它不是 main 上的 push run ⇒ 失败集合不可比"
        )
    if baseInfo.headBranch != "main":
        return (
            f"基线 run 的分支是 {sanitizeForReport(baseInfo.headBranch)!r} 而不是 'main'"
            " ⇒ 它不是 main 的快照 ⇒ 失败集合不可比"
        )
    if baseInfo.headRepo.lower() != repo.lower():
        return (
            f"基线 run 的分支在 {sanitizeForReport(baseInfo.headRepo)!r} 而不是 {repo!r}"
            " ⇒ 它可能是 fork 上一个同名的分支 ⇒ 失败集合不可比"
        )
    return None


def _emptySurface(
    baseSha: str | None,
    prSha: str | None,
    error: str,
    baseTrustError: str | None = None,
    mainTipSha: str | None = None,
) -> CiSurfaceInfo:
    """一个「判定不出来」的判定结果（各清单一律为空）。"""
    return CiSurfaceInfo(
        baseSha=baseSha,
        prSha=prSha,
        mainTipSha=mainTipSha,
        changedFiles=(),
        mainAdvanceFiles=(),
        netChangedFiles=(),
        unattributedCiFiles=(),
        baseTrustError=baseTrustError,
        error=error,
    )


def _fetchRunHeads(
    repo: str, baseRunId: str, prRunId: str
) -> tuple[RunHeadInfo | None, RunHeadInfo | None, str | None]:
    """取两次 run 的 head 信息 ⇒ `(baseInfo, prInfo, error)`。

    ⚠️ 失败**不抛异常** ⇒ 以 `error` 字符串返回 ⇒ 调用方走「判定不出来」的分支。
    ⚠️ PR run 要的是**完整**信息（`run_started_at` 决定 `M_T` 的取数上界）
    ⇒ 不能只取 `head_sha`。
    """
    try:
        baseInfo = fetchRunHeadInfo(repo, baseRunId)
    except _FETCH_ERRORS as error:
        return None, None, f"拉取基线 run 的信息失败：{sanitizeForReport(str(error))}"
    try:
        prInfo = fetchRunHeadInfo(repo, prRunId)
    except _FETCH_ERRORS as error:
        return baseInfo, None, f"拉取 PR run 的信息失败：{sanitizeForReport(str(error))}"
    if not prInfo.headSha:
        return baseInfo, None, "PR run 的 head_sha 为空 ⇒ 无法确定 PR 侧改动"
    return baseInfo, prInfo, None


def _fetchMainTips(repo: str, prInfo: RunHeadInfo) -> tuple[tuple[str, str] | None, str | None]:
    """取 `M_T` 的**两条腿** ⇒ `((run 腿, commit-date 腿), error)`。

    ⚠️ 上界锚 **`created_at`**（事件派发时刻），不是 `run_started_at`：
    `pull_request` 事件的**合并 commit 在派发那一刻就被冻结**了
    ⇒ 复跑**不会**重新计算它 ⇒ 复跑检出的仍是**同一个**合并 commit
    ⇒ 它的 main 侧底座是**派发那一刻**的 main。
    实测（PR #105 run `41908498247`）：`created_at=2026-11-04T20:38:19Z`、
    `run_started_at=2026-11-04T22:24:11Z`（attempt=2）；合并 commit `57a267a9`
    的第一父 = `83581e10`（= 派发那一刻的 main）；attempt-2 的 job 日志写着
    `git fetch … +57a267a93f…:refs/remotes/pull/105/merge`。

    ⚠️ **两条腿都要取**（2026-11-07 安全评审 HIGH-2）：
    - **run 腿**（main 的 push run 列表）：main 的那次 push **没触发** workflow、
      或它的 run **创建得太晚**（排队 > 宽限）⇒ 偏旧 ⇒ `A` 腿漏算 ⇒ fail-open；
    - **commit-date 腿**（commits API）：commit 日期 ≠ push 时刻
      （rebase / 时钟偏斜）⇒ 自己也可能偏旧。
    ⇒ `A` 腿取**两条腿的并集** ⇒ 谁漏掉的那一段，另一条兜住。
    """
    try:
        cutoff = mainTipCutoff(
            createdAt=prInfo.createdAt, runStartedAt=prInfo.runStartedAt
        )
    except ValueError as error:
        return None, sanitizeForReport(str(error))
    cutoffIso = cutoff.isoformat()
    try:
        runBasedSha = fetchMainTipShaAt(repo, cutoffIso)
    except _FETCH_ERRORS as error:
        return None, (
            "无法确定 PR run 的 main 侧底座（PR 的 CI 检出的是 `refs/pull/N/merge`，"
            f"不是 PR 头）：{sanitizeForReport(str(error))}"
        )
    try:
        commitDateSha = fetchMainTipShaByCommitDate(repo, cutoffIso)
    except _FETCH_ERRORS as error:
        return None, (
            "无法确定 main 的推进上界（commits API 取不到 main 在那一刻的 tip）"
            f"：{sanitizeForReport(str(error))}"
        )
    return (runBasedSha, commitDateSha), None


def _fetchMainAdvanceFiles(
    repo: str, baseSha: str, mainTipSha: str, commitDateTipSha: str
) -> tuple[tuple[str, ...] | None, str | None]:
    """取 `A` = main 在 `[M_base, M_T]` 之间的内容推进 ⇒ `(files, error)`。

    ⚠️ **两条腿取并集**（2026-11-07 安全评审 HIGH-2）：`M_T` 有两条取法，
    各自都可能偏旧 ⇒ 单用一条会漏掉 main 的推进 ⇒ 两次 run 的 CI 定义
    可能其实不同 ⇒ 失败集合不可比 ⇒ **fail-open**。
    ⇒ 并集 = 「两条腿各自认为 main 推进了什么」的合集 ⇒ 漏的那条由另一条兜住。
    """
    try:
        byRunBased = fetchNetChangedFiles(repo, baseSha, mainTipSha)
        byCommitDate = (
            byRunBased
            if commitDateTipSha == mainTipSha
            else fetchNetChangedFiles(repo, baseSha, commitDateTipSha)
        )
    except _FETCH_ERRORS as error:
        return None, (
            "拉取 main 的内容推进清单失败："
            f"{sanitizeForReport(str(error))}"
        )
    return tuple(sorted(set(byRunBased) | set(byCommitDate))), None


def _fetchPrChangedFiles(
    repo: str, mainTipSha: str, prSha: str
) -> tuple[tuple[str, ...] | None, str | None]:
    """取 `P` = PR **自己**改的文件（三点语义）⇒ `(files, error)`。

    ⚠️ 分叉点用 `merge_base(M_T, PR 头)`：PR 分支若是从更早的 main 起，
    分叉点就是那个更早的 commit ⇒ `P` 只含 PR 自己的改动（不含 main 的）。
    """
    try:
        return fetchChangedFiles(repo, mainTipSha, prSha), None
    except _FETCH_ERRORS as error:
        return None, (
            "拉取 PR 相对分叉点的改动文件清单失败："
            f"{sanitizeForReport(str(error))}"
        )


def resolveCiSurface(repo: str, baseRunId: str, prRunId: str) -> CiSurfaceInfo:
    """算出「两次 run 实际跑的代码，在 CI 相关面上是否不同」。

    ⚠️ **判定集合 = `A ∪ P` ∩「CI 相关面」**（`A` = main 的推进、`P` = PR 自己的改动）；
    两者分开保存**只用于措辞**（谁的责任、要不要重新跑 CI）。

    ⚠️ **基线必须先验可信**（`_baseTrustError`）：基线不是 main 的 push run ⇒
    整个对差的前提不成立 ⇒ 不进入后面的采集。

    **任何一步失败都不抛异常** —— 失败会以 `error` 字段返回 ⇒ 调用方
    走「判定不出来 ⇒ 保守拦下」的分支，而不是崩在网络错误上。
    ⚠️ 判定集拉不到时**不退回三点 diff**：那正是本模块要消除的假阳性来源。
    """
    baseInfo, prInfo, error = _fetchRunHeads(repo, baseRunId, prRunId)
    if error is not None or baseInfo is None or prInfo is None:
        return _emptySurface(
            None if baseInfo is None else baseInfo.headSha,
            None if prInfo is None else prInfo.headSha,
            error or "拉取两次 run 的信息失败",
        )
    baseSha = baseInfo.headSha
    prSha = prInfo.headSha
    trustError = _baseTrustError(repo, baseInfo)
    if trustError is not None:
        return _emptySurface(
            baseSha,
            prSha,
            f"基线 run 不可信：{trustError}",
            baseTrustError=trustError,
        )
    mainTips, error = _fetchMainTips(repo, prInfo)
    if error is not None or mainTips is None:
        return _emptySurface(baseSha, prSha, error or "取 main 的 tip 失败")
    mainTipSha, commitDateTipSha = mainTips
    mainAdvance, error = _fetchMainAdvanceFiles(repo, baseSha, mainTipSha, commitDateTipSha)
    if error is not None or mainAdvance is None:
        return _emptySurface(baseSha, prSha, error or "取 main 的内容推进失败", mainTipSha=mainTipSha)
    prChanged, error = _fetchPrChangedFiles(repo, mainTipSha, prSha)
    if error is not None or prChanged is None:
        return _emptySurface(baseSha, prSha, error or "取 PR 的改动清单失败", mainTipSha=mainTipSha)
    return _buildSurface(baseSha, prSha, mainTipSha, mainAdvance, prChanged)


def _buildSurface(
    baseSha: str,
    prSha: str,
    mainTipSha: str,
    mainAdvance: tuple[str, ...],
    prChanged: tuple[str, ...],
) -> CiSurfaceInfo:
    """把两段差异合成判定结果（判定集合 = `A ∪ P`）。"""
    judgment = tuple(sorted(set(mainAdvance) | set(prChanged)))
    netCiFiles = detectCiSurfaceChanges(judgment)
    prCiFiles = detectCiSurfaceChanges(prChanged)
    return CiSurfaceInfo(
        baseSha=baseSha,
        prSha=prSha,
        mainTipSha=mainTipSha,
        changedFiles=tuple(sorted(prChanged)),
        mainAdvanceFiles=tuple(sorted(mainAdvance)),
        netChangedFiles=judgment,
        unattributedCiFiles=tuple(sorted(set(netCiFiles) - set(prCiFiles))),
        baseTrustError=None,
        error=None,
    )
