"""机制 4：结构化建议（feat-wiki-knowledge，Phase 8 M5）。

**两段式：先确定性预筛，再按需调用模型。** 这是本模块最重要的设计：

1. ``detectStructureTrigger`` 用正则判断这条知识**像不像**有结构可抽
   （阈值 / 步骤序列 / 公式 / 定义句）。没命中就不调模型 —— 一次调用都不花。
2. 只有命中触发器、且调用方给了 ``modelId``，才真正调模型抽取字段。

为什么必须先筛：结构化抽取是最容易失控的一路。机制 1/2/3 的调用次数由「新建
条目数」「有关系的条目对数」决定，而机制 4 若对每条知识都跑，导入 200 条就是
200 次调用，且**绝大多数条目根本没结构可抽**（一份 FAQ、一段说明文字）。确定性
预筛把成本压到「看起来像结构化知识的那几条」，代价是漏掉措辞不典型的条目 ——
这个取舍是对的：漏掉的可以手工触发，白花的 token 收不回来。

**维度由触发器判定，不由模型决定。** 模型的输出只填 ``extracted_structure``，
``suggested_dimension`` 始终取触发器命中的那个值。让模型同时决定「是什么维度」
和「填什么内容」，等于把两个可能各错一半的判断绑在一起，事后无法归因是分类错了
还是抽取错了。

**产出是建议而非既成事实**（与 M3/M4/M5 机制 3 一致）：落 ``structure_suggestion``
待业务专家接受/拒绝；只有接受（M6）才会物化成可执行规则或流程草稿。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy import text as saText
from sqlalchemy.dialects.postgresql import insert as pgInsert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.wiki_learning_models import StructureSuggestion
from app.domain.wiki_models import WikiPage
from app.services.learning.prompt_fence import neutralizeFence

if TYPE_CHECKING:  # 仅类型标注用，避免 service 间循环导入
    from app.services.learning.llm_invoker import LearningLLMInvoker

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "suggest_structure_v1.txt"

_STRUCTURE_PURPOSE = "wiki_structure_suggest"

# 触发器命中的维度。取值必须是 KNOWLEDGE_DIMENSIONS 的子集 —— 建议的
# suggested_dimension 会直接展示给业务专家，也是 M6 物化时的路由键。
TRIGGER_RULE = "RULE"
TRIGGER_PROCESS = "PROCESS"
TRIGGER_METRIC = "METRIC"
TRIGGER_CONCEPT = "CONCEPT"

STRUCTURE_TRIGGERS: tuple[str, ...] = (
    TRIGGER_RULE,
    TRIGGER_PROCESS,
    TRIGGER_METRIC,
    TRIGGER_CONCEPT,
)

# 抽取状态。四态而非两态，因为「为什么没有建议」有三种不同的修复动作：
# SKIPPED  没要求跑（没给模型 / 没命中触发器）—— 无事可做
# SUCCEEDED 跑通了
# FAILED   模型调用本身失败（无 key / 网络 / 非 JSON）—— 修模型接入
# INVALID  模型答了，但答的东西填不进任何结构（形状不合 / 必备字段为空）
#          —— 修 prompt，或这条知识确实抽不出结构
EXTRACTION_SKIPPED = "SKIPPED"
EXTRACTION_SUCCEEDED = "SUCCEEDED"
EXTRACTION_FAILED = "FAILED"
EXTRACTION_INVALID = "INVALID"

# 送模型的正文上限：结构化抽取要看完整算式/条件，比冲突判定（4000）放宽，
# 但仍设上限防止一次请求把整篇长文塞进去。
_MAX_CONTENT_CHARS = 8000

# ---------------------------------------------------------------------------
# 触发器正则
#
# 判定顺序固定为 RULE → PROCESS → METRIC → CONCEPT，取**第一个**命中的。
# 顺序有实际影响：一条「准时交付率 ≥ 95% 不达标则扣款」同时像 RULE 和 METRIC，
# 归到 RULE 更贴近它的用途（它是可执行判定，不是口径定义）。顺序写死并在此
# 说明，是为了避免以后有人调整顺序时无意改变已落库建议的维度分布。
# ---------------------------------------------------------------------------

# 比较方向：中文词与符号都收，因为业务文档两种都写。
_COMPARISON_RE = re.compile(
    r"(?:≥|≤|>=|<=|>|<|不低于|不少于|不高于|不超过|超过|高于|低于|大于|小于)"
)
# 比较方向后紧跟的数值（阿拉伯数字或中文数字）。
_THRESHOLD_RE = re.compile(
    r"(?:≥|≤|>=|<=|>|<|不低于|不少于|不高于|不超过|超过|高于|低于|大于|小于)"
    r"\s*[\d一二三四五六七八九十百千万零两]+"
)

# 有序步骤标记：`1.` / `1、` / `一、` / `第1步` / `- ` 开头。
_STEP_MARKER_RE = re.compile(
    r"(?m)^\s*(?:\d+\s*[.、)）]|[一二三四五六七八九十]+\s*[.、)）]|第\s*[一二三四五六七八九十\d]+\s*步|[-*•]\s+)"
)
# 至少这么多个步骤才算流程。2 个的清单太常见（「注意事项：1… 2…」），
# 把它当流程会产出一堆无用的 PROCESS 建议。
_MIN_STEPS = 3

# 公式：等号 + 比值语义（除号或比率词）。
_EQUALS_RE = re.compile(r"=|＝")
_RATIO_HINT_RE = re.compile(r"/|／|÷|率|占比|比例|百分比|均值|平均值")

# 定义句：术语 → 含义的系词结构。
_DEFINITION_RE = re.compile(r"是指|指的是|定义为|含义是|即为|又称|简称|叫做|俗称")


@lru_cache(maxsize=1)
def _loadSystemPrompt() -> str:
    """读取结构化抽取 system prompt（进程内缓存一次）。"""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def detectStructureTrigger(content: str) -> str | None:
    """判断正文像哪一类结构化知识；都不像则返回 ``None``。

    **只看正文，不看标题**：标题是标签而不是形状 —— 「供应商准入流程」这个标题
    既可能对应一份编号 SOP，也可能只是一段说明。拿标题当信号会让后者的正文被
    送去抽取，白花一次调用。

    判定是纯函数（无 IO、无状态），因此可以单测、可以在导入前预筛、也可以在
    写入口给用户提示「这条建议可以结构化」而不产生任何调用。
    """
    if not content:
        return None
    if _THRESHOLD_RE.search(content):
        return TRIGGER_RULE
    if len(_STEP_MARKER_RE.findall(content)) >= _MIN_STEPS:
        return TRIGGER_PROCESS
    if _EQUALS_RE.search(content) and _RATIO_HINT_RE.search(content):
        return TRIGGER_METRIC
    if _DEFINITION_RE.search(content):
        return TRIGGER_CONCEPT
    return None


def validateStructure(kind: str, data: Any) -> dict[str, Any] | None:
    """按维度校验并**归一化**模型输出；不可用则返回 ``None``。

    返回的是重新构造的字典而非原始 ``data``：模型常会附带解释性字段（``note``、
    ``reason``…），原样落库会让 ``extracted_structure`` 的形状随模型心情漂移，
    下游没法稳定读取。这里只保留契约内的键。

    校验必须在**写库前**做。先落库再等人工发现它没法用，比不落更差：建议列表里
    混进空壳，审核的人得逐条点开才知道是废的。
    """
    if not isinstance(data, dict):
        return None
    if kind == TRIGGER_RULE:
        return _validateRule(data)
    if kind == TRIGGER_PROCESS:
        return _validateProcess(data)
    if kind == TRIGGER_METRIC:
        return _validateMetric(data)
    if kind == TRIGGER_CONCEPT:
        return _validateConcept(data)
    return None


def _validateRule(data: dict[str, Any]) -> dict[str, Any] | None:
    """RULE 必备：非空 conditions，每条含 field + operator。

    只要求这两个字段：``value`` / ``unit`` 在「要求提交资质证明」这类不带数值的
    条件里本就为空，强求会让可用的规则被判废。
    """
    conditions = data.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return None
    normalized: list[dict[str, Any]] = []
    for raw in conditions:
        if not isinstance(raw, dict):
            return None
        field = raw.get("field")
        operator = raw.get("operator")
        if not _nonEmptyStr(field) or not _nonEmptyStr(operator):
            return None
        normalized.append(
            {
                "field": field.strip(),
                "operator": operator.strip(),
                "value": _conditionValue(raw.get("value"), operator),
                "unit": _stringOrNone(raw.get("unit")),
            }
        )
    action = data.get("action")
    if action is not None and not isinstance(action, dict):
        return None
    return {
        "conditions": normalized,
        "action": action if isinstance(action, dict) else {},
    }


def _validateProcess(data: dict[str, Any]) -> dict[str, Any] | None:
    """PROCESS 必备：非空 steps，每步含 name。

    ``seq`` 由**下标**重新生成而不是信模型给的值：模型偶尔会跳号或从 0 开始，
    而 seq 是流程可视化与「下一步是什么」的唯一序键，乱号会让渲染顺序错乱。
    """
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(steps, start=1):
        if not isinstance(raw, dict):
            return None
        name = raw.get("name")
        if not _nonEmptyStr(name):
            return None
        normalized.append(
            {
                "seq": index,
                "name": name.strip(),
                "actor_role": _stringOrNone(raw.get("actor_role")),
                "action": _stringOrNone(raw.get("action")),
                "sla_hours": raw.get("sla_hours")
                if isinstance(raw.get("sla_hours"), (int, float))
                else None,
                "condition": _stringOrNone(raw.get("condition")),
            }
        )
    return {"steps": normalized, "trigger": _stringOrNone(data.get("trigger"))}


def _validateMetric(data: dict[str, Any]) -> dict[str, Any] | None:
    """METRIC 必备：非空 formula。分子分母可缺（正文常只给算式不给拆分）。"""
    formula = data.get("formula")
    if not _nonEmptyStr(formula):
        return None
    return {
        "formula": formula.strip(),
        "numerator": _stringOrNone(data.get("numerator")),
        "denominator": _stringOrNone(data.get("denominator")),
        "unit": _stringOrNone(data.get("unit")),
    }


def _validateConcept(data: dict[str, Any]) -> dict[str, Any] | None:
    """CONCEPT 必备：term + definition。"""
    term = data.get("term")
    definition = data.get("definition")
    if not _nonEmptyStr(term) or not _nonEmptyStr(definition):
        return None
    aliases = data.get("aliases")
    return {
        "term": term.strip(),
        "definition": definition.strip(),
        "aliases": [a.strip() for a in aliases if _nonEmptyStr(a)]
        if isinstance(aliases, list)
        else [],
    }


def _nonEmptyStr(value: Any) -> bool:
    """是否是非空字符串（去掉空白后仍有内容）。"""
    return isinstance(value, str) and bool(value.strip())


def _stringOrNone(value: Any) -> str | None:
    """字符串归一化：非字符串或空串一律 ``None``，不留 ``""`` 这种半成品值。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


