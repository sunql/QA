"""报告侧 prompt context：业务术语 + 维度语义 + 分析模板（R3-C）。

注入到 `report_planner` 的 `_NARRATIVE_SYSTEM` / `_COMMENTARY_SYSTEM` 调用
（作为 `<user_content>` 的一个 part）。本模块**只做组装**：不调 LLM、不改数据、
不写库 —— 与 `report_planner` 的「纯装配」定位一致。

**数字免疫**：本模块渲染进 prompt 的只有术语的 `definition` 文本与静态模板，
绝不渲染 finding 行数据。静态模板因此刻意**不含数字**（如不写「> 1000x」）——
`test_llm_prompt_excludes_raw_rows` 用 `"100" not in promptText` 钉死这条不变量，
模板里任何形如 `1000` 的字面量都会以子串形式击穿它。

失败降级：DB 不可达 / 术语加载异常 → 跳过术语段，仍返回分析模板段（报告不炸）。
"""

from __future__ import annotations

import logging
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import TermDictionary

logger = logging.getLogger(__name__)

__all__ = ["ANALYSIS_TEMPLATES", "assembleSemanticContext"]

# 硬编码中文分析模板。组装时只取前 MAX_SEMANTIC_TEMPLATES 条，避免 prompt 膨胀。
# 约束：**不得含数字字面量**（见模块 docstring 的「数字免疫」）。
ANALYSIS_TEMPLATES: Final[list[str]] = [
    "建议关注占比 TopN 的维度，识别主要贡献者",
    "对比上月/上季度的环比变化，标注异常点（z-score 大于 2）",
    "金额跨度大（最大值远大于最小值）时，提示长尾风险",
    "供应商确认率偏低时，建议核查未确认挂账",
    "基地/区域维度不均衡时，提示业务集中度",
]

MAX_SEMANTIC_TEMPLATES = 3  # 注入的模板条数上限
MAX_SEMANTIC_TERMS = 3  # 注入的术语条数上限

_TERMS_HEADER = "## 维度业务含义"
_TEMPLATES_HEADER = "## 分析模板（参考，不要照抄）"


async def assembleSemanticContext(
    session: AsyncSession,
    *,
    columns: tuple[str, ...] = (),
    sourceTable: str | None = None,
) -> str:
    """生成 LLM 用的语义 context 块（术语段 + 模板段）。

    `columns` 应为 finding **行数据的列名**（不是 claim_text —— 后者是自然语言
    句子，永远匹配不上 `mapped_property_name`）。`sourceTable` 当前未参与匹配，
    保留为后续按表限定术语的扩展位。

    返回空串的唯二情形：模板常量被清空。术语段缺失不影响模板段。
    """
    parts: list[str] = []

    terms = await _safeLoadTerms(session, columns)
    if terms:
        parts.append(_TERMS_HEADER)
        parts += [
            f"- {term.term}：{term.definition or '（暂无定义）'}"
            for term in terms[:MAX_SEMANTIC_TERMS]
        ]

    if ANALYSIS_TEMPLATES:
        parts.append(_TEMPLATES_HEADER)
        parts += [f"- {tmpl}" for tmpl in ANALYSIS_TEMPLATES[:MAX_SEMANTIC_TEMPLATES]]

    return "\n".join(parts)


async def _safeLoadTerms(
    session: AsyncSession, columns: tuple[str, ...]
) -> list[TermDictionary]:
    """术语加载的降级包装：任何异常 → 空列表 + 留痕（不静默）。"""
    try:
        return await _loadTerms(session, columns)
    except Exception:  # noqa: BLE001 —— 术语只是 prompt 装饰，读失败不该炸掉报告
        logger.warning("报告语义 context：术语加载失败，跳过术语段", exc_info=True)
        return []


async def _loadTerms(
    session: AsyncSession, columns: tuple[str, ...]
) -> list[TermDictionary]:
    """加载 `mapped_property_name` 命中 `columns` 的术语（大小写不敏感）。

    `order_by(id)` 保证注入顺序确定（截断到 MAX_SEMANTIC_TERMS 时才可复现）。
    """
    if not columns:
        return []
    columnSet = {column.lower() for column in columns}
    rows = (
        (await session.execute(select(TermDictionary).order_by(TermDictionary.id)))
        .scalars()
        .all()
    )
    return [
        term
        for term in rows
        if (term.mapped_property_name or "").lower() in columnSet
    ]
