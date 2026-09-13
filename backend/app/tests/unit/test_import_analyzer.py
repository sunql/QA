"""ImportAnalyzer 单元测试（Two-Step CoT Step 1）。

测试分析结果的解析逻辑，不依赖真实 LLM 调用。
"""

from __future__ import annotations

import pytest

from app.services.learning.import_analyzer import (
    ImportAnalysis,
    ImportAnalyzer,
    OntologyLinkSuggestion,
    _parseAnalysis,
)


class TestParseAnalysis:
    """测试 _parseAnalysis 对各种输入的解析。"""

    def test_parse_complete_analysis(self):
        """完整分析结果应正确解析为 ImportAnalysis。"""
        parsed = {
            "dimension": {
                "primary": "RULE",
                "confidence": 0.92,
                "alternatives": ["POLICY"],
                "reason": "包含明确的准入阈值条件",
            },
            "key_entities": [
                {
                    "name": "供应商",
                    "type": "业务对象",
                    "role": "central",
                    "ontology_class_suggestion": "supplier",
                }
            ],
            "key_concepts": [
                {
                    "name": "准入资质",
                    "definition": "供应商成为合格供应商前必须满足的条件",
                    "relevance": "是供应商管理的核心环节",
                }
            ],
            "main_arguments": {
                "claims": ["注册资本必须大于等于1000万", "必须具备ISO9001认证"],
                "evidence": "第三章第2节明确规定",
                "confidence": "high",
            },
            "ontology_links": [
                {
                    "target_type": "ONTOLOGY_CLASS",
                    "target_id": "supplier",
                    "relation_type": "APPLIES_TO",
                    "confidence": 0.95,
                    "reasoning": "内容明确描述供应商准入规则",
                }
            ],
            "conflicts": [],
            "structure_suggestions": {
                "suggested_structure": {
                    "rule_kind": "THRESHOLD",
                    "conditions": [
                        {"field": "registered_capital", "operator": ">=", "value": 10000000}
                    ],
                },
                "claims_to_extract": [
                    {"text": "供应商注册资本必须大于等于1000万", "type": "RULE"},
                    {"text": "供应商必须具备ISO9001认证", "type": "RULE"},
                ],
            },
        }

        result = _parseAnalysis(parsed)

        assert isinstance(result, ImportAnalysis)
        assert result.dimension == "RULE"
        assert result.dimension_confidence == 0.92
        assert result.dimension_alternatives == ("POLICY",)
        assert result.dimension_reason == "包含明确的准入阈值条件"

        assert len(result.key_entities) == 1
        assert result.key_entities[0].name == "供应商"
        assert result.key_entities[0].ontology_class_suggestion == "supplier"

        assert len(result.key_concepts) == 1
        assert result.key_concepts[0].name == "准入资质"

        assert len(result.main_claims) == 2
        assert "注册资本必须大于等于1000万" in result.main_claims

        assert len(result.ontology_links) == 1
        assert result.ontology_links[0].target_type == "ONTOLOGY_CLASS"
        assert result.ontology_links[0].target_id == "supplier"
        assert result.ontology_links[0].relation_type == "APPLIES_TO"
        assert result.ontology_links[0].confidence == 0.95

        assert len(result.conflicts) == 0

        assert result.structure_suggestion is not None
        assert result.structure_suggestion.suggested_structure["rule_kind"] == "THRESHOLD"
        assert len(result.structure_suggestion.claims_to_extract) == 2

    def test_parse_minimal_analysis(self):
        """最小分析结果（只有 dimension）应正确解析。"""
        parsed = {
            "dimension": {
                "primary": "CONCEPT",
                "confidence": 0.8,
            }
        }

        result = _parseAnalysis(parsed)

        assert result.dimension == "CONCEPT"
        assert result.dimension_confidence == 0.8
        assert result.dimension_alternatives == ()
        assert result.key_entities == ()
        assert result.key_concepts == ()
        assert result.ontology_links == ()
        assert result.conflicts == ()
        assert result.structure_suggestion is None

    def test_parse_invalid_dimension_fallback(self):
        """无效维度应回退到 CONCEPT。"""
        parsed = {
            "dimension": {
                "primary": "INVALID_DIM",
                "confidence": 0.9,
            }
        }

        result = _parseAnalysis(parsed)

        assert result.dimension == "CONCEPT"  # 回退到默认值
        assert result.dimension_confidence == 0.9

    def test_parse_filters_invalid_ontology_link_types(self):
        """无效的本体关联类型应被过滤。"""
        parsed = {
            "dimension": {"primary": "RULE", "confidence": 0.9},
            "ontology_links": [
                {
                    "target_type": "INVALID_TYPE",
                    "target_id": "test",
                    "relation_type": "APPLIES_TO",
                    "confidence": 0.9,
                    "reasoning": "test",
                },
                {
                    "target_type": "ONTOLOGY_CLASS",
                    "target_id": "supplier",
                    "relation_type": "INVALID_RELATION",
                    "confidence": 0.9,
                    "reasoning": "test",
                },
                {
                    "target_type": "ONTOLOGY_CLASS",
                    "target_id": "supplier",
                    "relation_type": "APPLIES_TO",
                    "confidence": 0.95,
                    "reasoning": "valid link",
                },
            ],
        }

        result = _parseAnalysis(parsed)

        # 只有第三个是有效的
        assert len(result.ontology_links) == 1
        assert result.ontology_links[0].target_id == "supplier"

    def test_parse_limits_list_sizes(self):
        """列表字段应有大小限制，防止恶意输入。"""
        parsed = {
            "dimension": {"primary": "RULE", "confidence": 0.9},
            "key_entities": [{"name": f"entity_{i}", "type": "test", "role": "central"} for i in range(20)],
            "key_concepts": [{"name": f"concept_{i}", "definition": "d", "relevance": "r"} for i in range(20)],
            "ontology_links": [
                {
                    "target_type": "ONTOLOGY_CLASS",
                    "target_id": f"class_{i}",
                    "relation_type": "APPLIES_TO",
                    "confidence": 0.9,
                    "reasoning": "test",
                }
                for i in range(10)
            ],
        }

        result = _parseAnalysis(parsed)

        assert len(result.key_entities) == 10  # 限制为 10
        assert len(result.key_concepts) == 10  # 限制为 10
        assert len(result.ontology_links) == 5  # 限制为 5

    def test_to_dict_serialization(self):
        """ImportAnalysis.toDict() 应返回可 JSON 序列化的字典。"""
        analysis = ImportAnalysis(
            dimension="RULE",
            dimension_confidence=0.92,
            dimension_alternatives=("POLICY",),
            dimension_reason="test reason",
            key_entities=(),
            key_concepts=(),
            main_claims=("claim1", "claim2"),
            evidence_summary="evidence",
            evidence_confidence="high",
            ontology_links=(
                OntologyLinkSuggestion(
                    target_type="ONTOLOGY_CLASS",
                    target_id="supplier",
                    relation_type="APPLIES_TO",
                    confidence=0.95,
                    reasoning="test",
                ),
            ),
            conflicts=(),
            structure_suggestion=None,
        )

        result = analysis.toDict()

        assert isinstance(result, dict)
        assert result["dimension"]["primary"] == "RULE"
        assert result["dimension"]["confidence"] == 0.92
        assert result["main_arguments"]["claims"] == ["claim1", "claim2"]
        assert len(result["ontology_links"]) == 1
        assert result["ontology_links"][0]["target_id"] == "supplier"


class TestImportAnalyzer:
    """测试 ImportAnalyzer 类的接口。"""

    def test_analyzer_initialization(self):
        """分析器应能正常初始化。"""
        analyzer = ImportAnalyzer()
        assert analyzer is not None


__all__ = ["TestParseAnalysis", "TestImportAnalyzer"]