# 集合算子的取值是**数组**（与求值器 ``_compareMembership`` 的契约一致）
_MEMBERSHIP_OPERATORS = frozenset({"IN", "NOT IN"})


def _conditionValue(value: Any, operator: str) -> Any:
    """按算子归一化条件的取值。

    集合算子单独走一条路：``_stringOrNone`` 只放行字符串与数值，数组会被压成
    ``None``。压掉之后这条规则**仍能通过物化的全部校验**（算子合法、条件非空），
    只是永远判不了 —— 审核人在 dry-run 里看到的是「没命中」，不是「规则坏了」。

    形状不对时不做二次裁决，原样交给物化阶段拒绝（``_normalizedConditions`` 是
    取值类型的唯一裁决点）：抽取阶段对取值形状保持宽松，与它对待算子同款态度。
    """
    if (
        isinstance(operator, str)
        and operator.strip().upper() in _MEMBERSHIP_OPERATORS
        and isinstance(value, list)
    ):
        items = [_stringOrNone(item) for item in value]
        return [item for item in items if item is not None]
    return _stringOrNone(value)


@dataclass(frozen=True)
class SuggestionGenerationResult:
    """一次结构化抽取的结果（不可变）。

    ``triggeredKind`` 与 ``extractionStatus`` 必须分开报：没命中触发器、命中但
    没给模型、给了模型但抽取失败 —— 三种情况的 ``suggestions`` 都是空列表，
    只看列表长度无法区分，用户也就无从知道该不该给个模型再试一次。
    """

    suggestions: tuple[StructureSuggestion, ...]
    triggeredKind: str | None
    extractionStatus: str


