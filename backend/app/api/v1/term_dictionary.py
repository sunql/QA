"""NL2SQL 术语字典 API。

术语（用户习惯用语 → 本体概念映射）的增删查：
- GET    /term-dictionary
- POST   /term-dictionary
- DELETE /term-dictionary/{id}
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.schemas import TermDictionaryCreate, TermDictionaryRead
from app.services.term_dictionary_service import TermDictionaryService

router = APIRouter(prefix="/term-dictionary", tags=["term-dictionary"])
_termDictionaryService = TermDictionaryService()


@router.get("", response_model=list[TermDictionaryRead], status_code=status.HTTP_200_OK)
async def listTerms(db: AsyncSession = Depends(getDb)) -> list[TermDictionaryRead]:
    """列出全部术语字典条目。"""
    entities = await _termDictionaryService.listTerms(db)
    return [TermDictionaryRead.model_validate(e) for e in entities]


@router.post("", response_model=TermDictionaryRead, status_code=status.HTTP_201_CREATED)
async def createTerm(
    dto: TermDictionaryCreate,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> TermDictionaryRead:
    """新增一条术语字典条目（created_by 取自当前用户，不信任客户端）。"""
    entity = await _termDictionaryService.createTerm(db, dto, createdBy=user.userId)
    return TermDictionaryRead.model_validate(entity)


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def deleteTerm(
    id: int,
    db: AsyncSession = Depends(getDb),
) -> None:
    """按 id 删除术语条目。"""
    await _termDictionaryService.deleteTerm(db, id)
