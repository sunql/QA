"""M7 Hypothesis Hook（v3.1 B6 / 蓝图 §5.6）：数据查询完成后的可选假设后处理。

数据查询完成后，若用户问题命中归因类词表（``isHypothesisTrigger``）且本轮确有
数据结果，追加一次 LLM 调用生成 ≤3 条「可能解释」假设（每条含 schema driver +
只读验证 SQL），落库并在前端答案下方展示「可能原因」区块。

关键裁决（brief §触发设计）：IntentType 13 类已冻结且无 RootCause 类型，v1 只走
「用户显式说『分析原因』」分支——纯词表触发，``intent_service`` / ``chat_recall``
路由行为零改动。

红线：
- best-effort：假设链路任何一步失败 → 空列表 + warning，绝不中断主回答；
- 计量：假设生成的 LLM 调用 ``purpose="hypothesis"`` 进 ``_recordUsage``，
  失败路径上已消耗的 token 经 ``_consumedTokens`` 补账（M4 口径）；
- verification_sql 只存储不执行——执行走用户显式发起的既有 QUERY 链路
  （SQL Guard 自然生效），存储前仅做轻量只读静态校验。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schemas import HypothesisRead
from app.infrastructure.llm.base_client import LlmMessage
from app.services.llm_json_fence import stripJsonFence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 触发词表（词表驱动，后续按 query 日志扩充）
# ---------------------------------------------------------------------------
# 中英文归因类问法；IntentType 冻结（无 RootCause），v1 只走词表触发分支。
HYPOTHESIS_TRIGGER_PHRASES: tuple[str, ...] = (
    "分析原因",
    "原因是什么",
    "什么原因",
    "原因分析",
    "归因",
    "为什么下降",
    "为什么上升",
    "为什么减少",
    "为什么增加",
    "为何下降",
    "为何上升",
    "怎么回事",
)

# 英文触发词按独立词匹配（两侧补空格做边界，避免误伤含 why 子串的词）
_HYPOTHESIS_ENGLISH_TRIGGER = " why "

# 蓝图 §5.6 硬上限
HYPOTHESIS_MAX_COUNT = 3

# 因果句式（prompt 硬约束的解析出口第二层）：同时含「因为…所以」的陈述判为
# 因果断言，整条丢弃（容错——不抛错，只降级该条）
_CAUSAL_PATTERN = ("因为", "所以")

# verification_sql 只读静态校验：首 token 白名单 + DML/DDL 词边界黑名单。
# 完整防线仍是执行链路的 SQL Guard，这里只挡明显越权的存储。
_SQL_FORBIDDEN_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|"
    r"EXEC|EXECUTE|CALL|VACUUM|COPY|SET|BEGIN|COMMIT|ROLLBACK|ATTACH|DETACH|"
    r"PRAGMA|LOCK|COMMENT|REINDEX|CLUSTER|EXPLAIN)\b",
    re.IGNORECASE,
)
_SQL_ALLOWED_PREFIXES = ("SELECT", "WITH")

# LLM 假设生成的 system prompt 硬约束（JSON-only + 禁因果句式 + 只引用给定 driver）
_HYPOTHESIS_SYSTEM_PROMPT = (
    "你是数据分析助手。基于用户问题与已查询到的数据摘要，给出最多 "
    f"{HYPOTHESIS_MAX_COUNT} 条数据波动或结果的「可能解释」假设。\n"
    "硬约束：\n"
    "1. 只输出「可能解释」，禁止任何「因为X所以Y」的因果断言句式；\n"
    "2. driver 字段只能从下方给出的可用列/指标列表中选择，没有合适值就填 null；\n"
    "3. 每条假设附一条可独立执行的只读 SELECT 验证 SQL（不得含任何 DML/DDL）；\n"
    "4. 只输出 JSON 数组，不要任何解释性文字，格式：\n"
    '[{"statement": "可能解释", "driver": "列名或null", '
    '"verification_sql": "SELECT ..."}]'
)

_HYPOTHESIS_PROMPT_MARKER = "可能原因假设"


@dataclass(frozen=True)
class Hypothesis:
    """一条「可能解释」假设（不可变）。"""

    statement: str        # 可能解释（禁止因果断言）
    driver: str | None    # schema 中发现的 driver（列名/指标名）
    verificationSql: str  # 只读 SELECT，可独立执行验证


def isHypothesisTrigger(question: str | None) -> bool:
    """纯函数触发判定（零 DB 依赖）：问题文本命中归因词表即 True。

    触发的第二前提「本轮确有数据」由 ``HypothesisMixin._maybeGenerateHypotheses``
    把关——无数据时即使命中词表也静默跳过，不调 LLM。
    """
    if not question or not question.strip():
        return False
    normalized = f" {question.lower().strip()} "
    if _HYPOTHESIS_ENGLISH_TRIGGER in normalized:
        return True
    return any(phrase in normalized for phrase in HYPOTHESIS_TRIGGER_PHRASES)


def isValidVerificationSql(sql: str | None) -> bool:
    """verification_sql 存储前的轻量只读静态校验。

    首 token 必须 SELECT/WITH；词边界匹配的 DML/DDL 关键词一律拒绝
    （词边界保证 ``UPDATE_TIME``/``SORT_ORDER`` 这类列名不误杀）。
    """
    if not sql or not sql.strip():
        return False
    cleaned = sql.strip().rstrip(";").strip()
    firstToken = cleaned.split(None, 1)[0].upper() if cleaned.split() else ""
    if firstToken not in _SQL_ALLOWED_PREFIXES:
        return False
    return _SQL_FORBIDDEN_RE.search(cleaned) is None


def _hypothesisFromItem(item: object) -> Hypothesis | None:
    """把单个 JSON 项解析为 Hypothesis；畸形项返回 None（丢弃，不抛错）。"""
    if not isinstance(item, dict):
        return None
    statement = str(item.get("statement", "") or "").strip()
    sql = str(
        item.get("verification_sql") or item.get("verificationSql") or ""
    ).strip()
    if not statement or not isValidVerificationSql(sql):
        return None
    if all(p in statement for p in _CAUSAL_PATTERN):
        return None  # 因果断言：prompt 硬约束的解析出口第二层
    driverRaw = item.get("driver")
    driver = str(driverRaw).strip() if driverRaw is not None else None
    return Hypothesis(
        statement=statement,
        driver=driver or None,
        verificationSql=sql,
    )


def parseHypotheses(raw: str) -> list[Hypothesis]:
    """解析 LLM 假设回复：fence 剥离 + JSON 解析 + 畸形项丢弃 + 裁剪到 3 条。

    任何解析失败（非 JSON / 非 list / 空结果）都返回空列表——调用方按
    best-effort 降级处理。
    """
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(stripJsonFence(raw))
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = data.get("hypotheses")
    if not isinstance(data, list):
        return []
    hypotheses: list[Hypothesis] = []
    for item in data:
        parsed = _hypothesisFromItem(item)
        if parsed is not None:
            hypotheses.append(parsed)
        if len(hypotheses) >= HYPOTHESIS_MAX_COUNT:
            break
    return hypotheses


def extractDriverNames(classes: list) -> list[str]:
    """从召回的本体类属性名提取可用 driver 提示（让 LLM 只能引用存在的列）。

    属性经 ``listClasses`` 的 selectinload 已加载；任何异常都按「无提示」降级。
    """
    drivers: list[str] = []
    try:
        for cls in classes:
            for prop in getattr(cls, "properties", None) or []:
                name = getattr(prop, "property_name", None)
                if name and name not in drivers:
                    drivers.append(name)
    except Exception as exc:  # pragma: no cover - 防御分支
        logger.warning("driver 提示提取失败，降级为空列表: %s", exc)
        return []
    return drivers[:30]


def buildHypothesisMessages(
    question: str, dataSummary: str, drivers: list[str],
) -> list[LlmMessage]:
    """构造假设生成的 LLM 消息（question/dataSummary 经围栏隔离进 user prompt）。"""
    from app.services.learning.prompt_fence import neutralizeFence

    driverText = "、".join(drivers) if drivers else "（无可用列提示）"
    userPrompt = (
        f"<user_content>\n{neutralizeFence(question)}\n"
        f"{neutralizeFence(dataSummary)}\n</user_content>\n\n"
        f"{_HYPOTHESIS_PROMPT_MARKER}生成任务。可用列/指标：{driverText}"
    )
    return [
        LlmMessage(role="system", content=_HYPOTHESIS_SYSTEM_PROMPT),
        LlmMessage(role="user", content=userPrompt),
    ]


# ---------------------------------------------------------------------------
# 落库 / 查询（模块级函数，mixin 与 API 共用）
# ---------------------------------------------------------------------------


async def saveHypotheses(
    session: AsyncSession,
    sessionId: str,
    turnQuestion: str,
    hypotheses: list[Hypothesis],
) -> list:
    """批量落库一轮假设（独立事务：失败由调用方 best-effort 吞掉）。

    返回落库后的 ORM 行（id/created_time 已由 flush 赋值），供响应 DTO 复用。
    """
    from app.domain.models import AnalysisHypothesis

    if not hypotheses:
        return []
    rows = [
        AnalysisHypothesis(
            session_id=sessionId,
            statement=h.statement,
            driver=h.driver,
            verification_sql=h.verificationSql,
            turn_question=turnQuestion,
        )
        for h in hypotheses
    ]
    session.add_all(rows)
    await session.flush()
    await session.commit()
    return rows


async def listSessionHypotheses(
    session: AsyncSession, sessionId: str, limit: int = 10,
) -> list:
    """返回某 session 最新假设（created_time 倒序 limit N）。"""
    from app.domain.models import AnalysisHypothesis

    stmt = (
        select(AnalysisHypothesis)
        .where(AnalysisHypothesis.session_id == sessionId)
        .order_by(AnalysisHypothesis.created_time.desc(), AnalysisHypothesis.id.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


class HypothesisMixin:
    """挂在 ChatService：数据查询完成后的可选假设生成（模式同 MultiStepMixin）。

    非流式（chat_service._handleGenericQuery / chat_multistep._executeMultiStep）
    与流式（chat_stream._streamQuery / _streamMultiStep）四条链路共用本方法，
    禁止复制粘贴逻辑（B5 收编教训）。
    """

    async def _maybeGenerateHypotheses(
        self,
        session: AsyncSession,
        sessionId: str,
        question: str,
        pc,
        *,
        data: list[dict],
    ) -> list:
        """触发词表 + 本轮有数据 → 恰好 +1 次 LLM（purpose="hypothesis"）→ 落库。

        返回落库后的假设 DTO（HypothesisRead，非流式直接附到 ChatResponse；
        流式不进 SSE 帧，前端靠 GET 端点取）。任何一步失败都降级为空列表 +
        warning，绝不中断主回答链路。
        """
        try:
            if not data or not isHypothesisTrigger(question):
                return []
            client = self._llmFactory(pc.selected)  # noqa: SLF001 - mixin 契约
            if client is None:
                logger.warning("假设生成跳过：模型配置无可用客户端（session=%s）", sessionId)
                return []
            messages = buildHypothesisMessages(
                question, self._summarizeStepData(data), extractDriverNames(pc.classes),
            )
            resp = await client.complete(messages)
            await self._recordUsage(
                session, sessionId, pc.selected,
                resp.promptTokens, resp.completionTokens,
                purpose="hypothesis",
                cachedTokens=getattr(resp, "cachedTokens", None),
            )
            hypotheses = parseHypotheses(resp.content)
            if not hypotheses:
                logger.warning("假设生成解析为空（session=%s），按无假设降级", sessionId)
                return []
            rows = await saveHypotheses(session, sessionId, question, hypotheses)
            return [HypothesisRead.model_validate(r) for r in rows]
        except Exception as exc:
            await self._accountHypothesisFailure(session, sessionId, pc, exc)
            logger.warning("假设生成失败，best-effort 降级（session=%s）: %s", sessionId, exc)
            return []

    async def _accountHypothesisFailure(
        self, session: AsyncSession, sessionId: str, pc, exc: Exception,
    ) -> None:
        """失败路径的已消耗 token 补账（M4 口径：逃逸异常必须带走用量）。"""
        try:
            promptTokens, completionTokens = self._consumedTokens(exc)
            if promptTokens or completionTokens:
                await self._recordUsage(
                    session, sessionId, pc.selected,
                    promptTokens, completionTokens, purpose="hypothesis",
                )
        except Exception as accountExc:  # pragma: no cover - 防御分支
            logger.warning("假设失败路径用量补账失败: %s", accountExc)
