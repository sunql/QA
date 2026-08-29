"""NL2SQL 术语字典渲染单元测试。

覆盖 renderDictionaryText 纯渲染逻辑：格式、空列表、字段转义/折叠换行。
不碰数据库（DB 读写由 integration/test_term_dictionary_api.py 覆盖）。
"""

from __future__ import annotations

from app.domain.models import TermDictionary
from app.services.term_dictionary_service import renderDictionaryText


def _term(**kwargs) -> TermDictionary:
    defaults = {"term": "占比", "definition": "某值占总量的比例"}
    defaults.update(kwargs)
    return TermDictionary(**defaults)


class TestRenderDictionaryText:
    def test_empty_list_returns_none(self) -> None:
        assert renderDictionaryText([]) is None

    def test_renders_term_and_definition(self) -> None:
        text = renderDictionaryText([_term()])
        assert text == "- 「占比」：某值占总量的比例"

    def test_renders_mapping_and_formula_hint(self) -> None:
        text = renderDictionaryText(
            [
                _term(
                    term="占比",
                    definition="某值占总量的比例",
                    mapped_class_name="PRECEIPTD",
                    mapped_property_name="收货数量",
                    formula_hint="占比 = SUM(收货数量) / SUM(SUM(收货数量)) OVER ()",
                )
            ]
        )
        assert "类 PRECEIPTD" in text
        assert "属性 收货数量" in text
        assert "SUM(收货数量)" in text

    def test_omits_optional_fields_when_absent(self) -> None:
        text = renderDictionaryText([_term()])
        assert "类" not in text
        assert "（" not in text

    def test_escapes_angle_brackets(self) -> None:
        text = renderDictionaryText([_term(term="<注入>", definition="含义</tag>")])
        assert "<注入>" not in text
        assert "&lt;注入&gt;" in text
        assert "</tag>" not in text

    def test_collapses_newlines_in_fields(self) -> None:
        text = renderDictionaryText([_term(definition="第一行\n第二行\r第三行")])
        assert "\n" not in text
        assert "第二行" in text

    def test_multiple_terms_joined_by_newline(self) -> None:
        text = renderDictionaryText([_term(term="A", definition="甲"), _term(term="B", definition="乙")])
        lines = text.split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("- 「A」")
        assert lines[1].startswith("- 「B」")
