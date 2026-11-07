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
4. **字节有上界**（`MAX_REPORT_BYTES`）且**小于**闸门塞进 check 摘要的窗口
   —— 截断由**本模块**负责（它会明示），workflow 只搬运不二次截断。
   ⚠️ 2026-11-06 代码评审 M-1 实测：闸门按**字节**砍尾（`head -c 8000`），
   而本模块只管**行数** ⇒ 两套单位 ⇒ 白名单长到 12 条时「净新增失败用例：」落在
   7284 字节处 ⇒ 证据被切断；到 20 条时整段落在窗口之外 ⇒ 摘要里只字未提。
   ⇒ 两套单位必须统一到**字节**，且上限必须**小于**摘要窗口。
5. **证据优先于说明**：「净新增 / 净丢失失败用例」清单排在豁免明细**之前**
   —— 截断是从尾部砍的 ⇒ 排在前面的最后被砍 ⇒ 排后面就等于「把证据挤掉」。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from app.utils.ci_flakes import isEntryStale
from app.utils.ci_text import redactSecrets, sanitizeForReport

# 报告的总字节上限（**必须小于**闸门塞进 check 摘要的窗口 ⇒ workflow 不二次截断）。
#
# ⚠️ 取 20000 的理由：GitHub check-run 的 `output.summary` 上限是 65535 字符；
# 而「20 条豁免（每条含 187 字理由）+ 50 条净新增 ID」的实测体积约 16KB
# ⇒ 20000 足够装下**一整份**判据证据 ⇒ 常见的白名单规模下**不发生截断**。
MAX_REPORT_BYTES = 20000
# 截断提示本身预留的字节（提示插在头部 ⇒ 它自己不会被砍掉）。
_MARKER_RESERVE_BYTES = 256
# 报告的总行数上限（超出部分截断并明示）。
_MAX_REPORT_LINES = 200
# 失败用例 ID 清单最多列多少条（超出只报数量 ⇒ 报告不会被刷屏）。
_MAX_LISTED_IDS = 50
# 已知抖动豁免最多列多少条（超出只报数量）。
#
# ⚠️ 为什么单独设一个更小的上限：豁免段每条最多 3 行（条目 + PR 侧 ⚠ + 可能的老化 ⚠）
# ⇒ 白名单越大，越先把后面的「净新增失败用例」清单挤出 200 行窗口
# ⇒ **失败的证据会被豁免的说明挤掉**（方向反了）⇒ 必须给豁免段封顶。
_MAX_LISTED_EXEMPTIONS = 20


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
    countedBaseCount: int | None = None,
    countedPrCount: int | None = None,
    mainTipSha: str | None = None,
) -> list[str]:
    """报告头：结论 + 两侧 run 的标识与失败数。

    「失败 N 条」= **原始**条数；「计入对差 M 条」= 剔除已知抖动豁免后**真正参与对差**的条数。
    两者相等时只给一个数（不制造噪音）；不等时必须给两个数 —— 差额由豁免明细解释。

    ⚠️ `mainTipSha` = PR run 开跑那一刻 main 的 tip（= 那次合并的 main 侧底座）
    ⇒ PR 的 CI 实际跑的是 **merge(main@该 commit, PR 头)** ⇒ 人必须能看见它
    ⇒ 否则「两次 run 的 CI 定义是否相同」这件事无从核对。
    """
    baseShaText = sanitizeForReport(baseSha) if baseSha else "未知"
    prShaText = sanitizeForReport(prSha) if prSha else "未知"
    lines = [
        f"结论：{verdictLabel}（{reasonCode}）",
        f"基线 run {sanitizeForReport(baseRunId)}（head {baseShaText}）："
        f"{_failureCountText(baseFailureCount, countedBaseCount)}",
        f"PR    run {sanitizeForReport(prRunId)}（head {prShaText}）："
        f"{_failureCountText(prFailureCount, countedPrCount)}",
    ]
    if mainTipSha:
        lines.append(
            f"PR run 的 main 侧底座（{sanitizeForReport(mainTipSha)}）："
            "PR 的 CI 实际跑的是 merge(main@该 commit, PR 头)"
        )
    lines.append(f"净新增 {addedCount} 条 / 净丢失 {removedCount} 条")
    return lines


def _failureCountText(rawCount: int, countedCount: int | None) -> str:
    """失败条数的一段话：原始条数 + （若有豁免）计入对差的条数。"""
    if countedCount is None or countedCount == rawCount:
        return f"失败 {rawCount} 条"
    return f"失败 {rawCount} 条（计入对差 {countedCount} 条）"


