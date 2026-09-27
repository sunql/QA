"""查询计划生成与校验（从 nl2sql_service 拆出）。

ReAct 两阶段流水线的阶段一：generateQueryPlan（LLM 出 JSON 计划 + 解析重试）、
generateValidatedPlan（generateQueryPlan → validatePlan 校验循环）、validatePlan
（纯代码校验引用在本体 schema 中）、validateConnectivity（JOIN 连通性）、
_finalizePlan（统一出口：补中间表 JOIN + 连通性 + 范围感知行数限制）。

未来扩展：新增一条计划校验规则加进 validatePlan；新增一种解析失败原因加 REASON_*。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.config import getSettings
from app.domain.enums import DataSourceType
from app.domain.exceptions import Nl2SqlError
from app.domain.models import OntologyClass, OntologyJoin
from app.domain.plan_drop import PlanDrop, formatPlanDrops
from app.domain.query_plan import PlanResult, QueryPlan
from app.infrastructure.llm.base_client import LlmMessage
from app.services.formula_parser import parseFormula
from app.services.llm_retry_policy import completeWithTransientRetry
from app.services.messages_zh import (
    MSG_NL2SQL_PLAN_INVALID,
    MSG_NL2SQL_PLAN_VALIDATION_FAILED,
)
from app.services.nl2sql_dialects import resolveDialect
from app.services.nl2sql_prompts import _buildPlanSystemPrompt, _buildPlanUserPrompt
from app.services.nl2sql_refs import (
    _aggregationAliases,
    _aliasRequiresFormula,
    _classRefNames,
    _extractFormulaProperties,
    _propertyOwnerHint,
    _timeBucketGroupHint,
)
from app.services.nl2sql_schema import (
    buildSchemaText,
    supplementJoinPath,
    _buildJoinGraph,
)
from app.services.nl2sql_scope import _applyScopeRowLimit

logger = logging.getLogger(__name__)

# 匹配 ```json ... ``` 代码块
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

# 查询计划 JSON 最大长度（字符），防止异常大响应耗尽内存
_MAX_PLAN_JSON_BYTES = 64 * 1024

# NL2SQL 单次 LLM 调用的 token 上限。回复达到上限时 SQL 可能被截断，
# 检测到后注入错误并重试，避免执行被截断的 SQL（0-2）
_NL2SQL_MAX_TOKENS = 2048
# 截断重试预算封顶：temperature=0 时确定性输出被 2048 截断会反复产出同一段截断 SQL，
# 故截断后翻倍预算让输出有机会续完；翻倍有上限，防止无界增长（0-2 交互修复）
_NL2SQL_TRUNCATION_BACKOFF = _NL2SQL_MAX_TOKENS * 2

# ---------------------------------------------------------------------------
# 计划解析观测性（M3）：失败原因分类。
# 每类失败沿日志单点输出 `reason=<常量>`，供按原因聚合失败率；常量即日志契约，
# 改名等于改监控口径，新增原因须同步 test_nl2sql_service.TestPlanParseObservability。
# ---------------------------------------------------------------------------
REASON_PLAN_REPLY_EMPTY = "PLAN_REPLY_EMPTY"  # 回复里没有可解析内容
REASON_PLAN_REPLY_NO_JSON = "PLAN_REPLY_NO_JSON"  # 回复里找不到 JSON 起始
REASON_PLAN_REPLY_TOO_LARGE = "PLAN_REPLY_TOO_LARGE"  # JSON 超过大小上限
REASON_PLAN_REPLY_JSON_INVALID = "PLAN_REPLY_JSON_INVALID"  # JSON 语法坏
REASON_PLAN_EMPTY = "PLAN_EMPTY"  # 解析成功但计划全空（判失败，走重试）
REASON_PLAN_DEGRADED = "PLAN_DEGRADED"  # 解析成功但有内容级丢弃（不失败，仅上报）


def _isEmptyPlan(plan: QueryPlan) -> bool:
    """计划是否「无可查询引用」：无 selectedClasses/conditions/aggregations 等
    任何可在本体 schema 中校验的真实引用。

    半空计划能通过 validatePlan（没有任何可校验的引用），随后被送进 SQL 生成，
    模型得以自由编造表名（结果报错被包装成"服务内部错误"）。故判为解析失败走重试，
    用尽后落到"无法回答"。

    判定取**最窄口径**：
    - 仅 ``target`` 不算合法内容（target 是模型对意图的**描述**，不是可查询引用）
    - 仅 ``rowLimit`` / ``perGroupLimit`` 不算合法内容（限制不是引用）
    - 仅 ``interpretation`` 不算（解释不是可查询目标）
    - ``selectedClasses`` / ``selectedProperties`` / ``conditions`` /
      ``aggregations`` / ``groupBy`` / ``joins`` / ``sortBy`` / ``partitionBy``
      任一非空即合法（这些是 plan 在本体 schema 中可被 validatePlan 校验的真实引用）

    ``target="无法回答"`` 的合法空计划不受影响（isUnanswerable 走自己的短路路径，
    _parsePlanOutcome 在更上层判空前放过）。

    半空计划闸门（方案A，2026-09-27）：``rowLimit`` / ``perGroupLimit`` 单独存在
    不再视为「有内容」。方案B（2026-09-27，本批）：``target`` 单独存在也不再视为
    「有内容」。这堵住 ``{"rowLimit": 100}`` / ``{"target": "查询XX"}`` 两条旁路，
    避免模型进 SQL 生成阶段自由选表/编表名。
    """
    return not (
        plan.selectedClasses
        or plan.selectedProperties
        or plan.conditions
        or plan.aggregations
        or plan.groupBy
        or plan.joins
        or plan.sortBy
        or plan.partitionBy
    )


@dataclass(frozen=True)
class _PlanParseOutcome:
    """计划解析结果（不可变）：plan 为 None 时 reason 必非空。"""

    plan: QueryPlan | None
    reason: str | None = None
    drops: tuple[PlanDrop, ...] = ()


def _parsePlanOutcome(content: str) -> _PlanParseOutcome:
    """从 LLM 回复解析查询计划：优先 ```json fence，其次裸 JSON 对象。

    失败不再静默：每种失败各有独立 reason，由调用方（重试循环）单点写日志。
    全空计划视为失败（see _isEmptyPlan）：它能通过 validatePlan，会直接进 SQL
    生成让模型自由编造表名。
    """
    match = _JSON_FENCE_RE.search(content)
    candidate = match.group(1) if match else content.strip()
    if not candidate:
        return _PlanParseOutcome(None, REASON_PLAN_REPLY_EMPTY)
    if not candidate.startswith("{"):
        # 尝试定位 JSON 起始
        brace = candidate.find("{")
        if brace == -1:
            return _PlanParseOutcome(None, REASON_PLAN_REPLY_NO_JSON)
        candidate = candidate[brace:]
    if len(candidate.encode("utf-8")) > _MAX_PLAN_JSON_BYTES:
        return _PlanParseOutcome(None, REASON_PLAN_REPLY_TOO_LARGE)
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return _PlanParseOutcome(None, REASON_PLAN_REPLY_JSON_INVALID)
    # 注：这里不需要 isinstance(data, dict) 兜底——上面的候选裁剪保证 candidate
    # 必以 '{' 开头，而 JSON 里以 '{' 开头的合法值只能是对象（数组是 '['）。
    # 该分支确为不可达死代码，2026-09-26 随 M3 观测性改造删除。
    #
    # 刻意不套 try/except：from_dictWithReport 与 from_dict 同一契约，**绝不抛错**
    # （历史 JSONB 不能让会话失败的前提），输入损坏一律转成 drops。故此调用点天然
    # 满足「结构损坏 → 重试」。⚠️ 若日后给 QueryPlan 加必填字段或 __post_init__
    # 校验，这个不变量会失效、异常将穿透重试循环（2026-09-26 code-reviewer 提示）。
    plan, drops = QueryPlan.from_dictWithReport(data)
    if plan.isUnanswerable:
        # ``target="无法回答"`` 是模型判定的合法空计划（语义：超出本体范围/答不了），
        # 必须在 _isEmptyPlan 之前短路——方案B 后 _isEmptyPlan 不再考虑 target 字段，
        # 否则 isUnanswerable 也会被当成 PLAN_EMPTY 走重试，浪费预算。
        return _PlanParseOutcome(plan, None, drops)
    if _isEmptyPlan(plan):
        return _PlanParseOutcome(None, REASON_PLAN_EMPTY, drops)
    return _PlanParseOutcome(plan, None, drops)


async def generateQueryPlan(
    question: str,
    classes: list[OntologyClass],
    llmClient: Any,
    modelConfig: Any,
    *,
    maxRetries: int | None = None,
    datasourceType: DataSourceType | str | None = None,
    oracle_version: str | None = None,
    schemaPrefix: str | None = None,
    context: str | None = None,
    priorState: str | None = None,
    initialErrors: list[str] | None = None,
    fewShot: str | None = None,
    valueSamples: dict[tuple[str, str], list[str]] | None = None,
    driftWarning: str | None = None,
    dictionaryText: str | None = None,
    joins: list[OntologyJoin] | None = None,
    featureCatalogText: str | None = None,
    scopeQuestion: str | None = None,
    globalFiltersText: str | None = None,
) -> PlanResult:
    """ReAct 推理阶段：生成结构化查询计划。

    调 LLM 输出 JSON 计划（选表/列/聚合/JOIN），解析为 QueryPlan。
    解析失败时注入错误重试（最多 maxRetries+1 次）；initialErrors 用于
    注入上一轮校验差异（见 generateValidatedPlan）。fewShot 为历史相似查询
    示例（1-2），经 _sanitizeContext 转义后注入 system prompt，仅作参考数据。
    driftWarning（2-4）为 schema 漂移告警文本，追加进 schema 小节。
    featureCatalogText（4.4）为可用 Feature 目录文本，经 _sanitizeContext
    转义后注入 system prompt（数据非指令）；None 不注入。
    globalFiltersText（feat-multistep-global-filter B 层）为多步场景下跨步骤共享的
    范围类约束（外购/内外贸/站点/物料类别/财年等），渲染进 user prompt 的
    [global_constraints] 块；None = 单步场景，不注入。
    返回不可变 PlanResult。
    """
    if maxRetries is None:
        maxRetries = getSettings().nl2sqlMaxRetries
    dialect = resolveDialect(datasourceType, oracle_version)
    schemaText = buildSchemaText(
        classes,
        schemaPrefix=schemaPrefix if dialect.useSchemaPrefix else None,
        valueSamples=valueSamples,
        driftWarning=driftWarning,
        joins=joins,
    )
    errors: list[str] = list(initialErrors or [])
    totalPrompt = 0
    totalCompletion = 0

    for attempt in range(maxRetries + 1):
        systemPrompt = _buildPlanSystemPrompt(
            schemaText, dialect, schemaPrefix,
            context=context, priorState=priorState, fewShot=fewShot,
            dictionaryText=dictionaryText,
            featureCatalogText=featureCatalogText,
        )
        userPrompt = _buildPlanUserPrompt(
            question, errors,
            scopeQuestion=scopeQuestion,
            globalFiltersText=globalFiltersText,
        )
        response = await completeWithTransientRetry(
            llmClient,
            messages=[
                LlmMessage(role="system", content=systemPrompt),
                LlmMessage(role="user", content=userPrompt),
            ],
            model=modelConfig.model_name,
            temperature=modelConfig.temperature if modelConfig and modelConfig.temperature is not None else 0.0,
            maxTokens=_NL2SQL_MAX_TOKENS,
            # M4：仅首轮允许额外一次同模型重试（预算 (maxRetries+1)+1），
            # 并把此前各轮已累加的用量挂到任何逃逸的异常上。
            allowRetry=attempt == 0,
            consumedTokenCounts=(totalPrompt, totalCompletion),
        )
        totalPrompt += response.promptTokens
        totalCompletion += response.completionTokens
        outcome = _parsePlanOutcome(response.content)
        if outcome.plan is None:
            # 单点 reason= 日志：按原因聚合失败率（M3 观测性）
            if outcome.drops:
                # PLAN_EMPTY 的成因往往就是「字段被丢光」⇒ 必须一并输出丢了什么，
                # 否则只知道"空了"、不知道"为什么空"（drops 会被构造出来又丢弃）
                logger.warning(
                    "NL2SQL 计划解析失败 attempt=%d reason=%s drops=%s",
                    attempt + 1,
                    outcome.reason,
                    formatPlanDrops(outcome.drops),
                )
                # 重试反馈带上具体丢失字段，让 LLM 下次知道补什么（前置核对 D）：
                # 否则 LLM 反复补 target 而忽略 selectedClasses/conditions 等真实缺口。
                dropsHint = ";".join(
                    f"{d.field}:{d.reason}" for d in outcome.drops
                )
                errors.append(
                    f"第 {attempt + 1} 次尝试未能解析出有效查询计划"
                    f"（丢失字段：{dropsHint}）"
                )
            else:
                logger.warning(
                    "NL2SQL 计划解析失败 attempt=%d reason=%s", attempt + 1, outcome.reason
                )
                errors.append(f"第 {attempt + 1} 次尝试未能从回复中解析出查询计划")
            continue
        if outcome.drops:
            logger.warning(
                "NL2SQL 计划解析降级 attempt=%d reason=%s drops=%s",
                attempt + 1,
                REASON_PLAN_DEGRADED,
                formatPlanDrops(outcome.drops),
            )
        return PlanResult(
            plan=outcome.plan, promptTokens=totalPrompt, completionTokens=totalCompletion
        )

    raise Nl2SqlError(
        MSG_NL2SQL_PLAN_INVALID,
        detail="; ".join(errors),
        tokens=(totalPrompt, totalCompletion),
    )


def _finalizePlan(
    planResult: PlanResult,
    classes: list[OntologyClass],
    joins: list[OntologyJoin] | None,
    scopeText: str = "",
) -> PlanResult:
    """统一出口：补充中间表 JOIN + 连通性校验 + 范围感知行数限制。

    validatePlan 通过后调用：supplementJoinPath 用 BFS 中间路径替换无直接关联的 JOIN，
    validateConnectivity 检查补充后是否仍存在不连通的表对；无 join 边时两者均不生效。
    scopeText 用于"按问题范围决定行数限制"（参见 _applyScopeRowLimit 与
    changes/feat-scope-aware-row-limit/summary.md）；单步为 question，多步为主问题∪子问题。
    """
    supplemented = supplementJoinPath(planResult.plan, classes, joins)
    connectivityIssues = validateConnectivity(supplemented, classes, joins)
    if connectivityIssues:
        raise Nl2SqlError(
            MSG_NL2SQL_PLAN_VALIDATION_FAILED,
            detail="; ".join(connectivityIssues),
            tokens=(planResult.promptTokens, planResult.completionTokens),
            lastPlan=supplemented,
        )
    scoped = _applyScopeRowLimit(supplemented, scopeText)
    return PlanResult(
        plan=scoped,
        promptTokens=planResult.promptTokens,
        completionTokens=planResult.completionTokens,
    )


def validatePlan(plan: QueryPlan, classes: list[OntologyClass]) -> list[str]:
    """纯代码校验计划引用是否在本体 schema 中。空列表 = 通过。

    不调 LLM，杜绝幻觉：检查选中的类、以及各类属性/聚合/分组/排序
    是否属于选定的类，JOIN 源/目标类与连接列是否存在。
    返回具体差异供重试反馈。
    """
    issues: list[str] = []
    classesById = {cls.class_name: cls for cls in classes}
    # 每类全部合法引用名（业务名/物理列/近义词别名，见 _propertyRefNames），
    # 供属性/分组/JOIN 列校验；聚合别名（含派生公式别名）供排序校验。
    propsByClass = {
        cls.class_name: _classRefNames(cls) for cls in classes
    }
    allPropNames = set().union(*propsByClass.values()) if propsByClass else set()
    aggAliases = _aggregationAliases(plan.aggregations)

    for name in plan.selectedClasses:
        if name not in classesById:
            issues.append(f"选中的类 {name} 不在本体 schema 中")

    # 属性校验限定在选定类内；未指定类时退回到全局属性集合
    if plan.selectedClasses:
        owned = set().union(*(propsByClass.get(cn, set()) for cn in plan.selectedClasses))
    else:
        owned = allPropNames

    for prop in plan.selectedProperties:
        if prop not in owned:
            issues.append(f"选中的属性 {prop} 不属于选定的任何类；{_propertyOwnerHint(prop, propsByClass)}")

    for agg in plan.aggregations:
        if agg.property not in owned:
            # 可操作报错：公式聚合的 property 常被误填成其他聚合的别名（如
            # AVG_PRICE_2026），property 应填公式主要引用的真实类属性，
            # 引用其他聚合别名应写在 formula 内（真实回归 2026-08-14）。
            if agg.formula:
                issues.append(
                    f"聚合属性 {agg.property} 不是选定的类的真实属性；"
                    f"公式聚合的 property 应填公式主要引用的真实属性，"
                    f"引用其他聚合别名应写在 formula 内（{agg.property} 是聚合别名，不是类属性）"
                )
            else:
                issues.append(
                    f"聚合属性 {agg.property} 不属于选定的任何类；"
                    f"{_propertyOwnerHint(agg.property, propsByClass)}"
                )
        # 派生指标硬约束（2026-08-17 真实回归）：alias 命中占比/比率/百分比
        # 等关键词时必须 formula（窗口函数 SUM(x)/SUM(SUM(x)) OVER ()），
        # 否则 SQL 不会算百分比，占比沦为列别名，重试耗尽后整步被标"无法回答"。
        # 报错须可操作，引导 LLM 写正确公式 + property 填真实类属性。
        if _aliasRequiresFormula(agg.alias) and not agg.formula:
            issues.append(
                f"聚合别名 {agg.alias} 是派生指标（占比/比率/百分比/比例/ratio/percent/share/pct），"
                f"必须使用 formula 表达式（窗口函数 SUM(x)/SUM(SUM(x)) OVER ()），"
                f"且 property 须填当前选中类的真实属性名；"
                f"若想用 SUM/COUNT/AVG 等基础聚合命名，请改用别名如 TOTAL_QTY/COUNT_NUM"
            )
        # 派生指标公式：校验公式中引用的属性是否都在本体里，杜绝幻觉。
        # 放行同计划内其他聚合的别名（如跨年比价公式 AVG_PRICE_2026 - AVG_PRICE_2025），
        # 与排序校验放行聚合 alias（_aggregationAliases）口径一致；未知引用仍拒绝。
        if agg.formula:
            parsed = parseFormula(agg.formula)
            # CTE 公式（WITH ... SELECT ... FROM cte_name）：CTE inner SELECT 的
            # 列名/表名（如 line_ratios.ratio、po_lines）不属于本体类属性，而是
            # CTE 内部定义。SQL Guard 已校验 CTE 语法，validatePlan 不对 CTE 内部
            # 的属性名做存在性校验（无法也无意义）；仅保留非 CTE 公式的校验逻辑。
            if not parsed.is_cte:
                for refProp in _extractFormulaProperties(agg.formula):
                    if refProp not in owned and refProp not in aggAliases:
                        issues.append(f"公式中的属性 {refProp} 不属于选定的任何类")

    for prop in plan.groupBy:
        if prop not in owned:
            hint = _timeBucketGroupHint(prop, classes) or _propertyOwnerHint(prop, propsByClass)
            issues.append(
                f"分组属性 {prop} 不属于选定的任何类" + (f"；{hint}" if hint else "")
            )

    # 「分别/各/每个 X 的 Top N」逐组取前 N 校验（2026-09-09）。
    # 语义红线：每组 Top-N 是分区内排名（ROW_NUMBER() OVER (PARTITION BY ...)），
    # 不是全局 N×组数坍缩。强制分区属性真实存在且为分组维、partitionBy 与
    # perGroupLimit 成对、组内有排序、rowLimit 必须为 null（防模型折出「前 N×组数」）。
    if bool(plan.partitionBy) != (plan.perGroupLimit is not None):
        issues.append(
            "partitionBy 与 perGroupLimit 必须成对设置：partitionBy 给出分区属性时，"
            "perGroupLimit 填每组取前 N；反之亦然（不要用全局 rowLimit 近似）"
        )
    for prop in plan.partitionBy:
        if prop not in owned:
            issues.append(f"分区属性 {prop} 不属于选定的任何类")
        elif prop not in plan.groupBy:
            issues.append(
                f"分区属性 {prop} 不在 groupBy 中：每组 Top-N 须先把分区维与取数维都放进 "
                f"groupBy（如 groupBy=[{prop}, 物料]、partitionBy=[{prop}]），再在分区内取前 N"
            )
    if plan.partitionBy and plan.perGroupLimit is not None:
        if plan.rowLimit is not None:
            issues.append(
                f"设置了 partitionBy/perGroupLimit 时 rowLimit 必须为 null"
                f"（每组 Top-N 不是全局前 {plan.rowLimit} 行；"
                f"把 N×组数折成全局行数是全局 Top-N 坍缩 bug 的根源，严禁）"
            )
        if not plan.sortBy:
            issues.append(
                "每组 Top-N 需在 sortBy 指定组内排序（聚合别名 desc，如 TOTAL_QTY desc），"
                "供 ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...) 使用"
            )

    for sort in plan.sortBy:
        # 排序合法引用 = 类属性引用名 ∪ 聚合别名（ORDER BY alias 在 ANSI SQL 合法，
        # 与 REFINE 捷径 3-5 排序列策略一致）。
        # 报错须可操作（真实回归 2026-08-14）：LLM 常把派生别名（如价格差异）只写进
        # sortBy 而未在聚合中声明公式聚合；原「不属于选定的任何类」无指引，重试仍生成
        # 同形计划。因此报错须引导把派生指标声明为公式聚合（alias 取排序名，formula
        # 引用其他聚合别名），并给出可用别名，供重试自愈。
        if sort.property not in owned and sort.property not in aggAliases:
            hint = (
                f"若 {sort.property} 是派生指标（如两年价格之差），请先在聚合中把它声明为"
                f"公式聚合（alias={sort.property}，formula 引用其他聚合别名）再在排序中引用该别名"
            )
            if aggAliases:
                aliases = sorted(aggAliases)
                if len(aliases) >= 2:
                    hint += f"，例如 {aliases[0]} - {aliases[1]}"
                hint += f"；可用的聚合别名：{', '.join(aliases)}"
            issues.append(f"排序属性 {sort.property} 不是类属性，也不是聚合别名；{hint}")

    for join in plan.joins:
        if join.sourceClass not in classesById:
            issues.append(f"JOIN 源类 {join.sourceClass} 不在本体 schema 中")
        if join.targetClass not in classesById:
            issues.append(f"JOIN 目标类 {join.targetClass} 不在本体 schema 中")
        sourceProps = propsByClass.get(join.sourceClass, set())
        targetProps = propsByClass.get(join.targetClass, set())
        for column in join.columns:
            if column not in sourceProps and column not in targetProps:
                issues.append(
                    f"JOIN 列 {column} 不属于 {join.sourceClass} 或 {join.targetClass} 的任何属性；"
                    "提示：join.columns 是列名数组（如 [\"SUPPLIER_CODE\", \"PARTNER_CODE\"]），"
                    "不要写 \"A = B\" 等式"
                )

    return issues


def validateConnectivity(
    plan: QueryPlan, classes: list[OntologyClass], joins: list[OntologyJoin] | None
) -> list[str]:
    """连通性校验（Feature B）：所有选中的类必须通过 join 目录的边连通。

    依赖 join 目录；无 join 边时本方法返回空列表（不误报）。
    需要配合 supplementJoinPath 在计划验证通过后补充中间表。
    """
    if len(plan.selectedClasses) <= 1 or not plan.joins:
        return []
    classToTable: dict[str, str] = {
        cls.class_name: cls.source_table
        for cls in classes
        if cls.source_table and cls.class_name
    }
    tablesWithSource = {
        classToTable[sc] for sc in plan.selectedClasses if sc in classToTable
    }
    if len(tablesWithSource) <= 1:
        return []
    graph = _buildJoinGraph(classes, joins)
    if not graph:
        return []
    start = next(iter(tablesWithSource))
    visited: set[str] = {start}
    queue = [start]
    while queue:
        node = queue.pop(0)
        for neighbor, _ in graph.get(node, []):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)
    disconnected = tablesWithSource - visited
    if disconnected:
        return [f"以下表无法通过关联路径连通：{', '.join(disconnected)}，请通过中间表建立 JOIN"]
    return []