class StructureSuggester:
    """机制 4：为单条知识产出结构化建议。"""

    async def suggestForPage(
        self,
        session: AsyncSession,
        pageId: str,
        *,
        invoker: LearningLLMInvoker | None = None,
    ) -> SuggestionGenerationResult:
        """预筛 + 抽取 + 落库，返回**本次新增**的建议。

        ``invoker`` 为空则只做预筛（报告 ``triggeredKind``，不产建议、零 token）。
        """
        page = await _loadPage(session, pageId)
        kind = detectStructureTrigger(page.content)
        if kind is None:
            return SuggestionGenerationResult((), None, EXTRACTION_SKIPPED)
        if invoker is None:
            return SuggestionGenerationResult((), kind, EXTRACTION_SKIPPED)
        if await self._hasPending(session, page.page_id, kind):
            # 已有待处置的同维度建议：再抽一次也会被 ON CONFLICT 丢掉，白付一次
            # 调用费。这里提前挡掉，让「重复点一下按钮」不再产生成本。
            return SuggestionGenerationResult((), kind, EXTRACTION_SKIPPED)

        try:
            parsed, _ = await invoker.completeJson(
                systemPrompt=_loadSystemPrompt(),
                userPrompt=_buildStructurePrompt(page, kind),
                mechanism="STRUCTURE",
                purpose=_STRUCTURE_PURPOSE,
            )
        except Exception as e:  # noqa: BLE001 - 失败降级为状态上报，见模块说明
            logger.warning("机制 4 结构化抽取失败（page=%s kind=%s）: %s", pageId, kind, e)
            return SuggestionGenerationResult((), kind, EXTRACTION_FAILED)

        structure = validateStructure(kind, parsed)
        confidence = _confidenceOf(parsed)
        proposals = (
            []
            if structure is None
            else [
                {
                    "page_id": page.page_id,
                    "suggested_dimension": kind,
                    "extracted_structure": structure,
                    "confidence": confidence,
                }
            ]
        )
        created = await self._persist(session, proposals)
        status = EXTRACTION_INVALID if structure is None else EXTRACTION_SUCCEEDED
        return SuggestionGenerationResult(tuple(created), kind, status)

    async def _hasPending(
        self, session: AsyncSession, pageId: str, kind: str
    ) -> bool:
        """该条目是否已有待处置的同维度建议。"""
        result = await session.execute(
            select(StructureSuggestion.id).where(
                StructureSuggestion.page_id == pageId,
                StructureSuggestion.suggested_dimension == kind,
                StructureSuggestion.status == "PENDING",
            )
        )
        return result.first() is not None

    async def _persist(
        self, session: AsyncSession, proposals: list[dict[str, Any]]
    ) -> list[StructureSuggestion]:
        """幂等写入建议，返回**本次真正新增**的行。

        用 ``ON CONFLICT DO NOTHING`` + ``RETURNING``（同机制 2/3）：先查后插在并发
        下会双双通过检查，第二条撞唯一约束冒 500；``RETURNING`` 只回吐真正插入的
        行，正好是「本次新增」的定义。

        冲突目标是**部分**唯一索引 ``uq_structure_suggestion_pending``，故必须同时
        给出 ``index_where`` —— PG 要靠它把 ON CONFLICT 推断到那个部分索引上，少了
        它直接报「no unique or exclusion constraint matching」。

        **没有建议时也必须 commit**：本方法同时承担「把 LLM 计量行落库」的责任。
        计量只 flush 不 commit，请求会话关闭时会一起回滚，于是「调了模型但抽不出
        结构」这条路径会把花掉的 token 记成 0（M4 的既有教训）。
        """
        ids: list[int] = []
        if proposals:
            stmt = (
                pgInsert(StructureSuggestion)
                .values(proposals)
                .on_conflict_do_nothing(
                    index_elements=["page_id", "suggested_dimension"],
                    index_where=saText("status = 'PENDING'"),
                )
                .returning(StructureSuggestion.id)
            )
            ids = list((await session.execute(stmt)).scalars().all())

        # 单点提交：建议与计量行同生共死（见 docstring）
        # 必须是 commit 不是 flush：getDb 依赖只在异常时 rollback，正常路径关闭
        # 会话时不 commit，没 commit 的 INSERT 在会话关掉时就回滚掉了——前端点了
        # 「生成建议」返回 id=18，关掉会话后 id=18 就消失了（M7 的回归）。
        await session.commit()
        if not ids:
            return []

        result = await session.execute(
            select(StructureSuggestion)
            .where(StructureSuggestion.id.in_(ids))
            .order_by(StructureSuggestion.id)
        )
        return list(result.scalars().all())


