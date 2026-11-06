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
    r"(?:^|.*/)conftest\.py$",
    r"(?:^|.*/)pytest\.ini$",
    r"(?:^|.*/)setup\.cfg$",
    r"(?:^|.*/)tox\.ini$",
    # 依赖锁定文件：改了依赖版本 ⇒ 测试环境变了 ⇒ 失败集合不再可比
    r"^backend/(?:pyproject\.toml|uv\.lock)$",
    r"^backend/app/tests/(?:_pg_support|milvus_isolation)\.py$",
    r"^backend/app/utils/ci_(?:shard|failure_set|text|gh|surface|head|pytest_log|judge|report|flakes)\.py$",
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
            error=f"拉取 run 的 head_sha 失败：{sanitizeForReport(str(error))}",
        )
    try:
        changedFiles = fetchChangedFiles(repo, baseSha, prSha)
    except _FETCH_ERRORS as error:
        return CiSurfaceInfo(
            baseSha=baseSha,
            prSha=prSha,
            changedFiles=(),
            error=f"拉取两次 run 之间的改动文件清单失败：{sanitizeForReport(str(error))}",
        )
    return CiSurfaceInfo(
        baseSha=baseSha,
        prSha=prSha,
        changedFiles=changedFiles,
        error=None,
    )
