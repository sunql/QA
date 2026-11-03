"""合并判据的**人读报告**渲染。

为什么单独一个模块：本模块只负责「把结构化结论变成文本」，
不碰 GitHub、也不做判定 ⇒ 判定逻辑（`ci_failure_set`）与呈现逻辑解耦，
两边都能单独测。

⚠️ 三条渲染纪律（任何改动都不得放宽）：

1. **每一个**插入报告的外部字符串都必须过 `sanitizeForReport`
   （剔控制字符 → 脱敏 → 截断）—— 漏掉一处就等于留了一条 ANSI/OSC 注入通道。
2. **逐字段**净化，不只在末尾整体净化一次（字段级截断会让脱敏看到半截凭据）。
3. **行数有上界**（`_MAX_REPORT_LINES`）—— 失败用例 ID 的条数由 PR 作者控制，
   无上界就能用几百行 `+` 把结论埋掉（结论在第 1 行，但只看尾巴的人会被骗）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from app.utils.ci_text import redactSecrets, sanitizeForReport

# 报告的总行数上限（超出部分截断并明示）。
_MAX_REPORT_LINES = 200
# 失败用例 ID 清单最多列多少条（超出只报数量 ⇒ 报告不会被刷屏）。
_MAX_LISTED_IDS = 50


def _reportFailureId(failureId: str) -> str:
    """失败用例 ID 的报告形态：净化后的可读 ID + 原始 ID 的指纹。

    指纹用于在被净化/脱敏后仍能唯一定位同一个用例（净化会抹掉控制字符、
    脱敏会把不同凭据压成同一个占位串 ⇒ 两个不同的原始 ID 可能渲染成同一个可读串）。
    """
    cleaned = sanitizeForReport(failureId)
    if cleaned == failureId:
        return cleaned
    digest = hashlib.sha256(failureId.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned} [sha256:{digest}]"


def _truncateIds(failureIds: tuple[str, ...]) -> list[str]:
    """失败 ID 清单 ⇒ 报告行（条数有上界）。"""
    listed = failureIds[:_MAX_LISTED_IDS]
    lines = [f"  {_reportFailureId(failureId)}" for failureId in listed]
    if len(failureIds) > len(listed):
        lines.append(f"  …… 另有 {len(failureIds) - len(listed)} 条未列出（清单过长）")
    return lines


def _headerLines(
    baseRunId: str,
    prRunId: str,
    baseFailureCount: int,
    prFailureCount: int,
    addedCount: int,
    removedCount: int,
    baseSha: str | None,
    prSha: str | None,
    verdictLabel: str,
    reasonCode: str,
) -> list[str]:
    """报告头：结论 + 两侧 run 的标识与失败数。"""
    baseShaText = sanitizeForReport(baseSha) if baseSha else "未知"
    prShaText = sanitizeForReport(prSha) if prSha else "未知"
    return [
        f"结论：{verdictLabel}（{reasonCode}）",
        f"基线 run {sanitizeForReport(baseRunId)}（head {baseShaText}）：失败 {baseFailureCount} 条",
        f"PR    run {sanitizeForReport(prRunId)}（head {prShaText}）：失败 {prFailureCount} 条",
        f"净新增 {addedCount} 条 / 净丢失 {removedCount} 条",
    ]


def _jobLines(runLabel: str, assessments: Iterable) -> list[str]:
    """某一侧 run 的逐 job 可用性明细。"""
    lines: list[str] = []
    for item in assessments:
        reason = item.unusableReason if item.unusableReason is not None else ""
        status = "可用" if item.isUsable else f"不可用（{sanitizeForReport(reason)}）"
        lines.append(
            f"  - [{runLabel}] {sanitizeForReport(item.job.name)} "
            f"[{sanitizeForReport(item.job.conclusion)}]：{status}，失败 {len(item.failureIds)} 条"
        )
        if item.hasFailureWithoutDetail:
            lines.append("      ⚠️ 该 job 真红但没有 FAILED 行 ⇒ 失败集合为空 ⇒ 不可信")
        if item.markerCount > 1:
            lines.append(
                f"      ⚠️ 日志里出现 {item.markerCount} 条完成标记 ⇒ 疑似有伪造标记 ⇒ 不可信"
            )
    return lines


def _assembleLines(
    baseRunId: str,
    prRunId: str,
    baseFailures: frozenset[str],
    prFailures: frozenset[str],
    diff,
    baseAssessments: tuple,
    prAssessments: tuple,
    verdict,
    baseSha: str | None,
    prSha: str | None,
    notes: Iterable[str],
) -> list[str]:
    """报告的正文行（顺序即阅读顺序：结论 → 原因 → 提示 → 明细）。"""
    verdictLabel = "可合并" if verdict.canMerge else "不可合并"
    lines = _headerLines(
        baseRunId,
        prRunId,
        len(baseFailures),
        len(prFailures),
        len(diff.added),
        len(diff.removed),
        baseSha,
        prSha,
        verdictLabel,
        verdict.reasonCode,
    )
    if verdict.blockingReason is not None:
        lines.append(f"原因：{sanitizeForReport(verdict.blockingReason)}")
    for note in notes:
        lines.append(f"提示：{sanitizeForReport(note)}")
    if diff.removed:
        lines.append("⚠️ 净丢失失败用例 ⇒ 须人工确认不是「把失败藏起来了」：")
        lines.extend(_truncateIds(tuple(diff.removed)))
    for runLabel, assessments in (("基线", baseAssessments), ("PR", prAssessments)):
        lines.extend(_jobLines(runLabel, assessments))
    if diff.added:
        lines.append("净新增失败用例：")
        lines.extend(_truncateIds(tuple(diff.added)))
    return lines


def renderReport(
    baseRunId: str,
    prRunId: str,
    baseFailures: frozenset[str],
    prFailures: frozenset[str],
    diff,
    baseAssessments: Iterable,
    prAssessments: Iterable,
    verdict,
    baseSha: str | None = None,
    prSha: str | None = None,
    notes: Iterable[str] = (),
) -> str:
    """人读的结论文本。

    **一切外部字符串都过 `sanitizeForReport`** —— 否则 PR 作者可以在测试里 print
    一条带 ANSI 的「失败用例 ID」，用 CSI 序列擦掉已打印的结论行 ⇒ 伪造报告。
    ⚠️ 净化必须覆盖**每一个**插入点：漏掉一处（实测漏在 `status` 字段的
    `unusableReason` 上）就等于留了一条 OSC 52 注入通道。
    ⇒ 收尾再整体过一遍 `redactSecrets`（凭据可能出现在失败用例 ID 里，
    例如有人把 token 写进了测试名）。

    `notes` 用来放「不拦但必须让人看见」的提示（例如 fork PR 无法自动核对 run 新鲜度）。
    """
    lines = _assembleLines(
        baseRunId,
        prRunId,
        baseFailures,
        prFailures,
        diff,
        tuple(baseAssessments),
        tuple(prAssessments),
        verdict,
        baseSha,
        prSha,
        notes,
    )
    if len(lines) > _MAX_REPORT_LINES:
        lines = lines[:_MAX_REPORT_LINES] + [
            f"（报告已截断：共 {len(lines)} 行，只显示前 {_MAX_REPORT_LINES} 行）"
        ]
    return redactSecrets("\n".join(lines))
