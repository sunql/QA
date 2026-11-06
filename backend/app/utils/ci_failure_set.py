"""CI 失败集合对差：把「PR 失败集合 ⊆ 基线失败集合」这条合并判据做成**可执行**工具。

背景
----
本仓的 CI 基线是**红的**（`main` 上即有既有失败）⇒ 合并判据不是「等 CI 变绿」，
而是**失败集合对差**：取同一个 CI 在 PR 与基线两次 run 的失败用例集合，
**净新增为空**才合并。

人工做这件事已经踩过多个坑（全部实测于 2026-11-02 的 PR #98 / #99 批次），
本模块把它们逐个封死：

1. **`cancelled` 的 job 失败集合是空的。** GH Actions 里 job 超时被标记为
   `cancelled`（**不是** `failure`），它的失败集合是**空的** ⇒ 直接拿它对差会得出
   「净新增 0」的**假结论**。⇒ `cancelled` / `skipped` / **缺完成标记**的 job
   一律判为**不可用**，必须复跑该 job 后再用。
2. **完成标记缺失 = 没跑完。** 每个分片 job 跑完必须打印
   `X_SHARD_DONE shard=N status=<pytest 退出码>`（unit 已有、integration 已补）。
   缺标记 ⇒ 该 job 在跑完前就死了 ⇒ 失败集合不可用。
3. **pytest 只有退出码 0 / 1 才代表「跑完了」。** 2=中断 / 3=内部错误 / 4=用法错误 /
   5=未收集到用例 / 128+N=被信号杀死 ⇒ 这些退出码下失败集合都是**空的或不可信的**
   ⇒ 一律判为不可用。（`status=137` 曾是实测出来的假绿通道：CI 脚本用
   `|| status=$?` 捕获退出码，pytest 被 OOM killer 杀掉时 bash 报 137，
   **标记照样打印** ⇒ 旧实现判「跑完了、真红、失败集合为空」⇒ 结论「可合并」。）
4. **真红但零失败明细 ⇒ 不可信。** `status=1` 却一条 `FAILED`/`ERROR` 都没有 ⇒
   失败集合是空的 ⇒ 同样会让对差得出假的「净新增 0」。
5. **日志里没有 pytest 的收尾汇总行 ⇒ 不可信（对 `status=0` 与 `status=1` 一视同仁）。**
   pytest 只有在**整个 session 跑完**时才会打出收尾汇总行（`2 failed, 118 passed in 812.34s`）。
   一个用例里 `os._exit(1)` 就能让 pytest **中途夭折**却留下退出码 1 ⇒
   CI 脚本照样打出 `status=1` ⇒ 但失败集合只是**前半截** ⇒ 净新增 0 是假的；
   `os._exit(0)` 同理留下「真绿」⇒ 失败集合是**空的** ⇒ 净新增 0 也是假的。
   ⇒ 汇总行是「跑完了」的**唯一旁证**，**必须**两侧都要求。
6. **汇总行里的失败数与日志里解析出的失败用例 ID 数必须一致。**
   汇总行的 `N failed` 是 pytest 自己算的，我们是另算一遍 ⇒ 两个数对不上
   ⇒ 说明日志被截断或失败行格式不认识 ⇒ 失败集合不完整 ⇒ 不可信。
7. **日志行带时间戳前缀。** GH Actions 的日志每行形如
   `2026-11-02T07:10:13.4960640Z FAILED app/tests/x.py::test_y`
   ⇒ 正则**不能锚定** `^FAILED`（那样一条都匹配不到）。
8. **`comm` 要求两侧同序同 collation。** python `sorted()`（码位序）与
   `sort -u`（locale 序）不同序时，`comm` 会输出**两边都非空**的假差异。
   ⇒ 本模块在 Python 里直接做集合差，**不依赖 shell `comm`**。
9. **两次 run 必须跑同一批 job。** PR 少跑一个分片 ⇒ 失败集合只会**变小** ⇒
   「净新增 0」是假的（而且 GitHub 上 CI 还会是**全绿** ⇒ 连二次防线都没有）。
   ⇒ 两侧 job 名集合不等价 ⇒ 直接判「对差不成立」。
10. **两次 run 必须是不同的 run，且各自真的有 job。**
   `--base-run` 与 `--pr-run` 填成同一个 id ⇒ 自比自 ⇒ 失败集合必然相同
   ⇒ 「净新增 0」是假的；某侧 run 一个 job 都没有（id 指错 / workflow 没跑起来）
   ⇒ 失败集合是空集 ⇒ 同样假的。
11. **job 名必须符合分片协议。** job 名含 `unit` / `integration` / `shard`
    却不匹配已知协议（`Backend / unit shard N`）⇒ 说明协议被改过 ⇒ 不可信。
    日志里出现分片完成标记、但 job 名不符合协议 ⇒ 同理不可信（不能降级成
    「非分片 job」）。
12. **报告里的一切外部字符串都要净化。** 日志与 workflow 的 `name:` 都由 PR 作者
    可控 ⇒ 可能含 ANSI/OSC 转义序列（改写剪贴板、擦除已打印的结论行 ⇒ 伪造报告）。
    ⇒ 打印前统一剔除控制字符并截断。
13. **`gh` 的 stderr 可能带凭据。** job-logs 端点 302 到 Azure Blob 的 SAS 签名 URL；
    `GH_DEBUG=api` 时还会 dump 请求头 ⇒ 落进报告前统一打码。
14. **PR 改了「CI 相关面」⇒ 失败集合不再可比。** 见 `app.utils.ci_surface`：
    PR 可以通过 conftest / workflow / 分片清单让失败用例**根本不产生 FAILED 行**
    ⇒ 假「净新增 0」⇒ 而 CI 还全绿 ⇒ 没有二次防线。
15. **PR run 必须跑的就是 PR 当前那版代码。** PR 又推了新 commit 而操作者
    拿的是上一次 push 的 run ⇒ 对差的是**旧代码** ⇒ 「净新增 0」是假的
    （而 GitHub 上最新那次 CI 可能已经红了，没人看）。见 `app.utils.ci_head`。

⚠️ 威胁模型的边界（务必读，别把它当成「防住了所有假绿」）
------------------------------------------------------------------
本工具解析的是 **pytest 的 stdout**，而 stdout 由 PR 作者**完全可控**
（测试里可以 `print` 任何东西）。⇒ 这里的判据只能拦住**「意外」的假绿**
（分片被取消、pytest 中途夭折、日志被截断、被信号杀死），
**拦不住**「蓄意」伪造 —— 作者完全可以打印一条假的收尾汇总行 / 假的完成标记，
让「失败集合是空的」看起来像「失败集合是完整的」。

⇒ 真正的合并授权**必须**落在仓库侧的三件事上：
**受保护分支 + 必需状态检查 + `.github/workflows/` 的 CODEOWNERS 审查**
（见 `Harness/rules/开发流程规范.md`）。本工具是**辅助判据**，不是唯一防线。

模块分工（判定 / 呈现 / 适配各自分开，便于逐层加守卫与做变异取证）：

=============================== ================================================
模块                             职责
=============================== ================================================
`app.utils.ci_failure_set`      本模块：CLI、参数、退出码、报告打印
`app.utils.ci_judge`            判定：job 可用性、失败集合、对差、结论
`app.utils.ci_report`           呈现：人读报告（净化、行数上界、指纹）
`app.utils.ci_pytest_log`       日志解析原语：失败 ID、完成标记、收尾汇总行
`app.utils.ci_head`             PR run 新鲜度（服务端事实，不受日志控制）
`app.utils.ci_gh` / `ci_text`   gh 适配 / 净化与脱敏
`app.utils.ci_surface`          「CI 相关面」判定
=============================== ================================================

用法（本地，人工执行）::

    uv run python -m app.utils.ci_failure_set \
        --repo sunql/QA --base-run 41396973529 --pr-run 41402551747

退出码：

===== ==========================
码     含义
===== ==========================
0     净新增 0 且所有 job 可用且覆盖等价 ⇒ 可合并
1     净新增失败非空 ⇒ 不可合并
2     存在不可用 job ⇒ 必须复跑后再对差
3     运行错误（gh 调用失败、参数非法、两侧 run 相同、某侧无 job、
      **PR run 已过期**等）
4     两次 run 的 job 集合不等价 ⇒ 对差不成立 ⇒ 人工确认
5     净丢失失败用例非空 ⇒ 须人工确认不是「把失败藏起来了」
6     PR 改了 CI 相关面 ⇒ 失败集合不可比 ⇒ 须人工确认
===== ==========================
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from app.utils.ci_flakes import (
    FlakeEntry,
    FlakeExemption,
    applyFlakeExemptions,
    defaultWhitelistPath,
    loadFlakeWhitelist,
)
from app.utils.ci_gh import validateRepo, validateRunId
from app.utils.ci_head import HeadFreshness, resolveHeadFreshness
from app.utils.ci_judge import (
    _REASON_COVERAGE,
    _REASON_NEEDS_REVIEW,
    CiJob,
    FailureDiff,
    JobAssessment,
    Verdict,
    assessJob,
    collectRunFailures,
    collectRunFromGithub,
    diffFailureSets,
    verdictFor,
)
from app.utils.ci_pytest_log import (
    extractFailureIds,
    parseShardDoneMarkers,
)
from app.utils.ci_report import renderReport
from app.utils.ci_surface import CiSurfaceInfo, detectCiSurfaceChanges, resolveCiSurface
from app.utils.ci_text import sanitizeForReport

# 结论码（判定侧的四个在 `ci_judge`；这两个只有 CLI 用得到）
_REASON_CI_SURFACE = "CI_SURFACE_CHANGED"
_REASON_STALE_RUN = "STALE_RUN"

# 拉取 CI 数据时可能被抛出的异常类型（gh 调用 + JSON 解析）。
_FETCH_ERRORS = (
    RuntimeError,
    OSError,
    ValueError,
    KeyError,
    AttributeError,
    subprocess.SubprocessError,
)

# ⚠️ 再导出（向后兼容）：既有调用方与测试从本模块导入判定 API。
# 这些名字**必须**是模块级全局 ⇒ 测试可以 `monkeypatch.setattr(ci_failure_set, ...)`
# 替换掉它们（`main()` 走全局查找 ⇒ 替换生效）。
__all__ = [
    "CiJob",
    "FailureDiff",
    "JobAssessment",
    "Verdict",
    "assessJob",
    "collectRunFailures",
    "collectRunFromGithub",
    "diffFailureSets",
    "extractFailureIds",
    "main",
    "parseShardDoneMarkers",
    "renderReport",
    "sanitizeForReport",
    "verdictFor",
]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _invalidArgumentReason(repo: str, baseRunId: str, prRunId: str) -> str | None:
    """参数非法时给出原因；合法则返回 None。"""
    repoReason = validateRepo(repo)
    if repoReason is not None:
        return repoReason
    for label, runId in (("--base-run", baseRunId), ("--pr-run", prRunId)):
        runIdReason = validateRunId(runId)
        if runIdReason is not None:
            return f"{label} 非法（应为纯数字）：{sanitizeForReport(runId)}"
    if baseRunId == prRunId:
        return (
            "--base-run 与 --pr-run 不能是同一个 run id"
            "（自比自 ⇒ 失败集合必然相同 ⇒ 「净新增 0」是假的）"
        )
    return None


def _applyCiSurfaceGuard(
    verdict: Verdict,
    surface: CiSurfaceInfo,
    allowCiChanges: bool,
) -> Verdict:
    """CI 相关面守卫：PR 改了 CI 相关文件 ⇒ 失败集合不可比 ⇒ 不可判「可合并」。

    只在**本该判「可合并」**时才起作用（其他结论已经拦住了）。
    判定不出来（`surface.error` 非空）时**保守拦下** —— 「不知道」不等于「没有」。
    """
    if not verdict.canMerge or allowCiChanges:
        return verdict
    if surface.error is not None:
        return Verdict(
            canMerge=False,
            blockingReason=(
                "无法判定 PR 是否改动了 CI 相关面（改动文件清单拉取失败）"
                f" ⇒ 失败集合可比性未知 ⇒ 不判可合并：{surface.error}"
            ),
            reasonCode=_REASON_CI_SURFACE,
        )
    ciChanges = detectCiSurfaceChanges(surface.changedFiles)
    if not ciChanges:
        return verdict
    return Verdict(
        canMerge=False,
        blockingReason=(
            "PR 改动了 CI 相关面 ⇒ 两次 run 的失败集合不可比 ⇒ 对差不成立："
            f"{list(ciChanges)}"
            "（PR 可以通过 conftest / workflow / 分片清单让失败用例不产生 FAILED 行）"
            "⇒ 须人工确认；确知无影响时用 --allow-ci-changes 显式豁免"
        ),
        reasonCode=_REASON_CI_SURFACE,
    )


@dataclass(frozen=True)
class RunComparison:
    """两侧 run 的对差素材（收拢参数，避免到处传 8 个位置参数）。"""

    baseRunId: str
    prRunId: str
    baseFailures: frozenset[str]
    prFailures: frozenset[str]
    baseAssessments: tuple[JobAssessment, ...]
    prAssessments: tuple[JobAssessment, ...]


def _parseArgs(argv: list[str] | None) -> argparse.Namespace:
    """解析 CLI 参数。"""
    parser = argparse.ArgumentParser(
        prog="app.utils.ci_failure_set",
        description="对差两次 CI run 的失败用例集合，给出合并判据结论",
    )
    parser.add_argument("--repo", required=True, help="仓库 slug，例如 sunql/QA")
    parser.add_argument("--base-run", required=True, help="基线 run id（main 上的那次）")
    parser.add_argument("--pr-run", required=True, help="PR run id")
    parser.add_argument(
        "--allow-ci-changes",
        action="store_true",
        help="PR 确实改动了 CI 相关文件（workflow / conftest / pytest 配置 / 分片清单）"
        "⇒ 失败集合不可比，此参数表示已人工确认无影响",
    )
    parser.add_argument(
        "--flake-whitelist",
        default=str(defaultWhitelistPath()),
        help="已知抖动白名单文件路径（默认：仓库根 .github/ci-known-flaky-tests.json）"
        "⇒ 读不到 / 非法 ⇒ 失败关闭（退出码 3），绝不退化成空白名单",
    )
    parser.add_argument(
        "--gate-title-file",
        default=None,
        help="把 check 标题写进这个**独立文件**（闸门用它覆盖 check 的标题）"
        "⇒ 标题不依赖「人读报告正文」的格式 ⇒ 报告怎么改都不影响它",
    )
    return parser.parse_args(argv)


def _collectBothRuns(
    repo: str,
    baseRunId: str,
    prRunId: str,
) -> tuple[frozenset[str], tuple[JobAssessment, ...], frozenset[str], tuple[JobAssessment, ...]]:
    """拉两侧 run 的失败集合与逐 job 判定（失败时抛 `_FETCH_ERRORS` 里的异常）。"""
    baseFailures, baseAssessments = collectRunFromGithub(repo, baseRunId)
    prFailures, prAssessments = collectRunFromGithub(repo, prRunId)
    return baseFailures, baseAssessments, prFailures, prAssessments


def _resolveFreshness(repo: str, prRunId: str, prSha: str | None) -> HeadFreshness:
    """PR run 新鲜度判定（取不到 run 的 head sha 时**不拦**，只留提示）。

    取不到 `prSha` 说明「CI 相关面」那一轮拉取就失败了 ⇒ 那一侧的守卫
    （`CI_SURFACE_CHANGED`，退出码 6）本来就会拦下 ⇒ 这里不必重复拦，
    只把「判不出来」这件事明示在报告里。
    """
    if prSha is None:
        return HeadFreshness(
            isFresh=True,
            reason=None,
            note=(
                "取不到 PR run 的 head sha ⇒ 无法自动核对 run 是否最新"
                "（请人工确认用的是最新一次 push 的 run）"
            ),
        )
    return resolveHeadFreshness(repo, prRunId, prSha)


def _applyFreshnessGuard(verdict: Verdict, freshness: HeadFreshness) -> Verdict:
    """PR run 新鲜度守卫：run 已过期 ⇒ 对差的是旧代码 ⇒ 不可判「可合并」。

    **只看 PR 侧** —— 基线侧是 main 上的一次快照，main 往前走不影响它的可用性。
    """
    if freshness.isFresh:
        return verdict
    return Verdict(
        canMerge=False,
        blockingReason=freshness.reason,
        reasonCode=_REASON_STALE_RUN,
    )


def _reportNotes(surface: CiSurfaceInfo, freshness: HeadFreshness, allowCiChanges: bool) -> list[str]:
    """报告里要显式留痕的提示（「不拦但必须让人看见」的那几件事）。"""
    notes: list[str] = []
    if freshness.note is not None:
        notes.append(freshness.note)
    if allowCiChanges:
        notes.append(
            "已用 --allow-ci-changes 豁免「CI 相关面被改动」的守卫"
            "（PR 改了 workflow / conftest / pytest 配置 ⇒ 失败集合原则上不可比）"
        )
    if surface.error is not None:
        notes.append(f"CI 相关面判定失败：{surface.error}")
    return notes


def _exitCodeFor(verdict: Verdict, comparison: RunComparison) -> int:
    """结论 ⇒ 退出码。"""
    if verdict.canMerge:
        return 0
    if verdict.reasonCode == _REASON_COVERAGE:
        return 4
    if verdict.reasonCode == _REASON_NEEDS_REVIEW:
        return 5
    if verdict.reasonCode == _REASON_CI_SURFACE:
        return 6
    if verdict.reasonCode == _REASON_STALE_RUN:
        return 3
    # 「不可用」优先于「净新增失败」：基线侧或 PR 侧只要有一个 job 不可用，
    # 结论就必须是「必须复跑」⇒ 退出码 2。
    unusable = [
        item for item in (*comparison.baseAssessments, *comparison.prAssessments) if not item.isUsable
    ]
    return 2 if unusable else 1


def _printReport(
    comparison: RunComparison,
    diff: FailureDiff,
    verdict: Verdict,
    surface: CiSurfaceInfo,
    notes: list[str],
    exemptions: tuple[FlakeExemption, ...] = (),
    rawBaseCount: int | None = None,
    rawPrCount: int | None = None,
    gateTitle: str | None = None,
) -> bool:
    """渲染并打印报告；渲染失败 ⇒ 返回 False（**绝不能**落到「可合并」）。"""
    try:
        report = renderReport(
            comparison.baseRunId,
            comparison.prRunId,
            comparison.baseFailures,
            comparison.prFailures,
            diff,
            comparison.baseAssessments,
            comparison.prAssessments,
            verdict,
            surface.baseSha,
            surface.prSha,
            notes,
            exemptions,
            rawBaseCount,
            rawPrCount,
        )
    except Exception as error:  # noqa: BLE001 —— 报告渲染失败不能当成「可合并」
        # 渲染异常 ⇒ 结论不可见 ⇒ 绝不能落到 `return 0`（假绿）。
        print(f"错误：渲染报告失败：{sanitizeForReport(str(error))}", file=sys.stderr)
        return False
    print(report)
    if gateTitle is not None:
        # ⚠️ **仅供人读（display-only）**：check 标题的**机器通道**是
        # `--gate-title-file` 指明的那个**独立文件**（SR-L-2：闸门不得从人读的
        # 报告正文里 grep 标题 ⇒ 报告一改就静默失效）。
        # 这一行只让**直接看 stdout** 的人也能看到豁免条数；它**不是**机器通道。
        print(f"gate-title: {gateTitle}")
    return True


def _gateTitle(
    code: int,
    exemptions: tuple[FlakeExemption, ...],
) -> str | None:
    """给 GitHub check 的**标题**（人第一眼看到的东西）⇒ 豁免必须出现在标题里。

    ⚠️ 为什么标题里必须有它：对称豁免的代价是「PR 侧确定性弄红白名单用例也不拦」
    ⇒ 唯一的提示原本只躺在 check summary 正文里（要展开才看得到），
    而 GitHub UI 上人先看到的是**标题 + 绿色对勾** ⇒ 「干净、可合并」会盖住真相。

    ⇒ 标题里带上豁免条数；**PR 侧新增红**时额外点明（那一类必须人工确认）。
    非 0 退出码不给标题（沿用 workflow 里按退出码生成的「退出码 N ⇒ 不可合并」）。

    ⚠️ 2026-11-06 代码评审 L-1：只数「**PR 侧新增红**」（基线不红、PR 红了）。
    两侧都红是**最典型**的抖动形态 ⇒ 却每次都报「请人工确认不是确定性回归」
    ⇒ 告警变噪音 ⇒ 真告警被淹没。
    """
    if code != 0:
        return None
    prSideCount = sum(1 for item in exemptions if item.inPr and not item.inBase)
    if not exemptions:
        return None
    if prSideCount > 0:
        return (
            f"净新增 0 ⇒ 可合并（含 {len(exemptions)} 条已知抖动豁免，"
            f"其中 PR 侧命中 {prSideCount} 条 ⇒ 请人工确认不是确定性回归）"
        )
    return f"净新增 0 ⇒ 可合并（含 {len(exemptions)} 条已知抖动豁免）"


def _loadComparison(repo: str, baseRunId: str, prRunId: str) -> RunComparison | None:
    """拉两侧 run 的对差素材；拉取失败或某侧无 job ⇒ 打印错误并返回 None。"""
    try:
        baseFailures, baseAssessments, prFailures, prAssessments = _collectBothRuns(
            repo, baseRunId, prRunId
        )
    except _FETCH_ERRORS as error:
        # 注意：不要用 `::error::` 前缀 —— 那是 GH Actions 的 workflow command，
        # 本工具是本地 CLI；一旦输出被贴进 Actions 日志就会变成一条注解。
        print(f"错误：拉取 CI 日志失败：{sanitizeForReport(str(error))}", file=sys.stderr)
        return None

    if not baseAssessments or not prAssessments:
        print(
            "错误：有一侧的 run 一个 job 都没有（run id 指错 / workflow 没跑起来 / "
            "job 列表尚未归档）⇒ 失败集合是空集 ⇒ 对差不成立",
            file=sys.stderr,
        )
        return None

    return RunComparison(
        baseRunId=baseRunId,
        prRunId=prRunId,
        baseFailures=baseFailures,
        prFailures=prFailures,
        baseAssessments=baseAssessments,
        prAssessments=prAssessments,
    )


def _loadFlakeEntries(whitelistPath: str) -> tuple[FlakeEntry, ...] | None:
    """读已知抖动白名单；读不到 / 非法 ⇒ 打印错误并返回 None（**失败关闭**）。

    白名单是**判据的一部分** ⇒ 判据读不出来 ⇒「不知道」不等于「没有」⇒ 保守拦下。
    绝不能在这里退化成「空白名单」（那等于把「读不出来」当成「没有抖动」）。

    ⚠️ 捕 **`Exception`** 而不是只捕 `FlakeWhitelistError`（2026-11-06 安全审查 L-1）：
    别的异常（`OSError` / `KeyError` / `MemoryError` …）会**逃出**本函数
    ⇒ CLI 裸 traceback + 退出码 **1**（1 的语义是「净新增失败」⇒ 操作者查错方向）。
    ⇒ 任何异常形态都归到「读不出来」⇒ 退出码 3。
    """
    try:
        return loadFlakeWhitelist(Path(whitelistPath))
    except Exception as error:  # noqa: BLE001 —— 判据读不出来 ⇒ 一律失败关闭
        print(f"错误：{sanitizeForReport(str(error))}", file=sys.stderr)
        return None


def _unmatchedWhitelistNote(
    entries: tuple[FlakeEntry, ...],
    exemptions: tuple[FlakeExemption, ...],
    skipped: tuple[FlakeExemption, ...] = (),
) -> str | None:
    """白名单里有条目**这次没命中失败集合** ⇒ 提示可能已失效 / 拼错。

    没有这条提示的话，一个拼错的 node ID 会**静默失效**：白名单看起来在管事，
    实际那条抖动照样随机挡人，而报告里看不出「有条目一直没生效」。

    ⚠️ 2026-11-06 代码评审 M-6：旧实现按 `len(entries) - len(exemptions)`
    算「未命中」⇒ 把 **`skipped`**（命中了失败集合、但因所在文件被改动而
    **未生效豁免**）也算成未命中 ⇒ 报告会说「本次命中 0 条，未命中 1 条
    ⇒ 建议清理」⇒ 操作者据此删掉一条**仍然活着**的抖动条目
    ⇒ 随机挡人的老毛病**静默复活**（B7 存在的目的被推翻）。
    ⇒ 「命中失败集合」= 生效豁免 + 因文件被改动而未生效。
    """
    matchedCount = len(exemptions) + len(skipped)
    unmatchedCount = len(entries) - matchedCount
    if unmatchedCount <= 0:
        return None
    return (
        f"白名单共 {len(entries)} 条，本次命中失败集合 {matchedCount} 条"
        f"（生效豁免 {len(exemptions)} 条 / 因所在文件被改动而未生效 {len(skipped)} 条），"
        f"未命中 {unmatchedCount} 条（可能已失效或拼写错误 ⇒ 建议清理）"
    )


def _skippedExemptionNotes(skipped: tuple[FlakeExemption, ...]) -> list[str]:
    """「本来会豁免、但因条件不满足而没豁免」的条目 ⇒ 报告里必须**明示**。

    条件 = 「该用例所在文件在本次 PR 中被改动」。
    ⇒ 不静默：否则操作者看到的是「白名单里有它却没生效」，却不知道为什么。
    """
    return [
        f"白名单用例 {sanitizeForReport(item.nodeId)} 所在文件本次被改动 "
        "⇒ 该豁免未生效 ⇒ 该用例的失败照常计入判据"
        "（请人工确认它仍然是抖动，而不是被这次改动引入的确定性回归）"
        for item in skipped
    ]


def _writeGateTitle(path: Path, title: str) -> None:
    """把 check 标题写进**独立文件**（闸门据此覆盖 check 的标题）。

    ⚠️ 为什么不从「人读报告正文」里 grep：报告是给人看的文本 ⇒ 改一下渲染
    （加缩进、换前缀、把标题挪到被截断的尾部）就静默失效 ⇒ 豁免提示从标题里消失
    ⇒ 对称豁免的代价重新被「绿色对勾」盖住。

    ⚠️ **不自动建父目录**：路径写错（父目录不存在）必须**响亮地失败**
    ⇒ 调用方拿退出码 3 ⇒ 闸门以失败告终，而不是「标题悄悄没写出来」。
    """
    path.write_text(title, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    退出码：0 可合并 / 1 净新增失败 / 2 存在不可用 job / 3 运行错误（含 run 已过期、
    白名单读不到）/ 4 覆盖不等价 / 5 净丢失失败待人工确认 / 6 CI 相关面被改动（集合不可比）。
    """
    args = _parseArgs(argv)

    invalidReason = _invalidArgumentReason(args.repo, args.base_run, args.pr_run)
    if invalidReason is not None:
        print(f"错误：{invalidReason}", file=sys.stderr)
        return 3

    entries = _loadFlakeEntries(args.flake_whitelist)
    if entries is None:
        return 3

    comparison = _loadComparison(args.repo, args.base_run, args.pr_run)
    if comparison is None:
        return 3

    # ⚠️ 「CI 相关面」必须**先**判定：豁免要条件化在「该用例所在文件本次未被改动」
    # ⇒ 需要改动文件清单（`surface.changedFiles`）。
    # 判定不出来（`surface.error` 非空）⇒ 清单未知 ⇒ 照常豁免
    # ⇒ 由「CI 相关面」守卫以退出码 6 兜住（语义仍是「须人工确认」，不是「净新增失败」）。
    surface = resolveCiSurface(args.repo, args.base_run, args.pr_run)
    changedFiles = None if surface.error is not None else surface.changedFiles

    # 豁免只影响「对差」：先剔除已知抖动用例，再算差集 ⇒ 判据里不再有噪声。
    # 不可用 job 判定 / 覆盖等价 / CI 相关面 / 新鲜度判定都**不看**白名单
    # ⇒ 白名单不能用来掩盖「job 没跑完」「少跑了分片」「改了 conftest」。
    outcome = applyFlakeExemptions(
        comparison.baseFailures,
        comparison.prFailures,
        entries,
        changedFiles=changedFiles,
    )
    counted = replace(comparison, baseFailures=outcome.baseFailures, prFailures=outcome.prFailures)

    freshness = _resolveFreshness(args.repo, args.pr_run, surface.prSha)
    diff = diffFailureSets(outcome.baseFailures, outcome.prFailures)
    verdict = verdictFor(diff, comparison.baseAssessments, comparison.prAssessments)
    verdict = _applyFreshnessGuard(verdict, freshness)
    verdict = _applyCiSurfaceGuard(verdict, surface, args.allow_ci_changes)
    notes = _reportNotes(surface, freshness, args.allow_ci_changes)
    unmatchedNote = _unmatchedWhitelistNote(entries, outcome.exemptions, outcome.skipped)
    if unmatchedNote is not None:
        notes.append(unmatchedNote)
    notes.extend(_skippedExemptionNotes(outcome.skipped))

    code = _exitCodeFor(verdict, comparison)
    title = _gateTitle(code, outcome.exemptions)
    if not _printReport(
        counted,
        diff,
        verdict,
        surface,
        notes,
        outcome.exemptions,
        outcome.rawBaseCount,
        outcome.rawPrCount,
        title,
    ):
        return 3
    if args.gate_title_file is not None and title is not None:
        try:
            _writeGateTitle(Path(args.gate_title_file), title)
        except OSError as error:
            # 标题是「豁免可见性」的**唯一**载体 ⇒ 写不出来就保守拦下
            # （与「报告渲染失败 ⇒ 3」同一纪律：结论不可见即不可合并）。
            print(f"错误：写 gate 标题文件失败：{sanitizeForReport(str(error))}", file=sys.stderr)
            return 3
    return code


if __name__ == "__main__":
    sys.exit(main())