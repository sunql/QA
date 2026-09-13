"""机制 6：事实原子（claim）与证据（evidence）抽取。"""
from __future__ import annotations
import hashlib, logging, re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.domain.wiki_models import CLAIM_OBJECT_TYPES, Evidence, KnowledgeClaim, WikiPage
from app.services.learning.prompt_fence import neutralizeFence

logger = logging.getLogger(__name__)

EXTRACTION_SUCCEEDED = "SUCCEEDED"
EXTRACTION_SKIPPED = "SKIPPED"
EXTRACTION_ALREADY_DONE = "ALREADY_DONE"
EXTRACTION_FAILED = "FAILED"
EXTRACTION_INVALID = "INVALID"
_MECHANISM = "CLAIM"
_MAX_CONTENT_CHARS = 6000
_MAX_CLAIM_TEXT_CHARS = 1000
_HASH_HEX_LENGTH = 64
_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_PROMPT_PATH = Path(__file__).parent / "prompts" / "extract_claim_v1.txt"

@dataclass(frozen=True)
class ClaimExtractionResult:
    claims: tuple[KnowledgeClaim, ...]
    status: str
    claimCount: int = 0
    evidenceCount: int = 0

def hashExcerpt(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:_HASH_HEX_LENGTH]

def locateExcerpt(content: str, quote: str):
    needle = quote.strip()
    if not needle:
        return None, None
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", content or "")]
    currentSection: str | None = None
    paragraphNo = 0
    for block in paragraphs:
        if not block:
            continue
        heading = _HEADING_PATTERN.match(block.splitlines()[0])
        if heading is not None:
            currentSection = heading.group(1).strip() or None
            continue
        paragraphNo += 1
        if needle in block:
            return currentSection, paragraphNo
    return None, None

