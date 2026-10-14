"""图表服务 —— 编排门面（决策 → spec → 渲染）。

**改造前**：`recommendChartType` 只看列形状（4 个出口，永远不返回散点），然后
**让 LLM 直接写 ECharts option**。两个后果：语义判不出来（占比与分类比较形状相同），
且 LLM 写错 option 是「图能不能出来」的唯一失败面。

**改造后**：形状由 `chart_decision` 的规则表判，语义由 `chart_label` 给一个标签
（只在规则歧义时调），spec 由 `chart_spec_builder` 确定性派生，option 由
`chart_renderer` 渲染。**LLM 不再产出任何图表代码。**

依赖方向单向：本模块 → decision / spec_builder → renderer → spec。

**计费口径不变**：仍返回 promptTokens/completionTokens/cachedTokens 三元组，
`_recordChartUsage` 与 `_summarizeUsage` 无需改动。改造后 LLM 调用**大幅减少**
（常见查询 0 次，只有歧义形状才 1 次，且 prompt 只有 ~250 token）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.domain.chart_spec import ChartSpec, coerceSpec, tableSpec
from app.domain.enums import ChartType
from app.domain.query_plan import QueryPlan
from app.services.chart_decision import (
    ChartDecision,
    buildChartSignals,
    decideChartKind,
    resolveByLabel,
)
from app.services.chart_label import classifySemanticLabel
from app.services.chart_renderer import renderChartOption
from app.services.chart_spec_builder import buildSpec
from app.services.chart_thresholds import loadChartThresholds, loadFullDataThreshold
from app.services.visual_payload import assembleTableOption
from app.services.visual_rationale import (
    VisualRationale,
    buildVisualRationale,
)

logger = logging.getLogger(__name__)

# 客户端/意图强制指定图型时用的伪规则号（便于日志区分「选出来的」和「指定的」）。
_FORCED_RULE_ID = "R_FORCED_CLIENT"


@dataclass(frozen=True)
class ChartBuild:
    """图表构建结果。

    三元组（promptTokens/completionTokens/cachedTokens）保持 4-tuple 的计费口径，
    另外带上 decision 与 spec 供日志与测试观察「为什么选了这个图」。Task 3 起
    同时带 `tableOption`（图之外的明细表负载）与 `rationale`（为什么这么画的
    判断依据，供前端 i18n 渲染）。
    """

    chartType: ChartType
    option: dict[str, Any]
    tableOption: dict | None
    rationale: VisualRationale
    promptTokens: int
    completionTokens: int
    cachedTokens: int | None
    decision: ChartDecision
    spec: ChartSpec


class ChartService:
    """决策 → spec → 渲染的编排门面。"""

    async def buildChart(
        self,
        *,
        session: Any,
        plan: QueryPlan | None,
        columns: list[str],
        data: list[dict],
        question: str,
        forcedKind: ChartType | None = None,
        intentKind: ChartType | None = None,
        llmClient: Any = None,
        modelConfig: Any = None,
    ) -> ChartBuild:
        """构建图表负载。**绝不抛错** —— 出图失败不该连带把整轮回答打断。

        优先级：客户端显式 `forcedKind` > 意图抽取 `intentKind` > 决策引擎。
        强制的图型仍要过 spec 形状校验（在 1 维数据上强制热力图会降级 —— 降级终点
        是**表格**，见 `coerceSpec`），因为「用户点了热力图」不等于「这份数据画得出
        热力图」。
        """
        if not columns or not data:
            return self._emptyResult(columns, data)

        thresholds = await loadChartThresholds(session)
        fullDataThreshold = await loadFullDataThreshold(session)
        signals = buildChartSignals(plan, columns, data, question)

        forced = forcedKind or intentKind
        decision = (
            ChartDecision(
                kind=forced,
                ruleId=_FORCED_RULE_ID,
                candidates=(),
                ambiguous=False,
            )
            if forced is not None
            else decideChartKind(signals, thresholds)
        )

        promptTokens = completionTokens = 0
        cachedTokens: int | None = 0
        if decision.ambiguous and llmClient is not None and modelConfig is not None:
            labelResult = await classifySemanticLabel(
                question=question,
                columns=columns,
                data=data,
                llmClient=llmClient,
                modelConfig=modelConfig,
            )
            promptTokens = labelResult.promptTokens
            completionTokens = labelResult.completionTokens
            cachedTokens = labelResult.cachedTokens
            decision = resolveByLabel(decision, signals, labelResult.label, thresholds)

        spec = buildSpec(decision, signals, data, plan)
        spec, degradeReason = coerceSpec(spec, columns)
        if degradeReason is not None:
            logger.warning(
                "图表 spec 校验未过，降级为表格（rule=%s decision=%s）：%s",
                decision.ruleId,
                decision.kind.value,
                degradeReason,
            )
        option = renderChartOption(spec, data)
        # rationale 的 kind 传「决策引擎选出的 kind（降级前的意图）」，不传降级后的
        # spec.kind：否则 DEGRADE_SPEC_INVALID 的文案会渲染成「数据结构不满足 table
        # 的绘图要求」。同理 R_FORCED_CLIENT 传的是被强制的那个 kind。
        rationale = buildVisualRationale(
            ruleId=decision.ruleId,
            kind=decision.kind,
            rowCount=len(data),
            degradeReason=degradeReason,
        )
        tableOption = assembleTableOption(
            specKind=spec.kind,
            columns=columns,
            data=data,
            fullDataThreshold=fullDataThreshold,
        )
        return ChartBuild(
            # 降级后 chartType 必须跟着变成 TABLE：前端按 chartType 分支选渲染器，
            # 说「hbar」却发 {columns, rows} 会让 ECharts 拿到非法 option 画空白。
            chartType=spec.kind,
            option=option,
            tableOption=tableOption,
            rationale=rationale,
            promptTokens=promptTokens,
            completionTokens=completionTokens,
            cachedTokens=cachedTokens,
            decision=decision,
            spec=spec,
        )

    @staticmethod
    def _emptyResult(columns: list[str], data: list[dict]) -> ChartBuild:
        """无数据：零 LLM 消耗，直接给表格负载。

        强制图型在这里也要让位 —— 没有数据就没有图，返回一个渲染不出来的
        kind 只会让前端画空白，不如老老实实交一张（空）表格。
        """
        decision = ChartDecision(
            kind=ChartType.TABLE,
            ruleId="R00_EMPTY_TABLE",
            candidates=(),
            ambiguous=False,
        )
        return ChartBuild(
            chartType=ChartType.TABLE,
            option={"columns": list(columns), "rows": list(data)},
            # 空数据表已在 option 里，不再附第二份表；rationale 零成本（纯函数、
            # 不读 DB），直接给 R00。
            tableOption=None,
            rationale=buildVisualRationale(
                ruleId="R00_EMPTY_TABLE", kind=ChartType.TABLE, rowCount=len(data)
            ),
            promptTokens=0,
            completionTokens=0,
            cachedTokens=0,
            decision=decision,
            spec=tableSpec(columns),
        )
