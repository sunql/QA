"""Two-Step CoT 知识分析（feat-wiki-knowledge 增强）。

借鉴 llm_wiki-main 的两步链式思考（Chain-of-Thought）摄取模式：
- Step 1 (Analysis): 深度分析内容，提取实体、概念、论点、本体关联建议
- Step 2 (Generation): 基于分析结果生成结构化知识条目

与单步分类相比，两步法能：
1. 提供更丰富的知识元数据（实体、概念、论点）
2. 自动发现与本体（Ontology）的关联
3. 提前检测潜在冲突
4. 为后续结构化提供建议

设计约束：
- 分析结果作为「建议」而非「决定」，业务专家可覆盖
- 本体关联建议默认不生效，需人工审核（与 KnowledgeRelation.confirmed 机制一致）
- 分析失败不阻断导入，降级为单步分类
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.domain.wiki_models import KNOWLEDGE_DIMENSIONS
from app.services.learning.prompt_fence import neutralizeFence

if TYPE_CHECKING:
    from app.services.learning.llm_invoker import LearningLLMInvoker, LearningLlmResult

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "analyze_v1.txt"

# 分析需要更多上下文，但也不能无限长
_MAX_CONTENT_CHARS = 8000


@lru_cache(maxsize=1)
def _loadAnalysisPrompt() -> str:
    """读取分析 prompt（进程内缓存一次）。"""
    return _PROMPT_PATH.read_text(encoding="utf-8")


@dataclass(frozen=True)
class EntitySuggestion:
    """实体识别结果。"""

    name: str
    type: str
    role: str  # central / peripheral
    ontology_class_suggestion: str | None = None


@dataclass(frozen=True)
class ConceptSuggestion:
    """概念识别结果。"""

    name: str
    definition: str
    relevance: str


@dataclass(frozen=True)
class OntologyLinkSuggestion:
    """本体关联建议。"""

    target_type: str  # ONTOLOGY_CLASS / ONTOLOGY_METRIC / ENTITY_MAPPING
    target_id: str
    relation_type: str  # DESCRIBES / DEFINES / APPLIES_TO / AFFECTS / REFERENCES
    confidence: float
    reasoning: str


@dataclass(frozen=True)
class ConflictWarning:
    """冲突预警。"""

    conflict_type: str
    description: str
    severity: str  # HIGH / MEDIUM / LOW


@dataclass(frozen=True)
class ClaimSuggestion:
    """建议抽取的事实原子。"""

    text: str
    type: str  # FACT / DEFINITION / RULE / STATISTIC


@dataclass(frozen=True)
class StructureSuggestion:
    """结构建议。"""

    suggested_structure: dict[str, Any]
    claims_to_extract: tuple[ClaimSuggestion, ...]


@dataclass(frozen=True)
class ImportAnalysis:
    """导入分析结果（Two-Step CoT Step 1 输出）。"""

    # 知识分类（与 AutoClassifier 输出对齐）
    dimension: str
    dimension_confidence: float
    dimension_alternatives: tuple[str, ...]
    dimension_reason: str

    # 深度分析结果
    key_entities: tuple[EntitySuggestion, ...]
    key_concepts: tuple[ConceptSuggestion, ...]
    main_claims: tuple[str, ...]
    evidence_summary: str
    evidence_confidence: str  # high / medium / low

    # 本体关联建议
    ontology_links: tuple[OntologyLinkSuggestion, ...]

    # 冲突预警
    conflicts: tuple[ConflictWarning, ...]

    # 结构建议
    structure_suggestion: StructureSuggestion | None

    def toDict(self) -> dict[str, Any]:
        """转成 JSON 可序列化字典。"""
        return {
            "dimension": {
                "primary": self.dimension,
                "confidence": self.dimension_confidence,
                "alternatives": list(self.dimension_alternatives),
                "reason": self.dimension_reason,
            },
            "key_entities": [
                {
                    "name": e.name,
                    "type": e.type,
                    "role": e.role,
                    "ontology_class_suggestion": e.ontology_class_suggestion,
                }
                for e in self.key_entities
            ],
            "key_concepts": [
                {
                    "name": c.name,
                    "definition": c.definition,
                    "relevance": c.relevance,
                }
                for c in self.key_concepts
            ],
            "main_arguments": {
                "claims": list(self.main_claims),
                "evidence": self.evidence_summary,
                "confidence": self.evidence_confidence,
            },
            "ontology_links": [
                {
                    "target_type": link.target_type,
                    "target_id": link.target_id,
                    "relation_type": link.relation_type,
                    "confidence": link.confidence,
                    "reasoning": link.reasoning,
                }
                for link in self.ontology_links
            ],
            "conflicts": [
                {
                    "conflict_type": c.conflict_type,
                    "description": c.description,
                    "severity": c.severity,
                }
                for c in self.conflicts
            ],
            "structure_suggestions": (
                {
                    "suggested_structure": self.structure_suggestion.suggested_structure,
                    "claims_to_extract": [
                        {"text": c.text, "type": c.type}
                        for c in self.structure_suggestion.claims_to_extract
                    ],
                }
                if self.structure_suggestion
                else None
            ),
        }


def _clampConfidence(raw: Any) -> float:
    """收敛置信度到 [0, 1]。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, value))