def validateClaims(payload: Mapping[str, Any]):
    rawClaims = payload.get("claims")
    if not isinstance(rawClaims, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw in rawClaims:
        if not isinstance(raw, Mapping):
            continue
        text = _optionalText(raw.get("claim_text"), _MAX_CLAIM_TEXT_CHARS)
        if text is None:
            continue
        normalized.append({
            "claim_text": text,
            "claim_type": _optionalText(raw.get("claim_type"), 30),
            "subject_id": _optionalText(raw.get("subject_id"), 128),
            "predicate": _optionalText(raw.get("predicate"), 100),
            "object_value": _optionalText(raw.get("object_value"), None),
            "object_type": _boundedObjectType(raw.get("object_type")),
            "confidence": _boundedConfidence(raw.get("confidence")),
            "evidence_quote": _optionalText(raw.get("evidence_quote"), None),
        })
    return normalized

def _optionalText(value: Any, maxLength: int | None):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if maxLength is not None and len(text) > maxLength:
        return text[:maxLength]
    return text

def _boundedObjectType(value: Any):
    text = _optionalText(value, 30)
    if text is None:
        return None
    upper = text.upper()
    return upper if upper in CLAIM_OBJECT_TYPES else None

def _boundedConfidence(value: Any):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or number > 1:
        return None
    return number

def _loadSystemPrompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")

def _buildUserPrompt(page: WikiPage) -> str:
    content = (page.content or "")[:_MAX_CONTENT_CHARS]
    return f"条目标题：{page.title}\n\n<user_content>\n{neutralizeFence(content)}\n</user_content>"

class ClaimExtractor:
    async def extractForPage(self, session: AsyncSession, pageId: str, *, invoker: Any | None = None, force: bool = False):
        page = await _loadPage(session, pageId)
        # 关键：在第一次 await 之前把所有 ORM 字段提取为原始值，
        # 避免后续访问触发 lazy-load 而跨 await 边界（MissingGreenlet）。
        page_id = page.page_id
        page_content = page.content
        page_title = page.title
        page_authority_level = page.authority_level
        if invoker is None:
            return ClaimExtractionResult((), EXTRACTION_SKIPPED)
        hasClaims = await self._hasClaims(session, page_id)
        if hasClaims and not force:
            return ClaimExtractionResult((), EXTRACTION_ALREADY_DONE)
        # force 语义：先拿到有效的新抽取结果，再删旧数据。
        # 若 LLM 失败（FAILED）或输出无效（INVALID）直接返回，旧 claims 原样保留 —
        # 「我认为旧的已过时」不构成在网络抖动时丢数据的理由。
        try:
            parsed, _ = await invoker.completeJson(
                systemPrompt=_loadSystemPrompt(),
                userPrompt=_buildUserPrompt(page),
                mechanism=_MECHANISM,
                purpose="wiki_claim_extract",
                maxTokens=16000,
            )
        except Exception as e:
            logger.warning("机制6抽取失败(page=%s): %s", pageId, e)
            return ClaimExtractionResult((), EXTRACTION_FAILED)
        drafts = validateClaims(parsed)
        if not drafts:
            return ClaimExtractionResult((), EXTRACTION_INVALID)
        if hasClaims:
            # 删除与插入同事务（evidence 由 DB ondelete=CASCADE 连带删除），
            # 由调用方统一 commit/rollback，不会出现「删了没写」的中间态。
            await self._deleteClaims(session, page_id)
        created, evidenceCount = await self._persist(session, page_id, page_title, page_authority_level, page_content, drafts)
        return ClaimExtractionResult(
            tuple(created), EXTRACTION_SUCCEEDED,
            claimCount=len(created),
            # 不读 ``c.evidences`` 关系属性：它是 lazy="select"，在 async
            # 上下文访问未加载集合会 MissingGreenlet。计数在 _persist 内同步得出。
            evidenceCount=evidenceCount,
        )

    async def _hasClaims(self, session: AsyncSession, pageId: str) -> bool:
        result = await session.execute(
            select(KnowledgeClaim.id).where(KnowledgeClaim.page_id == pageId).limit(1)
        )
        return result.first() is not None

    async def _deleteClaims(self, session: AsyncSession, pageId: str) -> None:
        """删除某 Page 的全部 claims；evidence 依赖 DB ``ondelete=CASCADE`` 连带清理。"""
        await session.execute(
            delete(KnowledgeClaim).where(KnowledgeClaim.page_id == pageId)
        )
        await session.flush()

    async def _persist(self, session: AsyncSession, page_id: str, page_title: str | None, page_authority_level: str | None, page_content: str, drafts: list[dict[str, Any]]):
        """落库 claims + evidences，返回 ``(claims, evidenceCount)``。"""
        created: list[KnowledgeClaim] = []
        for draft in drafts:
            claim = KnowledgeClaim(
                page_id=page_id,
                claim_text=draft["claim_text"],
                claim_type=draft["claim_type"],
                subject_id=draft["subject_id"],
                predicate=draft["predicate"],
                object_value=draft["object_value"],
                object_type=draft["object_type"],
                confidence=draft["confidence"],
                authority_level=page_authority_level,
                status="ACTIVE",
                triple_stale=False,
            )
            session.add(claim)
            created.append(claim)
        await session.flush()
        evidenceCount = 0
        for claim, draft in zip(created, drafts, strict=True):
            if self._appendEvidence(session, page_id, page_title, page_content, claim, draft.get("evidence_quote")):
                evidenceCount += 1
        await session.flush()
        return created, evidenceCount

    def _appendEvidence(self, session: AsyncSession, page_id: str, page_title: str | None, page_content: str, claim: KnowledgeClaim, quote: str | None) -> bool:
        """有 quote 才落 evidence，返回是否实际写入。"""
        if quote is None:
            return False
        sectionName, paragraphNo = locateExcerpt(page_content, quote)
        session.add(Evidence(
            claim_id=claim.id,
            source_type="WIKI_PAGE",
            source_id=page_id,
            page_number=None,
            section_name=(sectionName or page_title)[:200] if sectionName or page_title else None,
            paragraph_no=paragraphNo,
            content=quote,
            content_hash=hashExcerpt(quote),
        ))
        return True

async def _loadPage(session: AsyncSession, pageId: str) -> WikiPage:
    from app.services.wiki_page_service import WikiPageService
    return await WikiPageService().getPage(session, pageId)

__all__ = [
    "EXTRACTION_SUCCEEDED", "EXTRACTION_SKIPPED", "EXTRACTION_ALREADY_DONE",
    "EXTRACTION_FAILED", "EXTRACTION_INVALID", "ClaimExtractionResult",
    "ClaimExtractor", "hashExcerpt", "locateExcerpt", "validateClaims",
]
