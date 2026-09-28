"""自然语言转 SQL 服务（门面）。

流程：本体 schema 文本注入 → LLM 生成 SQL → 解析 → SQL Guard 校验。
解析或安全校验失败时注入错误信息重试（最多 NL2SQL_MAX_RETRIES 次）。
纯函数式：不修改入参 classes，返回不可变 SqlResult。

**本文件是门面**：核心逻辑已按流水线阶段拆到同目录模块，方法体 → 模块函数，
名字不变（camelCase），本文件只做薄委托 + 保留两个编排入口（generateSql /
parseSqlFromResponse）与 SqlResult，并在底部 re-export 所有被测试/生产直接
import 的私有名，保证 30+ 处既有 import 零改动。

拆分模块（DAG 单向，无环）：
  nl2sql_dialects.py  SQL 方言规则
  nl2sql_refs.py      属性/类引用归一化 + 校验提示
  nl2sql_refine.py    REFINE 捷径（纯代码 SQL 改写）
  nl2sql_schema.py    schema 文本渲染 + JOIN 图 + 净化原语
  nl2sql_prompts.py   Plan/SQL 两阶段 Prompt 拼装
  nl2sql_scope.py     问题范围感知的行数限制
  nl2sql_plan.py      计划生成与校验（阶段一）
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
from app.domain.enums import DataSourceType
from app.domain.exceptions import Nl2SqlError, SqlSafetyError
from app.domain.models import OntologyClass, OntologyJoin
from app.domain.query_plan import QueryPlan
from app.infrastructure.business_db_pool import _assert_read_only
from app.infrastructure.llm.base_client import LlmMessage
from app.services.llm_retry_policy import completeWithTransientRetry
from app.services.messages_zh import (
    MSG_NL2SQL_PLAN_VALIDATION_FAILED,
    MSG_NL2SQL_SQL_INVALID,
)

from app.services.nl2sql_dialects import (
    SqlDialect,
    resolveDialect,
    _SQL_DIALECTS,
)
from app.services.nl2sql_plan import (
    generateQueryPlan,
    validatePlan,
    validateConnectivity,
    _finalizePlan,
    _parsePlanOutcome,
    _isEmptyPlan,
    _NL2SQL_MAX_TOKENS_DEFAULT,
    _NL2SQL_TRUNCATION_BACKOFF_DEFAULT,
)
from app.services.nl2sql_prompts import (
    _buildPlanSystemPrompt,
    _buildPlanUserPrompt,
    _buildSystemPrompt,
    _buildUserPrompt,
    _assertPriorCteSafe,
    _renderStatePart,
)
from app.services.nl2sql_refine import (
    applyRefineDirect,
    _REFINE_MAX_LIMIT_DEFAULT,
    _normalizeDate,
)
from app.services.nl2sql_refs import (
    _OWNER_HINT_MAX_CLASSES_DEFAULT,
    _normalizePlanProperties,
    _splitCompoundRef,
)
from app.services.nl2sql_schema import (
    buildSchemaText,
    supplementJoinPath,
    _pruneClassesForSql,
    _buildIndirectJoinHints,
    _sanitizeContext,
    _sanitizeSchemaField,
    _safeSchemaPrefix,
    _buildJoinGraph,
    _findJoinPath,
    _resolveRefTable,
    _CRITICAL_DIGEST_MAX_ITEMS_DEFAULT,
    _CRITICAL_DIGEST_MAX_DESC_CHARS_DEFAULT,
    _VALUE_SAMPLE_VALUE_MAX_DEFAULT,
)
from app.services.nl2sql_scope import (
    _applyScopeRowLimit,
    _coerceRowLimit,
    _hasExplicitRowIntent,
    _hasTimeScope,
)

# 匹配 ```sql ... ``` 或 ``` ... ``` 代码块
_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SqlResult:
    """NL2SQL 生成结果（不可变）。"""

    sql: str
    promptTokens: int
    completionTokens: int
    # 4-1（feat-token-cache）：DeepSeek prompt cache 命中 token 数；透传
    # LlmResponse.cachedTokens；与 plan 阶段 cachedTokens 合并进 _costFor。
    cachedTokens: int | None = None


# ---------------------------------------------------------------------------
# 魔数治理 Phase 2 hard tier：system_config 现读 getter（门面实例方法）。
# 与 chat_recall/chat_context 的 `_getXxx(session)` 同口径：不缓存、失败不阻断、
# int getter 非正返默认、text() 直写 key 不走 ORM。
# ---------------------------------------------------------------------------


async def _readIntConfig(session: AsyncSession, key: str, default: int) -> int:
    """读 system_config 的 int 配置；缺席/格式错/非正/异常返 default。

    与 ``ChatService._getClassFilterMaxClasses`` 同口径（SSOT），读失败不阻断主链路。
    """
    raw: str | None = None
    try:
        row = await session.execute(
            text(f"SELECT value FROM system_config WHERE key = '{key}'")
        )
        raw = row.scalar_one_or_none()
        if raw is None or raw == "":
            return default
        value = int(raw)
        if value <= 0:
            logger.warning(
                "%s 非正值 %r，返默认值 %d", key, raw, default,
            )
            return default
        return value
    except (TypeError, ValueError):
        logger.warning(
            "%s 值非法 %r，返默认值 %d", key, raw, default,
        )
        return default
    except Exception:
        logger.warning(
            "读取 %s 失败，返默认值 %d", key, default, exc_info=True,
        )
        return default


# 布尔开关的显式假值/真值字面量（大小写不敏感）。
_FALSY_CONFIG_VALUES: frozenset[str] = frozenset({"0", "false", "off", "no"})
_TRUTHY_CONFIG_VALUES: frozenset[str] = frozenset({"1", "true", "on", "yes"})


async def _readBoolConfig(session: AsyncSession, key: str, default: bool) -> bool:
    """读 system_config 的布尔开关；缺席/非法/异常返 default。

    与 `_readIntConfig` **刻意分开**：后者的契约是「非正返默认」，因此无法表达
    「0 = 关闭」——真实踩坑（2026-09-28 方案 A 的 A/B）：回滚开关初版用 `_readIntConfig`
    读，运维写 0 会被当成非法值回落默认 1，**开关永远关不掉**，A/B 两臂实际都是开启态，
    据此得出的「裁剪前后 token 逐字相同」是假象。

    可识别假值 "0"/"false"/"off"/"no"、真值 "1"/"true"/"on"/"yes"（大小写不敏感）；
    其他非空值按 int 解析（>0 为真，与 `_readIntConfig` 口径一致）。
    """
    raw: str | None = None
    try:
        row = await session.execute(
            text(f"SELECT value FROM system_config WHERE key = '{key}'")
        )
        raw = row.scalar_one_or_none()
    except Exception:
        logger.warning("读取 %s 失败，返默认值 %s", key, default, exc_info=True)
        return default
    if raw is None or raw == "":
        return default
    token = str(raw).strip().lower()
    if token in _FALSY_CONFIG_VALUES:
        return False
    if token in _TRUTHY_CONFIG_VALUES:
        return True
    try:
        return int(token) > 0
    except (TypeError, ValueError):
        logger.warning("%s 值非法 %r，返默认值 %s", key, raw, default)
        return default


async def _readSchemaConfig(session: AsyncSession) -> dict[str, int]:
    """在编排层一次性现读 schema 渲染 3 阈值 + token 上限（NL2SQL_MAX_TOKENS）。

    单事务同 session 读 4 个 key，避免 generateSql / generateValidatedPlan 内多次
    往返 DB；缺席/格式错/非正逐 key 返 _DEFAULT（单 key 失败不影响其他 key）。
    """
    return {
        "digestMaxItems": await _readIntConfig(
            session, "CRITICAL_DIGEST_MAX_ITEMS", _CRITICAL_DIGEST_MAX_ITEMS_DEFAULT
        ),
        "digestMaxDescChars": await _readIntConfig(
            session, "CRITICAL_DIGEST_MAX_DESC_CHARS", _CRITICAL_DIGEST_MAX_DESC_CHARS_DEFAULT
        ),
        "valueSampleValueMax": await _readIntConfig(
            session, "VALUE_SAMPLE_VALUE_MAX", _VALUE_SAMPLE_VALUE_MAX_DEFAULT
        ),
        "maxTokens": await _readIntConfig(
            session, "NL2SQL_MAX_TOKENS", _NL2SQL_MAX_TOKENS_DEFAULT
        ),
        # feat-token-prune：1=SQL 阶段按 plan 裁剪 schema（默认），0=退回全量
        # （回滚阀门——裁剪落在 SQL 正确性关键路径，留一个可即时关闭的开关）。
        # 用 _readBoolConfig 而非 _readIntConfig：后者非正返默认，表达不了「0=关」。
        "pruneSchema": await _readBoolConfig(
            session, "NL2SQL_SQL_SCHEMA_PRUNING", True
        ),
    }


async def _readPlanConfig(session: AsyncSession) -> dict[str, int]:
    """在编排层一次性现读 validatePlan 的 OWNER_HINT_MAX_CLASSES。"""
    return {
        "ownerHintMaxClasses": await _readIntConfig(
            session, "OWNER_HINT_MAX_CLASSES", _OWNER_HINT_MAX_CLASSES_DEFAULT
        ),
    }


async def _readRefineConfig(session: AsyncSession) -> dict[str, int]:
    """在编排层一次性现读 REFINE_MAX_LIMIT。"""
    return {
        "maxLimit": await _readIntConfig(
            session, "REFINE_MAX_LIMIT", _REFINE_MAX_LIMIT_DEFAULT
        ),
    }


class Nl2SqlService:
    """基于本体 schema 生成并校验只读 SQL。"""

    # ------------------------------------------------------------------
    # 薄委托：方法体已在模块里，此处只转发，保持公共/私有 API 签名不变。
    # ------------------------------------------------------------------

    @staticmethod
    def resolveDialect(
        datasourceType: DataSourceType | str | None, oracle_version: str | None = None
    ) -> SqlDialect:
        """按数据源类型解析方言；未指定或未知类型回退 Oracle（历史行为）。"""
        return resolveDialect(datasourceType, oracle_version)

    def buildSchemaText(
        self,
        classes: list[OntologyClass],
        *,
        schemaPrefix: str | None = None,
        valueSamples: dict[tuple[str, str], list[str]] | None = None,
        driftWarning: str | None = None,
        joins: list[OntologyJoin] | None = None,
    ) -> str:
        """将本体类列表渲染为 LLM 可读的 schema 文本（含外键 JOIN 关系与继承层级）。"""
        return buildSchemaText(
            classes,
            schemaPrefix=schemaPrefix,
            valueSamples=valueSamples,
            driftWarning=driftWarning,
            joins=joins,
        )

    def supplementJoinPath(
        self, plan: QueryPlan, classes: list[OntologyClass], joins: list[OntologyJoin] | None
    ) -> QueryPlan:
        """补充中间表 JOIN：无直接关联的 JOIN 用 BFS 中间路径替换。"""
        return supplementJoinPath(plan, classes, joins)

    def validateConnectivity(
        self, plan: QueryPlan, classes: list[OntologyClass], joins: list[OntologyJoin] | None
    ) -> list[str]:
        """连通性校验（Feature B）：所有选中的类必须通过 join 目录的边连通。"""
        return validateConnectivity(plan, classes, joins)

    def _buildIndirectJoinHints(
        self, classes: list[OntologyClass], joins: list[OntologyJoin] | None
    ) -> str:
        """生成 2 跳间接 JOIN 路径提示文本，供 schema prompt 注入。"""
        return _buildIndirectJoinHints(classes, joins)

    def _finalizePlan(
        self,
        planResult: Any,
        classes: list[OntologyClass],
        joins: list[OntologyJoin] | None,
        scopeText: str = "",
    ) -> Any:
        """统一出口：补充中间表 JOIN + 连通性校验 + 范围感知行数限制。"""
        return _finalizePlan(planResult, classes, joins, scopeText)

    def _parsePlanOutcome(self, content: str) -> Any:
        """从 LLM 回复解析查询计划：优先 ```json fence，其次裸 JSON 对象。"""
        return _parsePlanOutcome(content)

    def _buildPlanSystemPrompt(
        self,
        schemaText: str,
        dialect: SqlDialect,
        schemaPrefix: str | None,
        context: str | None = None,
        priorState: str | None = None,
        fewShot: str | None = None,
        dictionaryText: str | None = None,
        featureCatalogText: str | None = None,
    ) -> str:
        """推理阶段 System Prompt：要求模型先输出结构化查询计划 JSON。"""
        return _buildPlanSystemPrompt(
            schemaText, dialect, schemaPrefix,
            context=context, priorState=priorState, fewShot=fewShot,
            dictionaryText=dictionaryText,
            featureCatalogText=featureCatalogText,
        )

    def validatePlan(
        self, plan: QueryPlan, classes: list[OntologyClass],
        *, ownerHintMaxClasses: int = _OWNER_HINT_MAX_CLASSES_DEFAULT,
    ) -> list[str]:
        """纯代码校验计划引用是否在本体 schema 中。空列表 = 通过。

        ownerHintMaxClasses 为魔数治理 Phase 2 hard tier 注入参数：默认 _DEFAULT，
        编排层（generateValidatedPlan）传 system_config 现读值；直接调本方法时
        缺省为默认值（既有调用零改动）。
        """
        return validatePlan(plan, classes, ownerHintMaxClasses=ownerHintMaxClasses)

    def _buildPlanUserPrompt(
        self, question: str, errors: list[str], *,
        scopeQuestion: str | None = None,
        globalFiltersText: str | None = None,
    ) -> str:
        """推理阶段 User Prompt：注入主问题范围提示 + 聚合/层优先级 + 重试反馈。"""
        return _buildPlanUserPrompt(
            question, errors,
            scopeQuestion=scopeQuestion,
            globalFiltersText=globalFiltersText,
        )

    def _buildSystemPrompt(
        self,
        schemaText: str,
        dialect: SqlDialect,
        schemaPrefix: str | None,
        context: str | None = None,
        priorState: str | None = None,
        plan: QueryPlan | None = None,
        fewShot: str | None = None,
        prior_cte: str | None = None,
    ) -> str:
        """SQL 生成阶段 System Prompt。"""
        return _buildSystemPrompt(
            schemaText, dialect, schemaPrefix,
            context=context, priorState=priorState, plan=plan, fewShot=fewShot,
            prior_cte=prior_cte,
        )

    def _buildUserPrompt(
        self, question: str, errors: list[str], executionError: str | None = None,
        *, scopeQuestion: str | None = None,
    ) -> str:
        """SQL 生成阶段 User Prompt：注入主问题范围提示 + 执行错误/校验差异回灌。"""
        return _buildUserPrompt(question, errors, executionError, scopeQuestion=scopeQuestion)

    async def generateQueryPlan(
        self,
        question: str,
        classes: list[OntologyClass],
        llmClient: Any,
        modelConfig: Any,
        **kwargs: Any,
    ) -> Any:
        """ReAct 推理阶段：生成结构化查询计划。"""
        return await generateQueryPlan(question, classes, llmClient, modelConfig, **kwargs)

    async def _readPlanConfigOrDefault(
        self, session: AsyncSession | None,
    ) -> dict[str, int]:
        """魔数治理：session 提供时现读 plan 配置，缺席时落默认值。"""
        if session is None:
            return {"ownerHintMaxClasses": _OWNER_HINT_MAX_CLASSES_DEFAULT}
        return await _readPlanConfig(session)

    async def _readRefineConfigOrDefault(
        self, session: AsyncSession | None,
    ) -> dict[str, int]:
        """魔数治理：session 提供时现读 refine 配置，缺席时落默认值。"""
        if session is None:
            return {"maxLimit": _REFINE_MAX_LIMIT_DEFAULT}
        return await _readRefineConfig(session)

    async def _readSchemaConfigOrDefault(
        self, session: AsyncSession | None,
    ) -> dict[str, int]:
        """魔数治理：session 提供时现读 schema 配置，缺席时落默认值。"""
        if session is None:
            return {
                "digestMaxItems": _CRITICAL_DIGEST_MAX_ITEMS_DEFAULT,
                "digestMaxDescChars": _CRITICAL_DIGEST_MAX_DESC_CHARS_DEFAULT,
                "valueSampleValueMax": _VALUE_SAMPLE_VALUE_MAX_DEFAULT,
                "maxTokens": _NL2SQL_MAX_TOKENS_DEFAULT,
                "pruneSchema": True,
            }
        return await _readSchemaConfig(session)

    async def generateValidatedPlan(
        self,
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
        maxPlanAttempts: int = 2,
        fewShot: str | None = None,
        valueSamples: dict[tuple[str, str], list[str]] | None = None,
        driftWarning: str | None = None,
        dictionaryText: str | None = None,
        joins: list[OntologyJoin] | None = None,
        scopeQuestion: str | None = None,
        featureCatalogText: str | None = None,
        globalFiltersText: str | None = None,
        session: AsyncSession | None = None,
    ) -> Any:
        """生成并通过本体 schema 校验的查询计划（ReAct 两阶段流水线阶段一）。

        每轮：generateQueryPlan → validatePlan。校验不过时把具体差异
        （"表 X 不在本体"）注入重试反馈，而非泛泛 retry；纯代码校验杜绝幻觉。
        （此方法保留在门面并**经由 self 分发** generateQueryPlan / validatePlan /
        _finalizePlan，而非委托给模块函数——既有测试通过 monkeypatch 实例方法隔离
        校验循环，见 test_property_ref_normalize.TestGenerateValidatedPlanRetryNormalizes。）
        """
        common = dict(
            maxRetries=maxRetries,
            datasourceType=datasourceType,
            oracle_version=oracle_version,
            schemaPrefix=schemaPrefix,
            context=context,
            priorState=priorState,
            fewShot=fewShot,
            valueSamples=valueSamples,
            driftWarning=driftWarning,
            dictionaryText=dictionaryText,
            joins=joins,
            featureCatalogText=featureCatalogText,
            globalFiltersText=globalFiltersText,
        )
        # 多步子问题常丢失主问题的时间范围（如主问「2025 年采购情况」，
        # 子问题只剩「查各供应商采购额」）→ 并集判定，宁可不限也不误限。
        scopeText = question if scopeQuestion is None else f"{scopeQuestion}\n{question}"
        # 复合形式 property 归一化：LLM 偶尔从 schema 渲染文本 '业务名 (alias)'
        # 原样抄进 property_name，validatePlan 严格 token 匹配必拒；归一化在
        # 每次 generateQueryPlan 返回后都跑一次（首次 + 重试），确保 validatePlan
        # 看到的永远是清洗后的 plan（2026-09-18 真实回归 + code-review HIGH 修复）。
        planResult = await self.generateQueryPlan(
            question, classes, llmClient, modelConfig,
            scopeQuestion=scopeQuestion, **common,
        )
        planResult = replace(
            planResult, plan=_normalizePlanProperties(planResult.plan, classes),
        )
        # 魔数治理 Phase 2 hard tier：编排层现读 validatePlan 配置（session 缺席落默认）。
        planCfg = await self._readPlanConfigOrDefault(session)
        for _ in range(maxPlanAttempts - 1):
            issues = self.validatePlan(
                planResult.plan, classes, ownerHintMaxClasses=planCfg["ownerHintMaxClasses"]
            )
            if not issues:
                return self._finalizePlan(planResult, classes, joins, scopeText)
            planResult = await self.generateQueryPlan(
                question, classes, llmClient, modelConfig,
                initialErrors=issues, scopeQuestion=scopeQuestion, **common,
            )
            planResult = replace(
                planResult, plan=_normalizePlanProperties(planResult.plan, classes),
            )
        issues = self.validatePlan(
            planResult.plan, classes, ownerHintMaxClasses=planCfg["ownerHintMaxClasses"]
        )
        if issues:
            raise Nl2SqlError(
                MSG_NL2SQL_PLAN_VALIDATION_FAILED,
                detail="; ".join(issues),
                tokens=(planResult.promptTokens, planResult.completionTokens),
                lastPlan=planResult.plan,
            )
        return self._finalizePlan(planResult, classes, joins, scopeText)

    @staticmethod
    def applyRefineDirect(
        sql: str, plan: QueryPlan | None, question: str, *, maxLimit: int = _REFINE_MAX_LIMIT_DEFAULT
    ) -> str | None:
        """REFINE 捷径：纯代码改写上一轮 SQL（行数/排序/筛选），不调 LLM。

        maxLimit 为魔数治理 Phase 2 hard tier 注入参数：默认 _DEFAULT，
        编排层（generateSql）传 system_config 现读值；直接调本方法时
        缺省为默认值（既有调用零改动）。
        """
        return applyRefineDirect(sql, plan, question, maxLimit=maxLimit)

    # ------------------------------------------------------------------
    # 编排入口（保留在门面）：流式 SQL 解析 + SQL 生成。
    # ------------------------------------------------------------------

    def parseSqlFromResponse(self, content: str) -> str | None:
        """从 LLM 回复中提取 SQL：优先 ```sql/``` 代码块，其次纯 SELECT/WITH 文本。"""
        match = _SQL_FENCE_RE.search(content)
        if match:
            candidate = match.group(1).strip()
            if candidate:
                return candidate
        stripped = content.strip()
        if stripped.upper().startswith(("SELECT", "WITH")):
            return stripped
        return None

    async def generateSql(
        self,
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
        plan: QueryPlan | None = None,
        executionError: str | None = None,
        fewShot: str | None = None,
        valueSamples: dict[tuple[str, str], list[str]] | None = None,
        driftWarning: str | None = None,
        joins: list[OntologyJoin] | None = None,
        scopeQuestion: str | None = None,
        prior_cte: str | None = None,
        session: AsyncSession | None = None,
    ) -> SqlResult:
        """生成 SQL。最多 maxRetries+1 次尝试；失败注入错误重试。

        datasourceType 决定方言规则（Oracle FETCH FIRST / MySQL·PG LIMIT）；
        schemaPrefix 来自数据源 username，用于表名前缀与 JOIN 示例。
        context 为可选历史对话上下文；priorState 为上一轮查询状态（多轮注入）。
        executionError 用于执行失败后的回灌重试（1-3）：把上一次生成的 SQL 在
        数据库执行时报的错误回灌给模型，引导其修正 SQL。该错误经 _sanitizeContext
        转义后注入 user prompt，仅作数据而非指令。
        fewShot 为历史相似查询示例（1-2），经 _sanitizeContext 转义后注入
        system prompt，仅作参考数据。
        driftWarning（2-4）为 schema 漂移告警文本，追加进 schema 小节。
        scopeQuestion 为多步场景下的主问题原文，由 _renderScopeHintPart 转义后
        注入 user prompt；None = 单步场景，不注入。
        prior_cte 为多步串联场景下前序步骤已生成的 CTE（Task 3.2 / M8）：
        **唯一合法形态是 WITH-less 片段** `cte_alias AS (cte_body), ...`
        （由 render_prior_cte 产出）。经 `_assertPriorCteSafe` 校验后，本方法补上
        **唯一一个**前导 `WITH` 拼装到最终 SQL（`WITH prior_cte <sql>`），
        同时注入 system prompt 供当前步引用前序 CTE。
        入参自带前导 `WITH` 会被**立即拒绝**（否则拼成 `WITH WITH`，见 M8）。

        session 为可选魔数治理注入：提供时同步现读 schema 渲染阈值 +
        NL2SQL_MAX_TOKENS + REFINE_MAX_LIMIT；缺席时全部落默认值（既有调用零改动）。
        """
        if maxRetries is None:
            maxRetries = getSettings().nl2sqlMaxRetries
        # 魔数治理 Phase 2 hard tier：session 提供时现读 schema/token/refine 配置。
        schemaCfg = await self._readSchemaConfigOrDefault(session)
        refineCfg = await self._readRefineConfigOrDefault(session)
        dialect = resolveDialect(datasourceType, oracle_version)
        # SQL 阶段 schema 裁剪（feat-token-prune，2026-09-28）：计划阶段必须看全量
        # 召回类才能选类，但 SQL 阶段手上已有 plan——没有理由再投一遍全部 13~30 个类。
        # 实测（13 类召回 / 选中 2 类）：SQL 阶段 prompt 15,750 → 8,224 tokens，
        # 每步合计 33,126 → 25,600（−22.7%，nl2sql 占全部 token 94.9%）。
        # 裁剪只影响本函数的 buildSchemaText 入参（classes 在本函数无其它消费点），
        # fail-closed 语义见 _pruneClassesForSql 文档。
        schemaClasses = (
            _pruneClassesForSql(plan, classes) if schemaCfg["pruneSchema"] else classes
        )
        schemaText = buildSchemaText(
            schemaClasses,
            schemaPrefix=schemaPrefix if dialect.useSchemaPrefix else None,
            valueSamples=valueSamples,
            driftWarning=driftWarning,
            joins=joins,
            digestMaxItems=schemaCfg["digestMaxItems"],
            digestMaxDescChars=schemaCfg["digestMaxDescChars"],
            valueSampleValueMax=schemaCfg["valueSampleValueMax"],
        )
        errors: list[str] = []
        totalPrompt = 0
        totalCompletion = 0
        # 4-1（feat-token-cache）：累计 SQL 阶段 DeepSeek cache 命中数。
        # 任一次响应 None → 整体记 None（保守，理由同 plan 阶段）。
        totalCached: int | None = None
        # prior_cte 先行校验（Task 3.2 / M8）：来自 LLM 生成或上层显式传入。
        # 契约 = WITH-less 片段（前导 WITH 由本方法补），故先拒绝自带 WITH 的入参，
        # 再按拼接后的真实形态做只读校验（见 _assertPriorCteSafe）。
        if prior_cte:
            _assertPriorCteSafe(prior_cte)
        # 截断重试预算：首次为 maxTokens（system_config.NL2SQL_MAX_TOKENS 或默认），
        # 截断命中后翻倍（有上限）。
        # temperature=0 时同输入必得同输出，若预算不变，截断重试只会反复产出
        # 同一段截断 SQL（且注入的截断提示使输入变长、更易再截断）；翻倍预算让
        # 确定性输出有机会续完（0-2 交互修复）。
        maxTokens = schemaCfg["maxTokens"]
        truncationBackoff = maxTokens * 2

        for attempt in range(maxRetries + 1):
            systemPrompt = _buildSystemPrompt(
                schemaText, dialect, schemaPrefix,
                context=context, priorState=priorState, plan=plan, fewShot=fewShot,
                prior_cte=prior_cte,
            )
            userPrompt = _buildUserPrompt(question, errors, executionError, scopeQuestion=scopeQuestion)
            response = await completeWithTransientRetry(
                llmClient,
                messages=[
                    LlmMessage(role="system", content=systemPrompt),
                    LlmMessage(role="user", content=userPrompt),
                ],
                model=modelConfig.model_name,
                temperature=modelConfig.temperature if modelConfig and modelConfig.temperature is not None else 0.0,
                maxTokens=maxTokens,
                # M4：同计划阶段——仅首轮额外一次重试，逃逸异常带已累加用量。
                allowRetry=attempt == 0,
                consumedTokenCounts=(totalPrompt, totalCompletion),
            )
            totalPrompt += response.promptTokens
            totalCompletion += response.completionTokens
            # 4-1（feat-token-cache）：任一轮 cached_tokens=None → 整体记 None。
            if response.cachedTokens is not None:
                totalCached = (totalCached or 0) + response.cachedTokens
            sql = self.parseSqlFromResponse(response.content)
            if sql is None:
                errors.append(
                    f"第 {attempt + 1} 次尝试未能从回复中解析出 SQL"
                )
                logger.warning("NL2SQL 解析失败 attempt=%d", attempt + 1)
                continue
            # 截断检测（0-2）：回复达到 token 上限时 SQL 可能被截断，
            # 即使 parseSqlFromResponse 提取到片段也不要直接执行，注入错误并翻倍预算重试
            isApprox = getattr(response, "isApproximateUsage", False)
            if not isApprox and response.completionTokens >= maxTokens:
                errors.append(
                    f"第 {attempt + 1} 次尝试的回复达到 token 上限，SQL 可能被截断"
                )
                maxTokens = min(maxTokens * 2, truncationBackoff)
                continue
            # prior_cte 已有先行校验；若有 prior_cte 则将其 prepend 到 LLM SQL，
            # 再对组合后的完整 SQL 做 SQL Guard（Task 3.2）。
            if prior_cte:
                sql = f"WITH {prior_cte}\n{sql}"
            try:
                _assert_read_only(sql)
            except SqlSafetyError as exc:
                # 回注拒绝原因（str(exc) 只有原因：被拒 SQL 存在 exc.sql 里，不进消息体），
                # 否则模型只看到「未通过安全校验」而不知违规点，只能盲重试（M2）。
                # 仍不回显被拒 SQL 文本本身，避免把失败模式喂回给模型迭代。
                errors.append(
                    f"第 {attempt + 1} 次尝试生成的 SQL 未通过安全校验"
                    f"（仅允许 SELECT/WITH 只读查询）: {exc}"
                )
                logger.warning("NL2SQL 安全校验失败 attempt=%d: %s", attempt + 1, exc)
                continue
            return SqlResult(
                sql=sql,
                promptTokens=totalPrompt,
                completionTokens=totalCompletion,
                cachedTokens=totalCached,
            )

        raise Nl2SqlError(
            MSG_NL2SQL_SQL_INVALID,
            detail="; ".join(errors),
            tokens=(totalPrompt, totalCompletion),
        )


# ---------------------------------------------------------------------------
# re-export：被测试/生产直接 import 的私有名（门面模式，保证 30+ 处既有 import 零改动）
# ---------------------------------------------------------------------------
from app.services.nl2sql_plan import (
    _NL2SQL_MAX_TOKENS_DEFAULT as _NL2SQL_MAX_TOKENS_DEFAULT,
    _NL2SQL_TRUNCATION_BACKOFF_DEFAULT as _NL2SQL_TRUNCATION_BACKOFF_DEFAULT,
)
from app.services.nl2sql_refine import (
    _REFINE_MAX_LIMIT_DEFAULT as _REFINE_MAX_LIMIT_DEFAULT,
    _normalizeDate as _normalizeDate,
)
from app.services.nl2sql_refs import (
    _OWNER_HINT_MAX_CLASSES_DEFAULT as _OWNER_HINT_MAX_CLASSES_DEFAULT,
)
from app.services.nl2sql_schema import (
    _CRITICAL_DIGEST_MAX_ITEMS_DEFAULT as _CRITICAL_DIGEST_MAX_ITEMS_DEFAULT,
    _CRITICAL_DIGEST_MAX_DESC_CHARS_DEFAULT as _CRITICAL_DIGEST_MAX_DESC_CHARS_DEFAULT,
    _VALUE_SAMPLE_VALUE_MAX_DEFAULT as _VALUE_SAMPLE_VALUE_MAX_DEFAULT,
)