def _parseDimension(parsed: dict[str, Any]) -> tuple[str, float, tuple[str, ...], str]:
    """解析维度分类结果。"""
    dim = parsed.get("dimension", {})
    primary = dim.get("primary", "CONCEPT")
    if primary not in KNOWLEDGE_DIMENSIONS:
        logger.warning("分析结果维度不在白名单，使用默认值: %r", primary)
        primary = "CONCEPT"

    alternatives = tuple(
        a
        for a in (dim.get("alternatives") or [])
        if isinstance(a, str) and a in KNOWLEDGE_DIMENSIONS and a != primary
    )[:2]

    return (
        primary,
        _clampConfidence(dim.get("confidence")),
        alternatives,
        str(dim.get("reason", ""))[:200],
    )


def _parseEntities(parsed: dict[str, Any]) -> tuple[EntitySuggestion, ...]:
    """解析实体列表。"""
    entities = []
    for e in parsed.get("key_entities", [])[:10]:  # 限制数量
        if not isinstance(e, dict) or "name" not in e:
            continue
        entities.append(
            EntitySuggestion(
                name=str(e.get("name", ""))[:100],
                type=str(e.get("type", ""))[:50],
                role=str(e.get("role", "peripheral"))[:20],
                ontology_class_suggestion=(
                    str(e.get("ontology_class_suggestion"))[:100]
                    if e.get("ontology_class_suggestion")
                    else None
                ),
            )
        )
    return tuple(entities)


def _parseConcepts(parsed: dict[str, Any]) -> tuple[ConceptSuggestion, ...]:
    """解析概念列表。"""
    concepts = []
    for c in parsed.get("key_concepts", [])[:10]:
        if not isinstance(c, dict) or "name" not in c:
            continue
        concepts.append(
            ConceptSuggestion(
                name=str(c.get("name", ""))[:100],
                definition=str(c.get("definition", ""))[:500],
                relevance=str(c.get("relevance", ""))[:200],
            )
        )
    return tuple(concepts)


def _parseOntologyLinks(parsed: dict[str, Any]) -> tuple[OntologyLinkSuggestion, ...]:
    """解析本体关联建议。"""
    valid_types = {"ONTOLOGY_CLASS", "ONTOLOGY_METRIC", "ENTITY_MAPPING"}
    valid_relations = {"DESCRIBES", "DEFINES", "APPLIES_TO", "AFFECTS", "REFERENCES"}

    links = []
    for link in parsed.get("ontology_links", [])[:5]:  # 限制数量
        if not isinstance(link, dict):
            continue
        target_type = str(link.get("target_type", ""))
        relation_type = str(link.get("relation_type", ""))

        if target_type not in valid_types or relation_type not in valid_relations:
            logger.warning(
                "无效的本体关联类型: target_type=%s, relation_type=%s",
                target_type,
                relation_type,
            )
            continue

        links.append(
            OntologyLinkSuggestion(
                target_type=target_type,
                target_id=str(link.get("target_id", ""))[:128],
                relation_type=relation_type,
                confidence=_clampConfidence(link.get("confidence")),
                reasoning=str(link.get("reasoning", ""))[:200],
            )
        )
    return tuple(links)