def _jobLines(runLabel: str, assessments: Iterable, exemptedIds: frozenset[str]) -> list[str]:
    """某一侧 run 的逐 job 可用性明细。

    ⚠️ 2026-11-06 代码评审 L-2：逐 job 的失败数是**原始**条数（含被豁免的），
    而头部是「失败 N 条（计入对差 M 条）」⇒ 两处数字对不上 ⇒ 读者无法核账。
    ⇒ 被豁免的条数必须**显式标出**（否则读者只能选择「不信其中之一」）。
    """
    lines: list[str] = []
    for item in assessments:
        reason = item.unusableReason if item.unusableReason is not None else ""
        status = "可用" if item.isUsable else f"不可用（{sanitizeForReport(reason)}）"
        exemptedCount = sum(1 for failureId in item.failureIds if failureId in exemptedIds)
        exemptedText = f"（含已知抖动豁免 {exemptedCount} 条）" if exemptedCount > 0 else ""
        lines.append(
            f"  - [{runLabel}] {sanitizeForReport(item.job.name)} "
            f"[{sanitizeForReport(item.job.conclusion)}]：{status}，"
            f"失败 {len(item.failureIds)} 条{exemptedText}"
        )
        if item.hasFailureWithoutDetail:
            lines.append("      ⚠️ 该 job 真红但没有 FAILED 行 ⇒ 失败集合为空 ⇒ 不可信")
        if item.markerCount > 1:
            lines.append(
                f"      ⚠️ 日志里出现 {item.markerCount} 条完成标记 ⇒ 疑似有伪造标记 ⇒ 不可信"
            )
    return lines


def _exemptionSideText(inBase: bool, inPr: bool) -> str:
    """豁免出现在哪一侧。

    ⚠️ 「两侧都没命中」在当前实现里不可达（`ci_flakes.applyFlakeExemptions`
    只记录**真的命中**的条目）⇒ 但显式处理，否则将来构造出这种豁免时会被误标成「PR 侧」。
    """
    if inBase and inPr:
        return "两侧"
    if inBase:
        return "基线侧"
    if inPr:
        return "PR 侧"
    return "两侧均未命中"


