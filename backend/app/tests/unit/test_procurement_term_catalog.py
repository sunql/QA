"""采购术语目录（M1-1.5）单测：不变量 + 覆盖度。

钉住四件事
----------
1. **计划覆盖度**：V0.3 计划 1.5 点名的 7 个术语（寻源/询价/报价/定标/到货/收货/
   零库存供应商）**必须一个不少**（少一个 = 计划项没交付）。
2. **口径安全**：无源数据的词条（问卷 C4.2 等）**不许挂 mappedClassName** ——
   挂了类等于告诉 LLM「去查这张表」，而源侧没数据 ⇒ 「0 行 = 真的是 0」假答案。
   反向：有源数据的词条**必须**挂类（不挂 = 口径没落锚点）。
3. **写入口契约**：term/definition/formulaHint/mapped* 的长度约束与
   ``TermDictionaryCreate`` 一致（超限会在 POST 时 422，单测先拦）。
4. **不可变**：目录是决策 SSOT，运行期改一条 = 所有消费者（脚本/测试）跟着变
   ⇒ frozen dataclass 必须真的拒写（变异反证）。

为什么单测（不进集成）能测这些：全是**纯数据断言**，不吃 DB。
"""

from __future__ import annotations

import dataclasses

import pytest

from app.domain.procurement_term_catalog import (
    PROCUREMENT_TERMS,
    V03_TERM_COUNT,
    ProcurementTerm,
    mappedClassNames,
    sourceBackedTerms,
    termNames,
    termSpec,
    unsourcedTerms,
)

# V0.3 计划 1.5 点名的术语（原文：「寻源/询价/报价/定标/到货/收货/零库存供应商等」）
_V03_REQUIRED_TERMS = (
    "寻源",
    "询价",
    "报价",
    "定标",
    "到货",
    "收货",
    "零库存供应商",
)

_TERM_MAX = 100
_DEFINITION_MAX = 2000
_FORMULA_MAX = 2000
_MAPPED_MAX = 100


def test_all_v03_required_terms_are_present() -> None:
    """计划点名的 7 个术语必须都在目录里（一个不少）。"""
    missing = [name for name in _V03_REQUIRED_TERMS if name not in termNames()]
    assert missing == [], f"计划 1.5 点名术语缺失：{missing}"


def test_term_count_is_at_least_the_planned_seven() -> None:
    """目录至少覆盖计划的 7 个（计划原文带「等」，可以更多，不能更少）。"""
    assert len(PROCUREMENT_TERMS) >= V03_TERM_COUNT
    assert len(termNames()) == len(PROCUREMENT_TERMS)


def test_terms_are_unique() -> None:
    """术语名唯一（term_dictionary 有表级唯一约束，重复在写入口直接炸）。"""
    assert len(set(termNames())) == len(termNames())


@pytest.mark.parametrize("spec", PROCUREMENT_TERMS, ids=lambda s: s.term)
def test_field_lengths_match_write_contract(spec: ProcurementTerm) -> None:
    """长度约束与 TermDictionaryCreate 一致（超限会在 POST 时 422）。"""
    assert 1 <= len(spec.term) <= _TERM_MAX
    assert 1 <= len(spec.definition) <= _DEFINITION_MAX
    if spec.formulaHint is not None:
        assert 1 <= len(spec.formulaHint) <= _FORMULA_MAX
    if spec.mappedClassName is not None:
        assert 1 <= len(spec.mappedClassName) <= _MAPPED_MAX
    if spec.mappedPropertyName is not None:
        assert 1 <= len(spec.mappedPropertyName) <= _MAPPED_MAX


@pytest.mark.parametrize("spec", PROCUREMENT_TERMS, ids=lambda s: s.term)
def test_evidence_is_present(spec: ProcurementTerm) -> None:
    """每条口径必须带出处（问卷题号 / 数据标准 / 本体实测），否则不可复核。"""
    assert spec.evidence, f"「{spec.term}」缺 evidence"


@pytest.mark.parametrize("spec", unsourcedTerms(), ids=lambda s: s.term)
def test_unsourced_terms_must_not_map_a_class(spec: ProcurementTerm) -> None:
    """硬规则 1：无源数据 ⇒ 不许挂类/属性（防止「0 行 = 真的是 0」假答案）。"""
    assert spec.hasSourceData is False
    assert spec.mappedClassName is None, f"「{spec.term}」无源数据却挂了类 {spec.mappedClassName}"
    assert spec.mappedPropertyName is None


@pytest.mark.parametrize("spec", sourceBackedTerms(), ids=lambda s: s.term)
def test_source_backed_terms_must_map_a_live_lineage_b_class(spec: ProcurementTerm) -> None:
    """有源数据 ⇒ 必须挂类（不挂 = 口径没落锚点）；类名必须是 Lineage B 词表。"""
    assert spec.hasSourceData is True
    assert spec.mappedClassName is not None
    assert spec.mappedClassName.startswith(("DIM_", "DWD_")), (
        f"「{spec.term}」映射类 {spec.mappedClassName} 不是 Lineage B 词表（DIM_/DWD_）"
    )


def test_mapped_property_requires_mapped_class() -> None:
    """属性脱离类无法校验（写入口同样拒绝：property requires class）。"""
    for spec in PROCUREMENT_TERMS:
        if spec.mappedPropertyName is not None:
            assert spec.mappedClassName is not None, f"「{spec.term}」只给了属性没给类"


def test_mapped_class_names_are_deduplicated_and_stable() -> None:
    """mappedClassNames() 去重且按声明序（集成测试据此种类 ⇒ 顺序必须可复现）。"""
    names = mappedClassNames()
    assert len(names) == len(set(names))
    firstSeen: list[str] = []
    for spec in PROCUREMENT_TERMS:
        if spec.mappedClassName and spec.mappedClassName not in firstSeen:
            firstSeen.append(spec.mappedClassName)
    assert names == tuple(firstSeen)


def test_partition_is_exhaustive() -> None:
    """sourceBacked / unsourced 互补且并集 = 全集（分类漏项 = 规则执行不到）。"""
    assert len(sourceBackedTerms()) + len(unsourcedTerms()) == len(PROCUREMENT_TERMS)
    assert {s.term for s in sourceBackedTerms()}.isdisjoint({s.term for s in unsourcedTerms()})


def test_term_spec_lookups() -> None:
    """termSpec 命中返回词条、未命中返回 None（不抛 —— 调用方决定文案）。"""
    spec = termSpec("零库存供应商")
    assert spec is not None and spec.mappedClassName == "DIM_SUPPLIER"
    assert termSpec("不存在的词") is None


def test_catalog_is_immutable() -> None:
    """变异反证：frozen dataclass 必须真的拒写（SSOT 不许运行期改）。"""
    spec = PROCUREMENT_TERMS[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.definition = "运行期篡改"  # type: ignore[misc]


def test_definitions_state_unconfirmed_scope_where_business_has_not_confirmed() -> None:
    """业务未确认的口径（C3.3 权威价空白）必须在定义里如实写明「未确认」。

    问卷 A5.1：「目前不定，后续通过系统配置」 ⇒ 词条只能写源系统事实 +
    显式标注未确认，不许编造已确认的口径。
    """
    quote = termSpec("报价")
    assert quote is not None
    assert "未" in quote.definition and "确认" in quote.definition
    assert "C3.3" in quote.definition or "C3.3" in (quote.formulaHint or "")
