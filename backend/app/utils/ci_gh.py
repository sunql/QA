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

from app.utils.ci_text import sanitizeForReport

# gh 单次调用的超时上限（秒）。没有它，gh 在没凭据时会挂住直到 CI 超时。
_GH_TIMEOUT_SEC = 300
# gh stderr 的最大保留长度
_MAX_STDERR_LEN = 2048

# `--repo` / `--run-id` / sha 的合法形态（它们会被拼进 gh 的 API 路径）。
# ⚠️ owner/repo 的**每段首字符**必须是非 `.` 的标识符字符：否则 `--repo ../..`
# 会通过校验，而 `repos/../../actions/runs/<id>/jobs` 被服务端规范化后可打到
# 另一个 API 端点（实测：两斜杠形态能过校验，三斜杠被拒 ⇒ 守卫用例要写两斜杠那一种）。
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_RUN_ID_PATTERN = re.compile(r"^[0-9]+$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
# 分支名（会被拼进 `repos/{repo}/commits/{branch}` 路径）：
# 只允许 GitHub 分支名的常规字符 ⇒ 排除 `..`（路径穿越）与空格/换行。
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,254}$")

# GitHub compare API 的 `files` 数组上限：**最多 300 项**。
# `--paginate` 只对 commits 分页，**不会**补齐 files ⇒ 命中上限必须报错，
# 否则「PR 改了多少文件」这件事会被**静默截断** ⇒ CI 相关面改动可能隐身。
_COMPARE_FILES_LIMIT = 300


@dataclass(frozen=True)
class RunHeadInfo:
    """一轮 run 的 head 信息。"""

    headSha: str
    headBranch: str
    headRepo: str


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

    `--allow-escape-sequences` 是必需的：GH Actions 的日志含终端转义序列，
    不加这个 flag `gh api` 会直接报错拒绝输出。

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
    ⇒ 用 compare API 就能算出这期间改了哪些文件 ⇒ 判断是否动了「CI 相关面」。
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
    """取一轮 run 的 head commit / 分支名 / 分支所在仓库。

    `head_repository.full_name` 用来区分「同仓 PR」与「fork PR」：
    fork 的分支不在本仓库 ⇒ 没法查它的当前 head ⇒ 新鲜度判定要另作处理。
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
    return RunHeadInfo(headSha=headSha, headBranch=headBranch, headRepo=headRepo)


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


def fetchChangedFiles(repo: str, baseSha: str, headSha: str) -> tuple[str, ...]:
    """取两个 commit 之间的改动文件清单。"""
    invalidReason = validateRepo(repo)
    if invalidReason is not None:
        raise ValueError(invalidReason)
    for label, sha in (("--base", baseSha), ("--head", headSha)):
        if not _SHA_PATTERN.match(sha):
            raise ValueError(f"{label} sha 形态异常：{sanitizeForReport(sha)}")
    payload = _runGh(
        ["api", f"repos/{repo}/compare/{baseSha}...{headSha}", "--paginate", "--slurp"]
    )
    return _parseChangedFiles(payload)


def _parseChangedFiles(payload: str) -> tuple[str, ...]:
    """从 compare 响应里取出改动文件名（合并分页）。

    ⚠️ compare API 的 `files` 数组**最多 300 项**（`--paginate` 只对 commits
    分页，**不会**补齐 files）⇒ 命中上限必须**报错**，不能当成「就这些文件」
    ⇒ 否则一个改了 300+ 文件的大 PR 里混着 `.github/workflows/ci.yml`
    也会被漏掉 ⇒ CI 相关面守卫放行 ⇒ 假「净新增 0」。
    """
    parsed = json.loads(payload)
    pages = parsed if isinstance(parsed, list) else [parsed]
    names: list[str] = []
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError(f"gh 返回了意料之外的 JSON 结构：{type(page).__name__}")
        for item in page.get("files") or []:
            if isinstance(item, dict) and "filename" in item:
                names.append(str(item["filename"]))
    if len(names) >= _COMPARE_FILES_LIMIT:
        raise ValueError(
            f"改动文件数达到 compare API 的上限（{_COMPARE_FILES_LIMIT}）⇒ 清单被截断，"
            "无法判定 PR 是否改动了 CI 相关面 ⇒ 须人工确认"
        )
    return tuple(names)
