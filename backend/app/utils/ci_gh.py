"""`gh` 命令适配层：把 GitHub Actions 的 run / job / log 拉成结构化数据。

为什么单独一个模块：本模块是**唯一**会拼 API 路径、唯一会调 `gh` 的地方 ⇒
输入校验、凭据脱敏、超时处理集中在这里，不散落在对差逻辑里。

⚠️ 两条硬纪律：

1. **参数走数组，绝不走 shell**（`subprocess.run(["gh", *args], ...)`）⇒
   没有 shell 插值面。
2. **每个会拼 URL 的函数自己校验 repo / runId / sha** —— 只在 CLI 入口校验
   是不够的：这些函数是模块的公开 API，被复用时未校验的输入即刻变成路径注入。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime

from app.utils.ci_text import sanitizeForReport

# gh 单次调用的超时上限（秒）。没有它，gh 在没凭据时会挂住直到 CI 超时。
_GH_TIMEOUT_SEC = 300
# gh stderr 的最大保留长度
_MAX_STDERR_LEN = 2048

# `--repo` / `--run-id` / sha 的合法形态（它们会被拼进 gh 的 API 路径）。
# ⚠️ owner/repo 的**每段首字符**必须是非 `.` 的标识符字符：否则 `--repo ../..`
# 会通过校验，而 `repos/../../actions/runs/<id>/jobs` 被服务端规范化后可打到
# 另一个 API 端点（实测：两斜杠形态能过校验，三斜杠被拒 ⇒ 守卫用例要写两斜杠那一种）。
# ⚠️ 结尾锚点用 `\Z` 而不是 `$`：Python 的 `$` **允许结尾换行**
# ⇒ `"a"*40 + "\n"` 能通过 `^[0-9a-f]{40}$`（实测）⇒ 校验形同虚设。
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_RUN_ID_PATTERN = re.compile(r"^[0-9]+\Z")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}\Z")
# 分支名（会被拼进 `repos/{repo}/commits/{branch}` 路径）：
# 只允许 GitHub 分支名的常规字符 ⇒ 排除 `..`（路径穿越）与空格/换行。
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,254}\Z")

# GitHub 的 ISO8601 时间戳（`2026-11-07T01:23:45Z`，可能带毫秒与时区偏移）。
_ISO_TIMESTAMP_PATTERN = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)

# 「沿 main 的第一父链回溯」的**最大跳数**（防无限回溯 ⇒ 判不出来）。
#
# ⚠️ 为什么要有上限：main 高速推进时，回溯到「事件派发那一刻」的 tip 可能要几十跳
#   ⇒ 而每跳都要一次 API 调用 ⇒ 必须封顶 ⇒ 超上限 ⇒ 报错（fail-closed），
#   **不能**「回溯到跳不动为止然后拿个错的 sha 当底座」⇒ 那会让 `A` 腿算错 ⇒ fail-open。
_MAX_FIRST_PARENT_HOPS = 100

# 一次取回 main 的近期提交历史的条数（够覆盖「回溯到派发时刻」即可，
# 超出部分按需单个补取）。
_MAIN_HISTORY_PAGE_SIZE = 100


@dataclass(frozen=True)
class RunHeadInfo:
    """一轮 run 的 head 信息。

    `event` = 触发这次 run 的事件（`push` / `pull_request` / …）。

    ⚠️ **基线必须是 `push` run**：GitHub 的 `runs?branch=<name>` 过滤匹配的是
    `head_branch`，**不区分事件**（实测：`branch=feat/merge-gate-flake-whitelist`
    返回的是 `event=pull_request` 的 run）⇒ 一个从名叫 `main` 的分支发起的
    fork PR 也会命中 `branch=main` ⇒ 它的 run 可能被当成「main 的基线」
    ⇒ 基线变成攻击者自己的代码 ⇒ 净变更腿被中和 ⇒ CI 相关面守卫失效。
    ⇒ 工具侧独立复核一遍（**不能只靠 workflow 的查询串**）。
    """

    headSha: str
    headBranch: str
    headRepo: str
    event: str
    createdAt: str
    """run 的**创建**时间（`created_at`）= **事件派发时刻** ⇒ 复跑时**不变**。

    ⚠️ 判定「PR run 的 main 侧底座」必须用**它**（不是 `run_started_at`）：
    `pull_request` 事件的**合并 commit 在事件派发那一刻就被冻结**了
    ⇒ 复跑（re-run）**不会**重新计算它 ⇒ 复跑检出的仍是**同一个**合并 commit
    ⇒ 它的 main 侧底座是**派发那一刻**的 main。

    实测（PR #105 run `41908498247`）：`created_at=2026-11-04T20:38:19Z`、
    `run_started_at=2026-11-04T22:24:11Z`（attempt=2）；合并 commit `57a267a9`
    的 committer.date = `20:38:18Z`（= 派发时刻的前 1 秒）、第一父 = `83581e10`
    （= 派发那一刻的 main）；attempt-2 的 job 日志写着
    `git fetch … +57a267a93f…:refs/remotes/pull/105/merge`
    ⇒ 复跑跑的仍是**同一个**合并 commit。
    """

    runStartedAt: str
    """**本次尝试**真正开跑的时间（`run_started_at`）⇒ 复跑时**会更新**。

    ⚠️ **不能**用它当 main 侧底座的上界（理由见 `createdAt`）：
    复跑在**新时刻**开跑，但它检出的是**派发时冻结的**那个合并 commit
    ⇒ 用它取到的 `M_T` 是**派发之后**的 main ⇒ `A` 腿会把「派发之后 main
    才推进的 CI 改动」也算进来 ⇒ 判定集偏大 ⇒ 假拦；
    更糟的是闸门侧若同样用它取基线 ⇒ 基线树与执行树都落到那个更晚的 main
    ⇒ `A` 变成空集 ⇒ 两次 run 实际跑的 CI 定义不同却判「可合并」⇒ **fail-open**。

    ⇒ 只作 `created_at` 缺失时的**兜底**（偏保守 ⇒ 宁可多仲裁一次）。
    """


def _runGh(args: list[str]) -> str:
    """调用 `gh`（参数数组，**绝不**走 shell）。

    两道加固：
    - 清掉 `GH_DEBUG`：它会让 gh 把**请求头**（含 token）dump 进 stderr
      ⇒ 落进报告/终端。
    - 设 `GH_PROMPT_DISABLED=1`：没有凭据时 gh 会挂交互提示直到 300s 超时。
    """
    env = {**os.environ, "GH_PROMPT_DISABLED": "1"}
    env.pop("GH_DEBUG", None)
    completed = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=_GH_TIMEOUT_SEC,
        env=env,
    )
    if completed.returncode != 0:
        stderr = sanitizeForReport(completed.stderr.strip())[:_MAX_STDERR_LEN]
        raise RuntimeError(f"gh {' '.join(args)} 失败（退出码 {completed.returncode}）：{stderr}")
    return completed.stdout


def validateRepo(repo: str) -> str | None:
    """`owner/repo` 非法时给出原因；合法则返回 None。

    **下沉到每个会拼 URL 的函数入口** —— 只在 CLI 校验是不够的：
    这些函数是模块的公开 API，被复用时未校验输入即刻变成路径注入。
    """
    if not _REPO_PATTERN.match(repo):
        return f"--repo 非法（应为 owner/repo）：{sanitizeForReport(repo)}"
    return None


def validateRunId(runId: str) -> str | None:
    """run id 非法时给出原因；合法则返回 None。"""
    if not _RUN_ID_PATTERN.match(runId):
        return f"run id 非法（应为纯数字）：{sanitizeForReport(runId)}"
    return None


def _parseJobs(payload: str) -> list[dict]:
    """解析 gh 返回的 job 列表，合并分页。

    兼容三种形态：
    - `{"jobs": [...]}` —— 单页对象（默认形状）；
    - `[{...}, {...}]` —— `--paginate --slurp` 的**页数组**（每页形如 `{"jobs": [...]}`）
      ⇒ 必须把各页的 job **合并**，否则只看到第一页；
    - `[{...job...}, ...]` —— 直接的 job 数组。
    """
    parsed = json.loads(payload)
    pages = parsed if isinstance(parsed, list) else [parsed]
    rawItems: list[dict] = []
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError(f"gh 返回了意料之外的 JSON 结构：{type(page).__name__}")
        if "jobs" in page:
            rawItems.extend(page["jobs"])
        else:
            rawItems.append(page)
    return rawItems


def fetchJobPayloads(repo: str, runId: str) -> list[dict]:
    """取一轮 run 的全部 job（原始 dict 形态）。

    `--slurp` 是必需的：`--paginate` 对**对象形状**的响应不会合并，而是把各页
    原样拼接（实测 gh 2.102）⇒ 多页时 `json.loads` 直接炸 `Extra data`。
    """
    invalidReason = validateRepo(repo) or validateRunId(runId)
    if invalidReason is not None:
        raise ValueError(invalidReason)
    payload = _runGh(
        ["api", f"repos/{repo}/actions/runs/{runId}/jobs", "--paginate", "--slurp"]
    )
    return _parseJobs(payload)


def fetchJobLog(repo: str, jobId: int) -> str:
    """取单个 job 的日志文本。

    ⚠️ `jobId` **必须自己校验**（它会被拼进 API 路径）：
    `"../../runs/12345"` 会拼出 `repos/{repo}/actions/jobs/../../runs/12345/logs`
    ⇒ 服务端规范化后打到**另一个端点**（与 `--repo ../..` 同一族）。
    """
    invalidReason = validateRepo(repo)
    if invalidReason is not None:
        raise ValueError(invalidReason)
    if isinstance(jobId, bool) or not isinstance(jobId, int) or jobId <= 0:
        raise ValueError(f"job id 非法（应为正整数）：{sanitizeForReport(str(jobId))}")
    return _runGh(["api", f"repos/{repo}/actions/jobs/{jobId}/logs", "--allow-escape-sequences"])


def fetchRunHeadSha(repo: str, runId: str) -> str:
    """取一轮 run 的 head commit sha。

    用途：两次 run 的 head sha 就是「基线那次跑到哪」与「PR 跑到哪」两个点
    ⇒ 两次 run 跑的那两份代码哪里不同 ⇒ 判断是否动了「CI 相关面」。
    """
    invalidReason = validateRepo(repo) or validateRunId(runId)
    if invalidReason is not None:
        raise ValueError(invalidReason)
    payload = _runGh(["api", f"repos/{repo}/actions/runs/{runId}"])
    data = json.loads(payload)
    sha = str(data.get("head_sha") or "")
    if not _SHA_PATTERN.match(sha):
        raise ValueError(f"run {runId} 的 head_sha 形态异常：{sanitizeForReport(sha)}")
    return sha


def fetchRunHeadInfo(repo: str, runId: str) -> RunHeadInfo:
    """取一轮 run 的 head commit / 分支名 / 分支所在仓库 / 触发事件。

    `head_repository.full_name` 用来区分「同仓 PR」与「fork PR」：
    fork 的分支不在本仓库 ⇒ 没法查它的当前 head ⇒ 新鲜度判定要另作处理。

    `event` 用来判定「这轮 run 是不是 push run」⇒ 基线必须是 push run
    （`branch=` 过滤不区分事件 ⇒ PR run 也会命中）。
    """
    invalidReason = validateRepo(repo) or validateRunId(runId)
    if invalidReason is not None:
        raise ValueError(invalidReason)
    payload = _runGh(["api", f"repos/{repo}/actions/runs/{runId}"])
    data = json.loads(payload)
    headSha = str(data.get("head_sha") or "")
    if not _SHA_PATTERN.match(headSha):
        raise ValueError(f"run {runId} 的 head_sha 形态异常：{sanitizeForReport(headSha)}")
    headBranch = str(data.get("head_branch") or "")
    headRepoData = data.get("head_repository") or {}
    headRepo = str(headRepoData.get("full_name") or "") if isinstance(headRepoData, dict) else ""
    event = str(data.get("event") or "")
    createdAt = str(data.get("created_at") or "")
    runStartedAt = str(data.get("run_started_at") or "")
    return RunHeadInfo(
        headSha=headSha,
        headBranch=headBranch,
        headRepo=headRepo,
        event=event,
        createdAt=createdAt,
        runStartedAt=runStartedAt,
    )


def fetchBranchHeadSha(repo: str, branch: str) -> str:
    """取某个分支**当前**的 head commit sha。

    ⚠️ `branch` 会被拼进 API 路径 ⇒ 必须校验（分支名来自 GitHub，
    对 fork PR 而言是**外部可控**的 ⇒ 不能当可信输入）。
    """
    invalidReason = validateRepo(repo)
    if invalidReason is not None:
        raise ValueError(invalidReason)
    if not _BRANCH_PATTERN.match(branch) or ".." in branch:
        raise ValueError(f"分支名非法：{sanitizeForReport(branch)}")
    payload = _runGh(["api", f"repos/{repo}/commits/{branch}"])
    data = json.loads(payload)
    sha = str(data.get("sha") or "")
    if not _SHA_PATTERN.match(sha):
        raise ValueError(f"分支 {branch} 的 head sha 形态异常：{sanitizeForReport(sha)}")
    return sha


# --------------------------------------------------------------------------
# main 的 tip（= PR run 的 main 侧底座）
# --------------------------------------------------------------------------
#
# ⚠️ **为什么需要它**：`ci.yml` 对 `pull_request` 触发，三个 `actions/checkout`
#   **都不带 `with: ref:`** ⇒ 检出 `refs/pull/N/merge` ⇒ PR 的 CI 跑的是
#   **merge(main@事件派发时刻, PR 头)** ⇒ 判定「两次 run 的 CI 定义是否相同」
#   必须知道那次合并用的 **main 侧底座**。
#
# ⚠️ **为什么不用 `pulls/{n}.merge_commit_sha`**：
#   ① 它是 `refs/pull/N/merge` 的**当前值** ⇒ main 一推进它就变 ⇒ 判定用的
#      「第二棵树」可能**不是**那次 run 实际执行的树 ⇒ 假拦 + fail-open；
#   ② PR 合并后该 ref 消失（实测 404）⇒ 历史 run 的合并树无法回溯。
#
# ⇒ 改用「main 的 push run 列表」反推：`M_T` = 不晚于（PR run **事件派发时刻**
#   + 宽限）的**最新一次 main push run** 的 head sha。
#   实测（PR #111）：PR run `42281369007` 派发于 `2026-11-06T19:38:13Z`，
#   之前最新的 main push run = `87f8e8e6…`（run `42281237159`，创建于 19:37:16Z）
#   ⇒ 正是 job 日志里的合并底座
#   （`HEAD is now at 23db713 Merge 1d226997… into 87f8e8e6…`）。
#
# ⚠️ **上界锚 `created_at`（事件派发时刻），不是 `run_started_at`**：
#   `pull_request` 事件的**合并 commit 在派发那一刻就被冻结**了
#   ⇒ 复跑（re-run）**不会**重新计算它 ⇒ 复跑检出的仍是**同一个**合并 commit
#   ⇒ 它的 main 侧底座是**派发那一刻**的 main。
#   实测（PR #105 run `41908498247`，attempt=2）：合并 commit `57a267a9` 的
#   committer.date = `20:38:18Z`（= run 的 `created_at` `20:38:19Z` 的前 1 秒）、
#   第一父 = `83581e10`；attempt-2 的 job 日志写着
#   `git fetch … +57a267a93f…:refs/remotes/pull/105/merge` ⇒ **同一个**合并 commit。
#
# ⚠️ 取数只信**服务端给的字段**，不读 job 日志（PR 作者可控 stdout，
#   与本仓 CODEOWNERS 记录在案的威胁模型冲突）。


def _parseIsoTimestamp(value: str) -> datetime | None:
    """解析 GitHub 的 ISO8601 时间戳；形态不符返回 None。"""
    text = (value or "").strip()
    if not _ISO_TIMESTAMP_PATTERN.match(text):
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def mainTipCutoff(*, createdAt: str) -> datetime:
    """PR run 的「main tip 取数上界」= **事件派发时刻**（`created_at`），**不加宽限**。

    ⚠️ 上界必须锚在 **`created_at`**（事件派发时刻），不是 `run_started_at`：
    `pull_request` 事件的**合并 commit 在派发那一刻就被冻结**了
    ⇒ 复跑（re-run）**不会**重新计算它 ⇒ 复跑检出的仍是**同一个**合并 commit
    ⇒ 它的 main 侧底座是**派发那一刻**的 main。

    实测（PR #105 run `41908498247`）：`created_at=2026-11-04T20:38:19Z`、
    `run_started_at=2026-11-04T22:24:11Z`（attempt=2）；合并 commit `57a267a9`
    的 committer.date = `20:38:18Z`、第一父 = `83581e10`（= 派发那一刻的 main）；
    attempt-2 的 job 日志写着
    `git fetch … +57a267a93f…:refs/remotes/pull/105/merge`
    ⇒ 复跑跑的仍是**同一个**合并 commit。

    ⇒ 锚 `run_started_at` ⇒ `M_T` 取到**派发之后**的 main ⇒ `A` 腿把
    「派发之后 main 才推进的 CI 改动」也算进来 ⇒ 判定集偏大 ⇒ 假拦；
    而闸门侧若同样用它取基线 ⇒ 基线树与执行树都落到那个更晚的 main
    ⇒ `A` 变成空集 ⇒ 两次 run 实际跑的 CI 定义不同却判「可合并」⇒ **fail-open**。

    ⚠️⚠️ **2026-11-07 代码评审 MEDIUM-1：宽限已删除**（原来 `+120s`）：
    闸门侧（`merge-gate.yml`）取基线用的上界是 `PR_RUN_CREATED_AT`（**无宽限**）
    ⇒ 工具侧多给 2 分钟 ⇒ 工具侧的 `M_T` 比闸门侧的基线**更新**
    ⇒ `A` 腿把「派发之后 main 才推进的 CI 改动」也算进来
    ⇒ **判定集偏大 ⇒ 假拦**（main 在 PR 派发后 2 分钟内推进过就中招）。
    ⇒ 两边必须**同一个上界、同一个比较符** ⇒ 工具侧也用 `created_at` 原值。

    ⚠️ **`created_at` 缺失 ⇒ 判定不出来**（不再回退 `run_started_at`）：
    闸门侧没有回退（`PR_RUN_CREATED_AT` 为空 ⇒ 直接 fail-closed）
    ⇒ 工具侧若回退 ⇒ 两边上界不同 ⇒ 又是「基线树 ≠ 执行树」⇒ fail-open。
    ⇒ **两边必须同时 fail-closed**。
    """
    dispatched = _parseIsoTimestamp(createdAt)
    if dispatched is None:
        raise ValueError(
            "PR run 的 created_at 缺失或形态异常"
            " ⇒ 算不出 main 侧的取数上界 ⇒ 判定不出来"
        )
    return dispatched


def fetchMainTipShaAt(repo: str, cutoffIso: str) -> str:
    """取「不晚于 `cutoffIso` 的最新一次 main push run」的 head sha（= `M_T`）。

    ⚠️ **查询串里的 `branch=main&event=push` 不能当防线**：
    GitHub 的 `runs?branch=<name>` 匹配的是 `head_branch`、**不区分事件**
    （实测：`branch=feat/merge-gate-flake-whitelist` 返回的是 `event=pull_request`
    的 run）⇒ 一个**名叫 main 的 fork 分支**也会命中。
    ⇒ `_parseMainTipSha` 侧**独立复核** `event` / `head_branch` / `head_repository`。
    """
    invalidReason = validateRepo(repo)
    if invalidReason is not None:
        raise ValueError(invalidReason)
    payload = _runGh(
        [
            "api",
            f"repos/{repo}/actions/runs?branch=main&event=push&per_page=100",
            "--paginate",
            "--slurp",
        ]
    )
    return _parseMainTipSha(payload, cutoffIso, repo)


def _parseMainTipSha(payload: str, cutoffIso: str, repo: str) -> str:
    """从 main 的 push run 列表里挑出「不晚于上界的最新一次」的 head sha。

    三条硬条件（缺一不可，缺则该 run **不计**）：
    1. `event == "push"`（PR run 不算）；
    2. `head_branch == "main"`；
    3. `head_repository == 本仓库`（fork 分支同名 main 不算）。
    """
    parsed = json.loads(payload)
    pages = parsed if isinstance(parsed, list) else [parsed]
    cutoff = _parseIsoTimestamp(cutoffIso)
    if cutoff is None:
        raise ValueError(f"取数上界形态异常：{sanitizeForReport(cutoffIso)}")
    best: tuple[datetime, str] | None = None
    for page in pages:
        runs = page.get("workflow_runs") if isinstance(page, dict) else None
        if not isinstance(runs, list):
            raise ValueError("gh 返回了意料之外的 JSON 结构（缺 workflow_runs）")
        for run in runs:
            best = _considerMainRun(run, cutoff, repo, best)
    if best is None:
        raise ValueError(
            f"找不到不晚于 {sanitizeForReport(cutoffIso)} 的 main push run"
            " ⇒ 算不出 PR run 的 main 侧底座 ⇒ 判定不出来"
        )
    return best[1]


def _considerMainRun(
    run: dict,
    cutoff: datetime,
    repo: str,
    best: tuple[datetime, str] | None,
) -> tuple[datetime, str] | None:
    """把一条 run 纳入候选（过三条硬条件 + 时间窗）⇒ 返回更新后的最优候选。"""
    if not isinstance(run, dict):
        raise ValueError(f"run 条目不是对象：{type(run).__name__}")
    if str(run.get("event") or "") != "push":
        return best
    if str(run.get("head_branch") or "") != "main":
        return best
    headRepoData = run.get("head_repository") or {}
    headRepo = str(headRepoData.get("full_name") or "") if isinstance(headRepoData, dict) else ""
    if headRepo.lower() != repo.lower():
        return best
    createdAt = str(run.get("created_at") or "")
    created = _parseIsoTimestamp(createdAt)
    if created is None:
        raise ValueError(f"run 的 created_at 形态异常：{sanitizeForReport(createdAt)}")
    if created > cutoff:
        return best
    sha = str(run.get("head_sha") or "")
    if not _SHA_PATTERN.match(sha):
        raise ValueError(f"run 的 head_sha 形态异常：{sanitizeForReport(sha)}")
    candidate = (created, sha)
    if best is None:
        return candidate
    if candidate[0] == best[0] and candidate[1] != best[1]:
        # ⚠️ **同一秒里的两次 main push run** ⇒ 靠 sha 字典序分不出先后
        #   ⇒ 选哪个都是**猜** ⇒ 判不出来 ⇒ 报错（fail-closed）。
        #   （不报错的话，字典序靠后的那个会「赢」⇒ 底座可能取到**更旧**的
        #   commit ⇒ `A` 腿漏算 main 的推进 ⇒ fail-open。）
        raise ValueError(
            "main 上有两次 push run 的 created_at 相同但 head_sha 不同"
            " ⇒ 分不出哪一次更新 ⇒ 算不出 main 侧底座 ⇒ 判定不出来"
        )
    if candidate > best:
        return candidate
    return best


# --------------------------------------------------------------------------
# main 的 tip：**第二条腿**（沿 main 的**第一父链**回溯，不依赖 run 列表）
# --------------------------------------------------------------------------
#
# ⚠️ **为什么要有第二条腿**（2026-11-07 安全评审 HIGH-2）：
# `M_T` 若只从「main 的 push run 列表」反推，有两个**漏检**方向 ——
# ① main 的那次 push **没触发** workflow ⇒ run 列表里根本没有它 ⇒ `M_T` 偏旧；
# ② 那次 push 的 run **创建得晚**（晚于事件派发那一刻）⇒ 落在上界之外 ⇒ `M_T` 偏旧。
# ⇒ 偏旧 ⇒ `A` 腿漏掉 main 的推进 ⇒ 两次 run 的 CI 定义可能其实**不同**
# ⇒ 失败集合不可比 ⇒ 却判「可合并」⇒ **fail-open**。
#
# ⚠️⚠️ **第二条腿为什么必须沿「第一父链」**（2026-11-07 代码评审 MEDIUM-2）：
# 原来的实现是 `commits?sha=main&until=<上界>` ⇒ 它返回的是
# 「**从 main 可达**、且 commit 日期 ≤ 上界的最新一次提交」
# ⇒ 那个提交**可能是侧提交**（后来被 merge 进 main 的分支上的提交，
#    作者日期早于上界、但它进 main 的时刻在上界之后）
# ⇒ 它的 diff **不是** main 在那一刻的推进 ⇒ 把它算进 `A` 腿 ⇒ **假拦**
# （且归因会把 main 自己的 CI 改动说成「与本 PR 无关的 CI 面差异」）。
#
# ⇒ 改成：从 **main 的当前 tip** 出发，沿 **`parents[0]`**（第一父）回溯，
#   直到 commit 的日期 ≤ 上界 ⇒ 得到的必然是 **main 主线**在该时刻的 tip
# ⇒ 侧提交**永远不在主线**上 ⇒ 天然排除。
#
# ⚠️ 两条腿**取并集**用（见 `ci_surface.py`）：commit 日期 ≠ push 时刻
#   （rebase / 时钟偏斜）⇒ 第二条腿自己也会偏旧/偏新 ⇒ 单用任何一条都会漏。
# ⇒ 谁漏掉的那一段，另一条兜住。


def _fetchMainTipRefSha(repo: str) -> str:
    """取 **main 分支当前 tip** 的 commit sha（走 git ref，不靠提交列表的首条）。"""
    payload = _runGh(["api", f"repos/{repo}/git/ref/heads/main"])
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError(f"gh 返回了意料之外的 JSON 结构：{type(parsed).__name__}")
    sha = str((parsed.get("object") or {}).get("sha") or "")
    if not _SHA_PATTERN.match(sha):
        raise ValueError(f"main 的 tip sha 形态异常：{sanitizeForReport(sha)}")
    return sha


def _parseCommitEntries(entries: object) -> dict[str, tuple[datetime, str | None]]:
    """把 commits 响应解析成 `sha → (提交日期, 第一父 sha)` 的映射。

    ⚠️ **畸形条目一律报错**（fail-closed）：静默跳过 = 那条提交从历史里消失
    ⇒ 回溯链断掉 ⇒ 拿到的 tip 是错的 ⇒ `A` 腿算错 ⇒ fail-open。
    """
    if not isinstance(entries, list):
        raise ValueError(f"gh 返回了意料之外的 JSON 结构：{type(entries).__name__}")
    history: dict[str, tuple[datetime, str | None]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"commit 条目不是对象：{type(entry).__name__}")
        sha = str(entry.get("sha") or "")
        if not _SHA_PATTERN.match(sha):
            raise ValueError(f"commit 的 sha 形态异常：{sanitizeForReport(sha)}")
        commitData = entry.get("commit")
        dateRaw = ""
        if isinstance(commitData, dict):
            committer = commitData.get("committer")
            if isinstance(committer, dict):
                dateRaw = str(committer.get("date") or "")
        committed = _parseIsoTimestamp(dateRaw)
        if committed is None:
            raise ValueError(f"commit 的 committer.date 形态异常：{sanitizeForReport(dateRaw)}")
        parents = entry.get("parents")
        if not isinstance(parents, list):
            raise ValueError(f"commit 的 parents 形态异常：{type(parents).__name__}")
        firstParent: str | None = None
        if parents:
            first = parents[0]
            firstParent = str((first or {}).get("sha") or "") if isinstance(first, dict) else ""
            if not _SHA_PATTERN.match(firstParent):
                raise ValueError(f"commit 的第一父 sha 形态异常：{sanitizeForReport(firstParent)}")
        history[sha] = (committed, firstParent)
    return history


def _fetchMainHistory(repo: str) -> dict[str, tuple[datetime, str | None]]:
    """取 main 的近期提交历史（`sha → (日期, 第一父)`）。

    ⚠️ 一次取回一页（`_MAIN_HISTORY_PAGE_SIZE` 条）⇒ 回溯链基本都在里面；
    超出部分由 `_fetchCommitEntry` 单个补取。
    """
    payload = _runGh(
        [
            "api",
            f"repos/{repo}/commits?sha=main&per_page={_MAIN_HISTORY_PAGE_SIZE}",
        ]
    )
    history = _parseCommitEntries(json.loads(payload))
    if not history:
        raise ValueError("main 的提交历史为空 ⇒ 算不出 main 侧底座 ⇒ 判定不出来")
    return history


def _fetchCommitEntry(repo: str, sha: str) -> tuple[datetime, str | None]:
    """单个取回某个 commit 的（日期, 第一父）。"""
    payload = _runGh(["api", f"repos/{repo}/commits/{sha}"])
    history = _parseCommitEntries([json.loads(payload)])
    entry = history.get(sha)
    if entry is None:
        raise ValueError(f"commit {sha[:12]}… 的响应里没有它自己 ⇒ 判定不出来")
    return entry


def fetchMainFirstParentTipAt(repo: str, cutoffIso: str) -> str:
    """取「main 主线在 `cutoffIso` 那一刻的 tip」的 sha（沿第一父链回溯）。

    ⚠️ 这条腿**不依赖 run 列表** ⇒ main 的 push 没触发 workflow、
    或它的 run 创建得晚于事件派发，都不会让它偏旧（见本节上方的说明）。
    """
    invalidReason = validateRepo(repo)
    if invalidReason is not None:
        raise ValueError(invalidReason)
    cutoff = _parseIsoTimestamp(cutoffIso)
    if cutoff is None:
        raise ValueError(f"取数上界形态异常：{sanitizeForReport(cutoffIso)}")
    history = _fetchMainHistory(repo)
    current = _fetchMainTipRefSha(repo)
    for _ in range(_MAX_FIRST_PARENT_HOPS):
        entry = history.get(current)
        if entry is None:
            entry = _fetchCommitEntry(repo, current)
        committed, firstParent = entry
        if committed <= cutoff:
            return current
        if firstParent is None:
            return current
        current = firstParent
    raise ValueError(
        f"沿 main 的第一父链回溯超过 {_MAX_FIRST_PARENT_HOPS} 跳"
        " ⇒ 算不出 main 在该时刻的 tip ⇒ 判定不出来"
    )


# --------------------------------------------------------------------------
# 树对树求差（GitHub Trees API）
# --------------------------------------------------------------------------
#
# ⚠️ **为什么不能只用 compare API 的三点 diff**（`compare/{base}...{head}`）：
# 三点从 **merge-base** 起算 ⇒ 分支若含「重建 main 的 tree 的合成提交」
# （tree 与 main 的 tip 相同、但不是同一个 commit）⇒ main 的 tip **不是**分支的祖先
# ⇒ merge-base 退回到**老底座** ⇒ 三点 diff 把 **main 自己的** CI 文件
# （`.github/workflows/ci.yml`、`merge-gate.yml`、`CODEOWNERS`、`ci_*.py` …）
# 全算成「PR 改的」⇒ **假** `CI_SURFACE_CHANGED`。
# 2026-11-05 实证（base `b6e809a2` / head `97047b8b`）：三点 **158** 个文件
# vs 树对树净变更 **11** 个（后者正是 PR 的真实改动，CI 面命中 0）。
#
# ⚠️ GitHub REST 的 compare **只支持 `base...head`**（`base..head` 实测 404）
# ⇒ 两点语义必须**自己算** ⇒ 走 Trees API 取两棵整棵树再按「路径 → 对象 sha」比。
#
# ⚠️ compare API 的 `files` 数组**最多 300 项** ⇒ 归因集可能被**静默截断**
# ⇒ 截断的归因集与净变更取交集 ⇒ 会把 PR 真改的 CI 文件**排除**掉 ⇒ fail-open。
# ⇒ 本模块的归因集**不用** compare 的 files，而是：compare 只取 `merge_base_commit`，
#   差异用 Trees API 树对树算 ⇒ **无 300 项上限**。
#   （Trees API 自己的上限是响应里的 `truncated` ⇒ 已 fail-closed。）
#
# ⚠️ 树对树只回答「两份代码哪里不同」，**不回答「谁改的」**。
# 「PR 改了什么」由 merge-base 与 PR 头两棵树之差回答 ⇒ 两者取**交集**才是
# 「PR 改的、且两次 run 的代码确实不同的文件」（判定在 `ci_surface.py`）。

# 递归树条目类型：
# - `blob` = 普通文件；
# - `commit` = 子模块（一个路径级的指针）；
# - `tree` = 目录 ⇒ **必须排除**（目录不是文件；「一边多一个空目录」不是改动）。
#
# ⚠️ 判定时只收 `blob` / `commit`（见 `_buildTreeFileMap`）⇒ 这里只列
# **全部**条目类型（`tree` 用于校验递归是否生效）。
_TREE_ENTRY_TYPES = ("blob", "commit", "tree")


def fetchTreePathShas(repo: str, sha: str) -> dict[str, str]:
    """取某个 commit 的**整棵树**的「路径 → "mode:sha"」映射（只含文件级条目）。

    ⚠️ sha 会被拼进 API 路径 ⇒ 自己校验（模块纪律：每个拼 URL 的函数自己校验）。
    ⚠️ 值里带 `mode` ⇒ 只改权限位（chmod）的改动**也能看见**（blob sha 不变）。
    """
    invalidReason = validateRepo(repo)
    if invalidReason is not None:
        raise ValueError(invalidReason)
    if not _SHA_PATTERN.match(sha):
        raise ValueError(f"sha 形态异常：{sanitizeForReport(sha)}")
    payload = _runGh(["api", f"repos/{repo}/git/trees/{sha}?recursive=1"])
    return _parseTreePathShas(payload)


def _parseTreePathShas(payload: str) -> dict[str, str]:
    """解析 Trees API 的递归树响应 ⇒ 「路径 → "mode:sha"」（只含文件级条目）。

    ⚠️ **四项 fail-closed 校验**（任一不通过 ⇒ 报错 ⇒ 判定不出来 ⇒ 保守拦下）：

    1. `truncated` 键必须存在且为假 —— 它是**唯一**能证明清单完整的字段
       （缺失时按「不完整」处理，不能当「未截断」）；
    2. 每个条目必须有 str 形态的 `path` / `sha` / `type` / `mode`
       ⇒ 畸形条目**报错**，**不静默跳过**（跳过 = 该路径从清单里消失
       = 少一个 CI 相关面文件 = 守卫静默失效）；
    3. `type` 只能是 `blob` / `tree` / `commit`（不认识的类型 ⇒ 报错）；
    4. **递归确实生效**：每个目录条目（`type=tree`）都必有以 `<dir>/` 开头的子条目
       （git 不存空目录 ⇒ 这条不变式恒成立；实测本仓 368 个目录条目、0 个无子条目）。
       不成立 ⇒ 服务端没递归 ⇒ 清单里只剩根级条目 ⇒ **所有** CI 相关面文件都在
       子目录里 ⇒ 一个都看不见 ⇒ 守卫**静默失效**且 `truncated=false` 不报错。
    """
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError(f"gh 返回了意料之外的 JSON 结构：{type(parsed).__name__}")
    if "truncated" not in parsed:
        raise ValueError(
            "Trees API 响应缺少 truncated 字段 ⇒ 无法证明清单完整 ⇒ 须人工确认"
        )
    # ⚠️ 必须判 `is not False`（而不是「真值」）：`truncated` 若是 `"false"`（字符串）
    # 或 `0`，真值判断会把它当成「已截断」⇒ 白拦；反过来若将来服务端改成别的
    # 形态，真值判断又可能当成「未截断」⇒ fail-open。
    # ⇒ 唯一放行的形态是**布尔假** ⇒ 其它一律当「不完整」处理。
    if parsed["truncated"] is not False:
        raise ValueError(
            "Trees API 返回的递归树被服务端截断或形态异常"
            f"（truncated={parsed['truncated']!r}）⇒ 清单不完整，"
            "无法判定 PR 是否改动了 CI 相关面 ⇒ 须人工确认"
        )
    entries = parsed.get("tree")
    if not isinstance(entries, list):
        raise ValueError(f"Trees API 响应缺少 tree 数组：{type(entries).__name__}")
    return _buildTreeFileMap(entries)


def _buildTreeFileMap(entries: list) -> dict[str, str]:
    """把递归树条目整理成「路径 → "mode:sha"」（目录条目不进映射）。"""
    fileMap: dict[str, str] = {}
    directoryPaths: list[str] = []
    allPaths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"Trees API 的条目不是对象：{type(entry).__name__}")
        path = entry.get("path")
        objSha = entry.get("sha")
        objType = entry.get("type")
        mode = entry.get("mode")
        if not all(isinstance(value, str) for value in (path, objSha, objType, mode)):
            raise ValueError(f"Trees API 的条目字段形态异常：{entry!r}")
        if objType not in _TREE_ENTRY_TYPES:
            raise ValueError(f"Trees API 出现未知的条目类型：{objType!r}")
        allPaths.append(path)
        if objType == "tree":
            directoryPaths.append(path)
            continue
        fileMap[path] = f"{mode}:{objSha}"
    _assertTreeIsRecursive(directoryPaths, allPaths)
    return fileMap


def _assertTreeIsRecursive(directoryPaths: list[str], allPaths: list[str]) -> None:
    """不变式：递归响应**确实是整棵树**（任一不成立 ⇒ 清单不完整 ⇒ 报错）。

    三条：

    1. **祖先齐全**：每个路径的各级父目录都必须出现在响应里
       （git 的树里每个目录都是一个条目 ⇒ 递归列举必然带上）。
       ⇒ 响应被「拍平到根级」时立刻暴露。
    2. **目录有子条目**：每个目录条目都必有以 `<dir>/` 开头的子条目
       （git 不存空目录 ⇒ 这条恒成立；实测本仓 368 个目录条目、0 个无子条目）。
    3. **不是全平**：响应里至少要有一个**带目录前缀**的条目
       ⇒ 否则「一个目录条目都没有」会让前两条**真空通过**
       ⇒ 清单只剩根级条目 ⇒ 所有 CI 相关面文件都在子目录里 ⇒ 一个都看不见
       ⇒ 守卫**静默失效**且 `truncated=false` 不报错。

    ⚠️ 复杂度：O(条目数 × 目录深度) ⇒ 不用「每个目录扫一遍全表」的 O(n²) 写法。
    """
    pathSet = set(allPaths)
    ancestorsWithChild: set[str] = set()
    hasNestedPath = False
    for path in allPaths:
        parts = path.split("/")
        if len(parts) > 1:
            hasNestedPath = True
        for index in range(1, len(parts)):
            ancestorsWithChild.add("/".join(parts[:index]))
    for path in allPaths:
        parts = path.split("/")
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if ancestor not in pathSet:
                raise ValueError(
                    "Trees API 的响应缺少祖先目录条目"
                    f"（{sanitizeForReport(ancestor)}）"
                    "⇒ 响应不是完整的递归树 ⇒ 清单可能缺文件 ⇒ 须人工确认"
                )
    for directory in directoryPaths:
        if directory not in ancestorsWithChild:
            raise ValueError(
                "Trees API 的响应没有递归展开"
                f"（目录 {sanitizeForReport(directory)} 下没有任何子条目）"
                "⇒ 清单只含根级条目 ⇒ CI 相关面文件会全部隐身 ⇒ 须人工确认"
            )
    if not hasNestedPath:
        raise ValueError(
            "Trees API 的响应里没有任何带目录前缀的条目"
            " ⇒ 递归没生效、清单只含根级条目"
            " ⇒ CI 相关面文件会全部隐身 ⇒ 须人工确认"
        )


def _diffTreePathShas(
    baseTree: dict[str, str], headTree: dict[str, str]
) -> tuple[str, ...]:
    """两棵树的「路径 → "mode:sha"」求差（新增 / 删除 / 内容变化都算改动）。"""
    return tuple(
        sorted(
            path
            for path in set(baseTree) | set(headTree)
            if baseTree.get(path) != headTree.get(path)
        )
    )


def fetchNetChangedFiles(repo: str, baseSha: str, headSha: str) -> tuple[str, ...]:
    """取两个 commit 之间的**净变更**文件清单（树对树，与历史形状无关）。

    ⚠️ 与 `fetchChangedFiles`（三点语义）**不是**一回事：
    三点 = 「PR 相对分叉点改了什么」（受分支历史形状影响）；
    净变更 = 「两次 run 跑的那两份代码哪里不同」（只看内容）。
    """
    baseTree = fetchTreePathShas(repo, baseSha)
    headTree = fetchTreePathShas(repo, headSha)
    return _diffTreePathShas(baseTree, headTree)


def fetchMergeBaseSha(repo: str, baseSha: str, headSha: str) -> str:
    """取两个 commit 的 **merge-base**（分叉点）。

    GitHub REST 的 compare **只给三点**（`base...head`）⇒ 它算出来的 diff 就是
    `diff(merge_base, head)` ⇒ 只要能拿到 `merge_base_commit`，
    三点语义就能**自己**用 Trees API 算 ⇒ 不受 compare 的 300 项上限约束。
    """
    invalidReason = validateRepo(repo)
    if invalidReason is not None:
        raise ValueError(invalidReason)
    for label, sha in (("--base", baseSha), ("--head", headSha)):
        if not _SHA_PATTERN.match(sha):
            raise ValueError(f"{label} sha 形态异常：{sanitizeForReport(sha)}")
    payload = _runGh(
        ["api", f"repos/{repo}/compare/{baseSha}...{headSha}", "--paginate", "--slurp"]
    )
    return _parseMergeBaseSha(payload)


def _parseMergeBaseSha(payload: str) -> str:
    """从 compare 响应里取 `merge_base_commit.sha`。

    ⚠️ compare 的 `files` 数组**最多 300 项**（`--paginate` 只对 commits 分页，
    **不会**补齐 files）⇒ 归因集不能取自它 ⇒ 只取 `merge_base_commit`，
    差异改走 Trees API 树对树（**无 300 项上限**）。
    ⚠️ 拿不到 merge-base ⇒ 无法确定分叉点 ⇒ 判定不出来 ⇒ 必须**报错**。
    """
    parsed = json.loads(payload)
    pages = parsed if isinstance(parsed, list) else [parsed]
    for page in pages:
        if not isinstance(page, dict):
            continue
        mergeBase = page.get("merge_base_commit")
        if isinstance(mergeBase, dict):
            sha = str(mergeBase.get("sha") or "")
            if _SHA_PATTERN.match(sha):
                return sha
    raise ValueError(
        "compare 响应里没有 merge_base_commit ⇒ 无法确定分叉点 ⇒ 须人工确认"
    )


def fetchChangedFiles(repo: str, baseSha: str, headSha: str) -> tuple[str, ...]:
    """取「PR 相对分叉点改了哪些文件」（三点语义）。

    ⚠️ **不用 compare API 的 `files` 数组**：它**最多 300 项** ⇒ 大 PR 会被
    **静默截断** ⇒ 归因集不完整 ⇒ 与净变更取交集时会把 PR 真改的 CI 文件
    **排除**掉 ⇒ fail-open（守卫静默失效）⇒ 必须报错。
    ⇒ 改成：compare 只取 `merge_base_commit`（分叉点），差异用 **Trees API
    树对树**算 ⇒ **无 300 项上限**（Trees API 的上限是 `truncated`，已 fail-closed）。

    ⚠️ 语义与 compare 的三点 diff 等价（三点 = `diff(merge_base, head)`）。
    ⚠️ **改名**在树对乡下表现为「旧路径删除 + 新路径新增」⇒ 两个路径都在清单里
    ⇒ 不需要 compare 的 `previous_filename` 补丁（补丁反而会在树对树下重复计名）。
    """
    mergeBaseSha = fetchMergeBaseSha(repo, baseSha, headSha)
    mergeBaseTree = fetchTreePathShas(repo, mergeBaseSha)
    headTree = fetchTreePathShas(repo, headSha)
    return _diffTreePathShas(mergeBaseTree, headTree)