def _parseConflicts(parsed: dict[str, Any]) -> tuple[ConflictWarning, ...]:
    """解析冲突预警。"""
    conflicts = []
    for c in parsed.get("conflicts", [])[:5]:
        if not isinstance(c, dict):
            continue
        severity = str(c.get("severity", "MEDIUM"))
        if severity not in {"HIGH", "MEDIUM", "LOW"}:
            severity = "MEDIUM"
        conflicts.append(
            ConflictWarning(
                conflict_type=str(c.get("conflict_type", ""))[:50],
                description=str(c.get("description", ""))[:500],
                severity=severity,
            )
        )
    return tuple(conflicts)


def _parseStructureSuggestion(parsed: dict[str, Any]) -> StructureSuggestion | None:
    """解析结构建议。"""
    ss = parsed.get("structure_suggestions")
    if not ss or not isinstance(ss, dict):
        return None

    claims = []
    for c in ss.get("claims_to_extract", [])[:10]:
        if not isinstance(c, dict) or "text" not in c:
            continue
        claims.append(
            ClaimSuggestion(
                text=str(c.get("text", ""))[:500],
                type=str(c.get("type", "FACT"))[:30],
            )
        )

    return StructureSuggestion(
        suggested_structure=ss.get("suggested_structure", {}),
        claims_to_extract=tuple(claims),
    )


def _parseAnalysis(parsed: dict[str, Any]) -> ImportAnalysis:
    """解析 LLM 分析输出为结构化结果。"""
    dimension, confidence, alternatives, reason = _parseDimension(parsed)

    main_args = parsed.get("main_arguments", {})
    claims = tuple(str(c)[:500] for c in main_args.get("claims", [])[:10])

    return ImportAnalysis(
        dimension=dimension,
        dimension_confidence=confidence,
        dimension_alternatives=alternatives,
        dimension_reason=reason,
        key_entities=_parseEntities(parsed),
        key_concepts=_parseConcepts(parsed),
        main_claims=claims,
        evidence_summary=str(main_args.get("evidence", ""))[:1000],
        evidence_confidence=str(main_args.get("confidence", "medium"))[:20],
        ontology_links=_parseOntologyLinks(parsed),
        conflicts=_parseConflicts(parsed),
        structure_suggestion=_parseStructureSuggestion(parsed),
    )


class ImportAnalyzer:
    """Two-Step CoT Step 1: 导入分析器。

    对知识内容进行深度分析，输出结构化的分析结果，
    为 Step 2（导入执行）提供丰富的元数据。
    """

    async def analyze(
        self,
        invoker: LearningLLMInvoker,
        *,
        title: str,
        content: str,
    ) -> tuple[ImportAnalysis | None, LearningLlmResult]:
        """分析知识内容，返回 (分析结果, 计量结果)。

        - 调用失败 / 输出非 JSON → ``invoker`` 抛 ``LLMUnavailableError``
        - 输出是合法 JSON 但字段不完整 → 返回部分填充的 ImportAnalysis
        """
        safeTitle = neutralizeFence(title)
        safeContent = neutralizeFence(content[:_MAX_CONTENT_CHARS])
        userPrompt = (
            f"<user_content>\n标题：{safeTitle}\n\n正文：\n{safeContent}\n</user_content>"
        )

        parsed, result = await invoker.completeJson(
            systemPrompt=_loadAnalysisPrompt(),
            userPrompt=userPrompt,
            mechanism="ANALYZE",
            purpose="wiki_import_analyze",
        )

        return _parseAnalysis(parsed), result


__all__ = [
    "ImportAnalyzer",
    "ImportAnalysis",
    "EntitySuggestion",
    "ConceptSuggestion",
    "OntologyLinkSuggestion",
    "ConflictWarning",
    "ClaimSuggestion",
    "StructureSuggestion",
]
