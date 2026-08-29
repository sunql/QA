"""NL2SQL 术语字典服务。

用户在对答中发现 LLM 误解某术语（如"实际到货""订的数量""占比"）时，将术语的
真实含义、映射的本体类/属性、以及结构性提示（如占比公式）录入；计划阶段把全部
术语渲染进 prompt，帮助 LLM 正确理解业务习惯用语。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import TermDictionary
from app.domain.schemas import TermDictionaryCreate
from app.services.messages_zh import MSG_TERM_DICT_NOT_FOUND, MSG_TERM_DICT_TERM_EXISTS
from app.services.nl2sql_service import _sanitizeSchemaField


def renderDictionaryText(terms: list[TermDictionary]) -> str | None:
    """把术语列表渲染为计划 prompt 的术语词典段；空列表返回 None。

    每个字段经 _sanitizeSchemaField 转义尖括号并折叠换行，与本体 schema
    字段同等待遇，防止词典内容被用于构造标签逃逸或注入裸行指令。
    """
    if not terms:
        return None
    lines: list[str] = []
    for t in terms:
        parts = [f"- 「{_sanitizeSchemaField(t.term)}」：{_sanitizeSchemaField(t.definition)}"]
        extra: list[str] = []
        if t.mapped_class_name:
            extra.append(f"类 {_sanitizeSchemaField(t.mapped_class_name)}")
        if t.mapped_property_name:
            extra.append(f"属性 {_sanitizeSchemaField(t.mapped_property_name)}")
        if t.formula_hint:
            extra.append(_sanitizeSchemaField(t.formula_hint))
        if extra:
            parts.append("（" + "、".join(extra) + "）")
        lines.append("".join(parts))
    return "\n".join(lines)


class TermDictionaryService:
    """术语字典 CRUD + 计划 prompt 文本渲染。"""

    async def listTerms(self, session: AsyncSession) -> list[TermDictionary]:
        result = await session.execute(select(TermDictionary).order_by(TermDictionary.id))
        return list(result.scalars().all())

    async def createTerm(
        self, session: AsyncSession, dto: TermDictionaryCreate, *, createdBy: str | None = None
    ) -> TermDictionary:
        existing = await session.execute(
            select(TermDictionary).where(TermDictionary.term == dto.term)
        )
        if existing.scalar_one_or_none() is not None:
            raise ValidationError(MSG_TERM_DICT_TERM_EXISTS.format(term=dto.term))
        entity = TermDictionary(
            term=dto.term,
            definition=dto.definition,
            mapped_class_name=dto.mapped_class_name,
            mapped_property_name=dto.mapped_property_name,
            formula_hint=dto.formula_hint,
            created_by=createdBy,
        )
        session.add(entity)
        await session.commit()
        await session.refresh(entity)
        return entity

    async def deleteTerm(self, session: AsyncSession, id: int) -> None:
        entity = await session.get(TermDictionary, id)
        if entity is None:
            raise NotFoundError(MSG_TERM_DICT_NOT_FOUND.format(id=id))
        await session.delete(entity)
        await session.commit()

    async def buildDictionaryText(self, session: AsyncSession) -> str | None:
        """加载并渲染全部术语为计划 prompt 的术语词典段；空表返回 None。"""
        return renderDictionaryText(await self.listTerms(session))
