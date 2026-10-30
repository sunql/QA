"""L4 token profile 日志的离线解析与聚合。

供 Harness 的 CLI 薄壳（analyze_token_profile.py）调用；解析/聚合逻辑放这里
以便单测覆盖。核心口径：

- 每轮 scale_i = P_i / Σest_i,c（provider prompt_tokens 是唯一权威总量）；
- 标定后组件和恒等于 ΣP_i（分母一致性）；
- resend tax = Σ_{i≥2} P_{i-1}（为旧上下文重复支付的部分）；
- 缓存有效计费 = Σ[(P_i - C_i) + C_i × multiplier]，C_i 缺失按全额。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.l4_token_profile import parseProfileLine


@dataclass(frozen=True)
class ParsedLog:
    """日志文件解析结果：按 runId 分组的逐轮 profile + 工具结果行 + 统计。"""

    runs: dict[str, list[dict]]
    toolResultRecords: tuple[dict, ...]
    loopEnds: tuple[dict, ...]
    linesSeen: int
    linesMatched: int
    linesSkipped: int


@dataclass(frozen=True)
class ComponentTotal:
    name: str
    estimatedTokens: int
    calibratedTokens: float


@dataclass(frozen=True)
class ToolTotal:
    name: str
    callCount: int
    chars: int
    escapeChars: int
    estimatedTokens: int
    calibratedTokens: float


@dataclass(frozen=True)
class RunAggregate:
    runId: str
    sessionId: str | None
    iterationsUsed: int
    terminatedReason: str | None
    totalPromptTokens: int
    totalCompletionTokens: int
    totalCachedTokens: int
    effectiveBilledInputTokens: int
    unknownUsageIterations: tuple[int, ...]
    components: tuple[ComponentTotal, ...]
    toolTotals: tuple[ToolTotal, ...]
    toolResultRecords: tuple[dict, ...]
    resendTaxTokens: int
    inputPricePer1k: float = 0.0
    outputPricePer1k: float = 0.0
    cacheMultiplier: float = 0.25
    cacheHitRecords: int = 0


def parseProfileLines(lines) -> ParsedLog:
    """解析日志行流；噪声/截断行计入 skipped，不抛异常。"""
    runs: dict[str, list[dict]] = {}
    toolRecords: list[dict] = []
    loopEnds: list[dict] = []
    seen = matched = 0
    for line in lines:
        seen += 1
        parsed = parseProfileLine(line)
        if parsed is None:
            continue
        matched += 1
        event = parsed.get("event")
        if event == "iteration":
            runs.setdefault(str(parsed.get("runId") or "unknown"), []).append(parsed)
        elif event == "tool_result":
            toolRecords.append(parsed)
        elif event == "loop_end":
            loopEnds.append(parsed)
    for profiles in runs.values():
        profiles.sort(key=lambda p: p.get("iteration") or 0)
    return ParsedLog(
        runs=runs,
        toolResultRecords=tuple(toolRecords),
        loopEnds=tuple(loopEnds),
        linesSeen=seen,
        linesMatched=matched,
        linesSkipped=seen - matched,
    )


def _calibrated(est: int, scale: float) -> float:
    return est * scale


def aggregateRun(
    runId: str,
    profiles: list[dict],
    *,
    toolResultRecords: tuple[dict, ...] | list[dict] = (),
    loopEnd: dict | None = None,
    cacheMultiplier: float = 0.25,
    inputPricePer1k: float = 0.0,
    outputPricePer1k: float = 0.0,
) -> RunAggregate:
    """把一个 run 的逐轮 profile 聚合成报告结构。"""
    ordered = sorted(profiles, key=lambda p: p.get("iteration") or 0)

    totalPrompt = 0
    totalCompletion = 0
    totalCached = 0
    effectiveBilled = 0
    cacheHitRecords = 0
    unknownIters: list[int] = []
    resendTax = 0
    compEst: dict[str, int] = {}
    compCal: dict[str, float] = {}
    toolEst: dict[str, int] = {}
    toolCal: dict[str, float] = {}
    toolMeta: dict[str, dict] = {}

    for profile in ordered:
        prompt = profile.get("providerPromptTokens")
        if prompt is None:
            unknownIters.append(profile.get("iteration") or 0)
            continue
        totalPrompt += prompt
        completion = profile.get("providerCompletionTokens") or 0
        totalCompletion += completion
        cached = profile.get("cachedTokens")
        if cached is None:
            effectiveBilled += prompt
        else:
            totalCached += cached
            effectiveBilled += (prompt - cached) + cached * cacheMultiplier
            cacheHitRecords += 1

        estTotal = profile.get("estimatedTotalTokens") or 0
        scale = (prompt / estTotal) if estTotal > 0 else 0.0
        for comp in profile.get("components") or []:
            name = comp["name"]
            est = comp.get("estimatedTokens") or 0
            compEst[name] = compEst.get(name, 0) + est
            compCal[name] = compCal.get(name, 0.0) + _calibrated(est, scale)
        for tool in profile.get("toolResults") or []:
            name = tool["name"]
            est = tool.get("estimatedTokens") or 0
            toolEst[name] = toolEst.get(name, 0) + est
            toolCal[name] = toolCal.get(name, 0.0) + _calibrated(est, scale)
            meta = toolMeta.setdefault(name, {"count": 0, "chars": 0, "escapeChars": 0})
            meta["count"] += tool.get("count") or 0
            meta["chars"] += tool.get("chars") or 0
            meta["escapeChars"] += tool.get("escapeChars") or 0

    # resend tax：第 i 轮（i≥2）为第 1..i-1 轮上下文重复支付的量
    promptsPresent = [p.get("providerPromptTokens") for p in ordered if p.get("providerPromptTokens") is not None]
    resendTax = sum(promptsPresent[:-1]) if len(promptsPresent) > 1 else 0

    components = tuple(
        ComponentTotal(name=name, estimatedTokens=compEst[name], calibratedTokens=compCal[name])
        for name in compEst
    )
    toolTotals = tuple(
        ToolTotal(
            name=name,
            callCount=toolMeta.get(name, {}).get("count", 0),
            chars=toolMeta.get(name, {}).get("chars", 0),
            escapeChars=toolMeta.get(name, {}).get("escapeChars", 0),
            estimatedTokens=toolEst[name],
            calibratedTokens=toolCal[name],
        )
        for name in toolEst
    )
    runRecords = tuple(r for r in toolResultRecords if str(r.get("runId")) == str(runId))

    sessionId = None
    terminatedReason = None
    if loopEnd is not None:
        sessionId = loopEnd.get("sessionId")
        terminatedReason = loopEnd.get("terminatedReason")
    elif ordered:
        sessionId = ordered[0].get("sessionId")

    return RunAggregate(
        runId=runId,
        sessionId=sessionId,
        iterationsUsed=len(ordered),
        terminatedReason=terminatedReason,
        totalPromptTokens=totalPrompt,
        totalCompletionTokens=totalCompletion,
        totalCachedTokens=totalCached,
        effectiveBilledInputTokens=int(round(effectiveBilled)),
        unknownUsageIterations=tuple(unknownIters),
        components=components,
        toolTotals=toolTotals,
        toolResultRecords=runRecords,
        resendTaxTokens=resendTax,
        inputPricePer1k=inputPricePer1k,
        outputPricePer1k=outputPricePer1k,
        cacheMultiplier=cacheMultiplier,
        cacheHitRecords=cacheHitRecords,
    )


def renderMarkdown(run: RunAggregate) -> str:
    """渲染单个 run 的 markdown 报告。"""
    lines: list[str] = []
    lines.append(f"# L4 token 构成报告（runId={run.runId}）")
    lines.append("")
    lines.append(
        f"- sessionId: {run.sessionId} | iterations: {run.iterationsUsed}"
        f" | terminated: {run.terminatedReason}"
    )
    lines.append(f"- provider 实测输入总量: **{run.totalPromptTokens}** tokens"
                 f"（输出 {run.totalCompletionTokens}）")
    if run.unknownUsageIterations:
        lines.append(f"- ⚠️ 缺 usage 轮次: {list(run.unknownUsageIterations)}（未计入总量）")
    lines.append("")

    lines.append("## 逐轮明细")
    lines.append("")
    lines.append("| iteration | prompt_tokens | completion | cached | hit% |")
    lines.append("|---|---|---|---|---|")
    lines.append(f"| **合计** | {run.totalPromptTokens} | {run.totalCompletionTokens}"
                 f" | {run.totalCachedTokens} | |")
    lines.append("")

    lines.append("## 组件构成")
    lines.append("")
    lines.append("| 组件 | 估值 tokens | 标定 tokens | 占实测总量 |")
    lines.append("|---|---|---|---|")
    calibratedSum = 0.0
    for comp in run.components:
        calibratedSum += comp.calibratedTokens
        pct = (comp.calibratedTokens / run.totalPromptTokens * 100) if run.totalPromptTokens else 0
        lines.append(
            f"| {comp.name} | {comp.estimatedTokens} | {comp.calibratedTokens:.0f} | {pct:.1f}% |"
        )
    pctSum = (calibratedSum / run.totalPromptTokens * 100) if run.totalPromptTokens else 0
    lines.append(
        f"| **合计** | {sum(c.estimatedTokens for c in run.components)}"
        f" | {calibratedSum:.0f} | {pctSum:.1f}% |"
    )
    lines.append("")

    lines.append("## 工具结果")
    lines.append("")
    lines.append("| 工具 | 累计次数 | 字符 | 转义净增 | 标定 tokens |")
    lines.append("|---|---|---|---|---|")
    for tool in run.toolTotals:
        lines.append(
            f"| {tool.name} | {tool.callCount} | {tool.chars} | {tool.escapeChars}"
            f" | {tool.calibratedTokens:.0f} |"
        )
    if run.toolResultRecords:
        lines.append("")
        lines.append("| iteration | 工具 | rawChars | sentChars | 转义放大 | rowCount |")
        lines.append("|---|---|---|---|---|---|")
        for rec in run.toolResultRecords:
            lines.append(
                f"| {rec.get('iteration')} | {rec.get('toolName')} | {rec.get('rawChars')}"
                f" | {rec.get('sentChars')} | {rec.get('escapeExpansion')} | {rec.get('rowCount')} |"
            )
    lines.append("")

    lines.append("## 放大与重发")
    lines.append("")
    lines.append(f"- resend tax（Σ_{{i≥2}} P_{{i-1}}）: **{run.resendTaxTokens}** tokens"
                 f"（占实测总量 {_pct(run.resendTaxTokens, run.totalPromptTokens)}）")
    lines.append("")

    lines.append("## 缓存与成本")
    lines.append("")
    hitRate = _pct(run.totalCachedTokens, run.totalPromptTokens)
    lines.append(f"- cached_tokens 合计: {run.totalCachedTokens}（命中率 {hitRate}；"
                 f"有缓存字段的轮次 {run.cacheHitRecords}/{run.iterationsUsed}）")
    lines.append(f"- 有效计费输入（缓存折扣 {run.cacheMultiplier}）: {run.effectiveBilledInputTokens} tokens")
    if run.inputPricePer1k > 0 or run.outputPricePer1k > 0:
        upperUsd = run.totalPromptTokens / 1000 * run.inputPricePer1k + (
            run.totalCompletionTokens / 1000 * run.outputPricePer1k
        )
        effectiveUsd = run.effectiveBilledInputTokens / 1000 * run.inputPricePer1k + (
            run.totalCompletionTokens / 1000 * run.outputPricePer1k
        )
        lines.append(f"- 台账口径成本（上界）: ${upperUsd:.4f}")
        lines.append(f"- 缓存折扣后实付估算: ${effectiveUsd:.4f}")
    lines.append("")
    return "\n".join(lines)


def _pct(part: float, total: float) -> str:
    if not total:
        return "0%"
    return f"{part / total * 100:.1f}%"