async def _loadPage(session: AsyncSession, pageId: str) -> WikiPage:
    """取待抽取的条目（复用 WikiPageService 的 404 语义）。"""
    from app.services.wiki_page_service import WikiPageService

    return await WikiPageService().getPage(session, pageId)


def _confidenceOf(parsed: dict[str, Any]) -> float | None:
    """取模型自报的把握度，越界或非数值一律视为没给。

    DB 列是 ``NUMERIC(4,3)``，能存的最大值不到 10；放一个 100 进去会直接
    溢出错，而这是模型随手就能编出来的值。越界即丢弃，不截断 —— 截断会把
    「模型乱填」伪装成「模型很有把握」。
    """
    value = parsed.get("confidence")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not 0.0 <= float(value) <= 1.0:
        return None
    return float(value)


def _buildStructurePrompt(page: WikiPage, kind: str) -> str:
    """拼装结构化抽取的 user prompt（动态内容全部过围栏中和）。

    围栏中和是硬要求而非风格：正文里写一句 ``</user_content>`` 就能把后续文字抬成
    「围栏之外的指令」，而这里被抬高的指令可以直接伪造 ``conditions``/``formula``
    —— 那些字段在 M6 会物化成 Agent 可执行的规则。M4 曾因替换值写成空操作而失去
    这层防护（见 summary §8.5），故本模块一律经 ``neutralizeFence``，不自己写
    ``replace``。
    """
    return (
        f"目标维度：{kind}\n\n"
        f"<user_content>\n标题：{neutralizeFence(page.title)}\n"
        f"正文：\n{neutralizeFence(page.content[:_MAX_CONTENT_CHARS])}\n</user_content>"
    )


__all__ = [
    "StructureSuggester",
    "SuggestionGenerationResult",
    "detectStructureTrigger",
    "validateStructure",
    "STRUCTURE_TRIGGERS",
    "TRIGGER_RULE",
    "TRIGGER_PROCESS",
    "TRIGGER_METRIC",
    "TRIGGER_CONCEPT",
    "EXTRACTION_SKIPPED",
    "EXTRACTION_SUCCEEDED",
    "EXTRACTION_FAILED",
    "EXTRACTION_INVALID",
]
