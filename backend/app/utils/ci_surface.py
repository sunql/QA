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
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass

from app.utils.ci_gh import fetchChangedFiles, fetchRunHeadSha
from app.utils.ci_text import redactSecrets, sanitizeForReport

# 「CI 相关面」：改了这些文件 ⇒ 两次 run 的失败集合**不再可比**。
#
# ⚠️ 工具自身的模块也在列：改 `ci_failure_set.py` 就能直接改判定逻辑
# ⇒ 失败集合与基线不可比 ⇒ 必须人工确认。
_CI_SURFACE_PATTERNS = (
    r"^\.github/workflows/",
    r"^backend/conftest\.py$",
    r"^backend/app/tests/(?:[^/]+/)*conftest\.py$",
    r"^backend/app/tests/(?:_pg_support|milvus_isolation)\.py$",
    r"^backend/(?:pyproject\.toml|pytest\.ini|setup\.cfg|tox\.ini)$",
    r"^backend/app/utils/ci_(?:shard|failure_set|text|gh|surface)\.py$",
)

# 拉取失败时可能被抛出的异常类型（`gh` 调用 + JSON 解析）。
_FETCH_ERRORS = (
    RuntimeError,
    OSError,
    ValueError,
    KeyError,
    AttributeError,
    subprocess.SubprocessError,
)


@dataclass(frozen=True)
class CiSurfaceInfo:
    """「CI 相关面」判定结果。

    `error` 非空 ⇒ **判定不出来** ⇒ 失败集合的可比性**未知**
    ⇒ 未知不等于没有 ⇒ 保守拦下。
    """

    baseSha: str | None
    prSha: str | None
    changedFiles: tuple[str, ...]
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


def resolveCiSurface(repo: str, baseRunId: str, prRunId: str) -> CiSurfaceInfo:
    """取两次 run 的 head sha，并算出这期间改了哪些文件。

    **任何一步失败都不抛异常** —— 失败会以 `error` 字段返回 ⇒ 调用方
    走「判定不出来 ⇒ 保守拦下」的分支，而不是崩在网络错误上。
    """
    try:
        baseSha = fetchRunHeadSha(repo, baseRunId)
        prSha = fetchRunHeadSha(repo, prRunId)
    except _FETCH_ERRORS as error:
        return CiSurfaceInfo(
            baseSha=None,
            prSha=None,
            changedFiles=(),
            error=f"拉取 run 的 head_sha 失败：{sanitizeForReport(redactSecrets(str(error)))}",
        )
    try:
        changedFiles = fetchChangedFiles(repo, baseSha, prSha)
    except _FETCH_ERRORS as error:
        return CiSurfaceInfo(
            baseSha=baseSha,
            prSha=prSha,
            changedFiles=(),
            error=f"拉取两次 run 之间的改动文件清单失败：{sanitizeForReport(redactSecrets(str(error)))}",
        )
    return CiSurfaceInfo(
        baseSha=baseSha,
        prSha=prSha,
        changedFiles=changedFiles,
        error=None,
    )
