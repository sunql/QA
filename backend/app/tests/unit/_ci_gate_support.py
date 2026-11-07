"""CI 合并闸门单元测试的**共享 support**（替身与固定数据）。

背景：闸门的 CLI 用例分两个套件 —— 基础对差（`test_ci_failure_set.py`）与
已知抖动白名单（B7，`test_ci_failure_set_flakes.py`）—— 共用同一批替身。
抽到本模块后：

- 依赖关系显式：谁提供什么，一眼可见；
- 两个套件各自只保留自己关心的用例，行数都在 800 行上限内
  （`test_ci_failure_set.py` 的白名单段曾把两个文件都顶到上限之上）；
- 与 `app/tests/unit/_agent_orchestration_runner_support.py` 的既有做法一致
  （跨模块只引用**公开**名字，避免私有名被改名后变成收集期 error）。

⚠️ 替身只替换**外部依赖**（GitHub API / run 新鲜度判定 / CI 相关面判定），
失败集合的抽取走的仍是**真实实现**（`collectRunFailures`）⇒ 断言钉住的是
闸门的**决策**，不是替身的行为。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.utils import ci_failure_set
from app.utils.ci_failure_set import CiJob, JobAssessment, collectRunFailures
from app.utils.ci_head import HeadFreshness
from app.utils.ci_surface import CiSurfaceInfo

# GH Actions 的真实日志行形状：行首是 ISO 时间戳 + 空格
TIMESTAMPED = "2026-11-02T07:10:13.4960640Z "

# PR #107 实证的抖动用例（Milvus 可见性竞态）。
FLAKY_NODE_ID = (
    "app/tests/integration/test_reconcile_milvus_id_mapping.py::test_backfill_is_idempotent"
)
# 一条「稳定红」的对照用例（用来造净新增 / 净丢失）。
STABLE_NODE_ID = "app/tests/unit/test_chat_service.py::test_reply"
# 同一条用例在 **repo 相对**路径下的形态（compare API 给的是仓库相对路径）。
FLAKY_FILE_IN_REPO = "backend/app/tests/integration/test_reconcile_milvus_id_mapping.py"


def job(
    jobId: int = 1,
    name: str = "Backend / unit shard 0",
    conclusion: str = "failure",
) -> CiJob:
    """一个 CI job。"""
    return CiJob(jobId=jobId, name=name, conclusion=conclusion)


def stubCiSurface(
    monkeypatch: pytest.MonkeyPatch,
    changedFiles: tuple[str, ...],
    error: str | None = None,
    netChangedFiles: tuple[str, ...] | None = None,
    unattributedCiFiles: tuple[str, ...] = (),
    baseTrustError: str | None = None,
    mainAdvanceFiles: tuple[str, ...] = (),
    mainTipSha: str | None = "a" * 40,
) -> None:
    """把「CI 相关面判定」换成固定结果（避免真调 gh 的 compare API）。

    默认给空清单 ⇒ CI 相关面守卫不触发 ⇒ CLI 用例只需关注失败集合对差本身。

    ⚠️ 三个清单的分工（2026-11-07 N6 第三版）：
    - `changedFiles` = `P` = PR **自己**改的文件（**措辞**用）；
    - `mainAdvanceFiles` = `A` = main 的内容推进（**措辞**用）；
    - `netChangedFiles` = **判定集合** = `A ∪ P`。

    ⚠️ `netChangedFiles` 默认与 `changedFiles` 相同（`None` ⇒ 取 `changedFiles`）：
    老调用方只给 `changedFiles` ⇒ 判定集合 = PR 的改动 ⇒ 仍然会看到
    「CI 相关面被改动 ⇒ 拦下」，语义不变。
    """
    resolvedNet = changedFiles if netChangedFiles is None else netChangedFiles
    monkeypatch.setattr(
        ci_failure_set,
        "resolveCiSurface",
        lambda repo, baseRunId, prRunId: CiSurfaceInfo(
            baseSha="a" * 40,
            prSha="b" * 40,
            mainTipSha=mainTipSha,
            changedFiles=changedFiles,
            mainAdvanceFiles=mainAdvanceFiles,
            netChangedFiles=resolvedNet,
            unattributedCiFiles=unattributedCiFiles,
            baseTrustError=baseTrustError,
            error=error,
        ),
    )


def stubHeadFreshness(
    monkeypatch: pytest.MonkeyPatch,
    isFresh: bool = True,
    reason: str | None = None,
    note: str | None = None,
) -> None:
    """把「PR run 新鲜度判定」换成固定结果（避免真调 gh 查分支 head）。

    默认新鲜 ⇒ CLI 用例只需关注失败集合对差本身。
    """
    monkeypatch.setattr(
        ci_failure_set,
        "resolveHeadFreshness",
        lambda repo, runId, runSha: HeadFreshness(isFresh=isFresh, reason=reason, note=note),
    )


def stubGithub(
    monkeypatch: pytest.MonkeyPatch,
    baseResult: tuple[frozenset[str], tuple[JobAssessment, ...]],
    prResult: tuple[frozenset[str], tuple[JobAssessment, ...]],
) -> None:
    """把 `collectRunFromGithub` 换成固定返回值（避免真调 gh）。"""
    stubCiSurface(monkeypatch, ())
    stubHeadFreshness(monkeypatch)

    def fake(repo: str, runId: str):
        return baseResult if runId == "111" else prResult

    monkeypatch.setattr(ci_failure_set, "collectRunFromGithub", fake)


# 「固定红」日志里那两条失败用例（真实形状：收尾汇总行与它们自洽）。
DEFAULT_RED_NODE_IDS: tuple[str, ...] = (
    "app/tests/unit/test_chat_service.py::test_reply",
    "app/tests/unit/test_rate_limit.py::test_burst",
)


def redLog(family: str = "UNIT", shard: int = 0) -> str:
    """一份「分片跑完但红了」的日志片段（真实形状）。

    **完成标记的族名与分片号必须与 job 名对齐**（job 名 `Backend / unit shard 0`
    ⇒ 标记 `UNIT_SHARD_DONE shard=0`）—— 实现会交叉校验两者 ⇒ fixture 不对齐时，
    用例会因为「标记与 job 不符」而变红，测不到本来要测的那件事。

    **必须带 pytest 的收尾汇总行**（`2 failed, … in …s`）—— 真红（status=1）
    而没有汇总行 ⇒ 无法确认 pytest 跑完了整个 session ⇒ 判定为不可用。
    """
    return redLogWith(DEFAULT_RED_NODE_IDS, family, shard)


def greenLog(family: str = "UNIT", shard: int = 0) -> str:
    """一份「分片跑完且全绿」的日志片段。"""
    return "\n".join(
        [
            f"{TIMESTAMPED}5431 passed, 1 skipped in 812.34s",
            f"{TIMESTAMPED}{family}_SHARD_DONE shard={shard} status=0",
        ]
    )


def oneRedShard() -> tuple[frozenset[str], tuple[JobAssessment, ...]]:
    """一个「红了的分片」的失败集合与逐 job 评估。"""
    jobs = [job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    return collectRunFailures(jobs, {1: redLog()})


def redLogWith(nodeIds: tuple[str, ...], family: str = "UNIT", shard: int = 0) -> str:
    """一份「红了，且失败用例恰好是给定这些」的日志（汇总行数字自洽）。"""
    lines = [f"{TIMESTAMPED}platform linux -- Python 3.13.0, pytest-8.3.4"]
    for nodeId in nodeIds:
        lines.append(f"{TIMESTAMPED}FAILED {nodeId} - assert 1 == 2")
    lines.append(f"{TIMESTAMPED}{len(nodeIds)} failed, 118 passed, 3 skipped in 812.34s")
    lines.append(f"{TIMESTAMPED}{family}_SHARD_DONE shard={shard} status=1")
    return "\n".join(lines)


def writeWhitelistFile(path: Path, nodeIds: tuple[str, ...]) -> None:
    """写一份白名单文件（每条一个 node ID，理由写死为测试用）。"""
    payload = {
        "entries": [
            {"nodeId": nodeId, "reason": "已知抖动：Milvus 可见性竞态", "since": "2026-11-06"}
            for nodeId in nodeIds
        ]
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def stubShards(
    monkeypatch: pytest.MonkeyPatch,
    baseNodeIds: tuple[str, ...],
    prNodeIds: tuple[str, ...],
) -> None:
    """两侧各一个红分片，失败用例恰好是给定的这些。"""
    baseJobs = [job(jobId=1, name="Backend / unit shard 0", conclusion="failure")]
    baseResult = collectRunFailures(baseJobs, {1: redLogWith(baseNodeIds)})
    prJobs = [job(jobId=11, name="Backend / unit shard 0", conclusion="failure")]
    prResult = collectRunFailures(prJobs, {11: redLogWith(prNodeIds)})
    stubGithub(monkeypatch, baseResult, prResult)