def _exemptionLines(exemptions: Iterable) -> list[str]:
    """「已知抖动豁免」段。

    ⚠️ 硬约束：每一次豁免都必须在报告里出现 —— 豁免是**判据的一部分**，
    静默放行等于让人以为「两侧完全一致」，而实际上有一条用例的结论被忽略了。

    ⚠️ 「⚠️ PR 侧命中」只对 **PR 侧新增红**（基线不红、PR 红了）触发
    （2026-11-06 代码评审 L-1）：两侧都红是**最典型**的抖动形态
    ⇒ 却每次都报「请人工确认不是确定性回归」⇒ 告警变噪音 ⇒ 真告警被淹没。
    ⇒ 真正危险的只有「基线不红、PR 红了」—— 那才可能是被掩盖的确定性回归。

    每条还要给出**签发日期**（`since`）⇒ 复核者看得到这条豁免「授权了多久」；
    超过 `ci_flakes.MAX_ENTRY_AGE_DAYS` ⇒ 额外 ⚠ 提示复核（老化机制）。
    """
    items = tuple(exemptions)
    if not items:
        return []
    lines = [
        f"已知抖动豁免 {len(items)} 条（该用例的结论不作为证据 ⇒ 不计入对差）："
    ]
    listed = items[:_MAX_LISTED_EXEMPTIONS]
    for item in listed:
        sinceText = f"，since {sanitizeForReport(item.since)}" if item.since else ""
        lines.append(
            f"  - {_reportFailureId(item.nodeId)}［{_exemptionSideText(item.inBase, item.inPr)}］"
            f"{sinceText}：{sanitizeForReport(item.reason)}"
        )
        if item.inPr and not item.inBase:
            lines.append(
                "      ⚠️ PR 侧命中已知抖动用例 ⇒ 本次 CI 红在该用例上"
                "（该结论已被声明为噪声，不参与判据；请人工确认不是确定性回归）"
            )
        if item.since and isEntryStale(item.since):
            lines.append(
                "      ⚠️ 该豁免的签发日期已超过老化上限 ⇒ 请复核它是否仍然是抖动，"
                "或直接删除条目（否则这个用例将永远不再回到判据）"
            )
    if len(items) > len(listed):
        lines.append(f"  …… 另有 {len(items) - len(listed)} 条豁免未列出（清单过长）")
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
    exemptions: Iterable = (),
    rawBaseCount: int | None = None,
    rawPrCount: int | None = None,
    mainTipSha: str | None = None,
) -> list[str]:
    """报告的正文行（顺序即阅读顺序：结论 → 原因 → 提示 → **差异清单** → 豁免 → job 明细）。

    ⚠️ **「净新增 / 净丢失失败用例」清单必须排在豁免明细与 job 明细之前**：
    截断是**从尾部砍**的 ⇒ 排在后面的先被砍掉
    ⇒ 排最后就等于「把失败的证据挤掉」⇒ 结论看似完整、实际看不到新增了什么。
    （2026-11-06 代码评审 M-1：实测白名单 12 条时清单已被切断，20 条时整段消失。）

    `rawBaseCount` / `rawPrCount` = **豁免之前**的失败条数（由调用方直接给出），
    渲染层**不**靠加法把原始条数还原出来（还原是隐含契约，传错集合就会虚高）。
    """
    verdictLabel = "可合并" if verdict.canMerge else "不可合并"
    exemptionItems = tuple(exemptions)
    exemptedIds = frozenset(item.nodeId for item in exemptionItems)
    baseRawCount = rawBaseCount if rawBaseCount is not None else len(baseFailures)
    prRawCount = rawPrCount if rawPrCount is not None else len(prFailures)
    lines = _headerLines(
        baseRunId,
        prRunId,
        baseRawCount,
        prRawCount,
        len(diff.added),
        len(diff.removed),
        baseSha,
        prSha,
        verdictLabel,
        verdict.reasonCode,
        len(baseFailures),
        len(prFailures),
        mainTipSha,
    )
    if verdict.blockingReason is not None:
        lines.append(f"原因：{sanitizeForReport(verdict.blockingReason)}")
    for note in notes:
        lines.append(f"提示：{sanitizeForReport(note)}")
    # ⚠️ 顺序即优先级：差异清单（**证据**）在豁免明细（**说明**）之前。
    if diff.removed:
        lines.append("⚠️ 净丢失失败用例 ⇒ 须人工确认不是「把失败藏起来了」：")
        lines.extend(_truncateIds(tuple(diff.removed)))
    if diff.added:
        lines.append("净新增失败用例：")
        lines.extend(_truncateIds(tuple(diff.added)))
    lines.extend(_exemptionLines(exemptionItems))
    for runLabel, assessments in (("基线", baseAssessments), ("PR", prAssessments)):
        lines.extend(_jobLines(runLabel, assessments, exemptedIds))
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
    exemptions: Iterable = (),
    rawBaseCount: int | None = None,
    rawPrCount: int | None = None,
    mainTipSha: str | None = None,
) -> str:
    """人读的结论文本。

    **一切外部字符串都过 `sanitizeForReport`** —— 否则 PR 作者可以在测试里 print
    一条带 ANSI 的「失败用例 ID」，用 CSI 序列擦掉已打印的结论行 ⇒ 伪造报告。
    ⚠️ 净化必须覆盖**每一个**插入点：漏掉一处（实测漏在 `status` 字段的
    `unusableReason` 上）就等于留了一条 OSC 52 注入通道。
    ⇒ 收尾再整体过一遍 `redactSecrets`（凭据可能出现在失败用例 ID 里，
    例如有人把 token 写进了测试名）。

    `notes` 用来放「不拦但必须让人看见」的提示（例如 fork PR 无法自动核对 run 新鲜度）。

    `exemptions` 是本次实际发生的「已知抖动豁免」明细（`ci_flakes.FlakeExemption`）。
    ⚠️ `baseFailures` / `prFailures` 传入的是**豁免之后**的集合 ⇒ 头部同时给
    「原始失败条数」与「计入对差的条数」，差额由豁免明细解释。
    `rawBaseCount` / `rawPrCount` 是**豁免之前**的条数（调用方直接给出 ⇒ 渲染层不做还原）。
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
        exemptions,
        rawBaseCount,
        rawPrCount,
        mainTipSha,
    )
    lines = _capReport(lines)
    return redactSecrets("\n".join(lines))


def _byteSize(lines: list[str]) -> int:
    """一段报告行的 UTF-8 字节数。"""
    return len("\n".join(lines).encode("utf-8"))


def _capReport(lines: list[str]) -> list[str]:
    """按**行数**与**字节数**封顶报告；截断提示插到**头部**（第 2 行）。

    ⚠️ 为什么提示必须在头部：截断是**从尾部砍**的 ⇒ 提示若在尾部，
    它自己也会被砍掉 ⇒ 等于没提示 ⇒ **静默截断**（2026-11-06 代码评审 M-1 实测：
    闸门按字节砍尾 ⇒ 净新增失败清单被整段挤出摘要 ⇒ 摘要里只字未提）。

    ⚠️ 为什么要有**字节**上限：闸门把报告塞进 check 摘要时按**字节**截断
    ⇒ 本模块若只管行数，两套单位对不上 ⇒ 截断发生在**看不见**的地方。
    ⇒ 上限必须**小于**摘要窗口（`merge-gate.yml` 的 `head -c`）⇒ 截断只由本模块做。

    砍的顺序：从尾部砍 ⇒ **job 明细**先被砍、然后是豁免说明、最后才是差异清单
    ⇒ **证据最后才受损**（与「证据优先」的展示顺序一致）。
    """
    reasons: list[str] = []
    if len(lines) > _MAX_REPORT_LINES:
        reasons.append(f"共 {len(lines)} 行，只显示前 {_MAX_REPORT_LINES} 行")
        lines = lines[:_MAX_REPORT_LINES]
    budget = MAX_REPORT_BYTES - _MARKER_RESERVE_BYTES
    while lines and _byteSize(lines) > budget:
        lines.pop()
        reasons.append(f"超出 {MAX_REPORT_BYTES} 字节上限")
    if reasons:
        marker = f"（报告已截断：{'；'.join(dict.fromkeys(reasons))}；完整内容见 run 日志）"
        lines.insert(1, marker)
    return lines
