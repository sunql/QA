"""WikiInjector 单测（wiki-ontology-link Task 4，纯函数 14 例）。

业务覆盖：
  - 评分 = sum(weight × recall_score) 按 (page_id, chunk_id) 分组
  - 去重：同一 chunk 多条 link 合并；page-level 与 chunk-level 是不同 key
  - 过滤：未召回的 ontology 不纳入；recall_score < minRecallScore 丢弃
  - 预算：maxChars 截断尾段；maxChunks 限制数量；负数走 default
  - 渲染：含 meta / 空块 / 字符截断
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.services.wiki_injector import (
    ScoredChunk, ScoredOntology, WikiBudget, WikiInjector,
)


@dataclass
class FakeOntology:
    type: str
    id: int
    recall_score: float


@dataclass
class FakeLink:
    page_id: str
    chunk_id: str | None
    ontology_type: str
    ontology_id: int
    weight: Decimal


def _rec(typ, id, score):
    return FakeOntology(type=typ, id=id, recall_score=score)


def _lnk(page, chunk, typ, id, weight="1.0"):
    return FakeLink(
        page_id=page, chunk_id=chunk, ontology_type=typ,
        ontology_id=id, weight=Decimal(weight),
    )


def _budget(maxChars=2000, maxChunks=5):
    return WikiBudget(maxChars=maxChars, maxChunks=maxChunks)


def test_score_sums_weight_x_recall_per_chunk():
    recalled = [_rec("class", 12, 0.8)]
    links = [_lnk("p001", "c005", "class", 12, "1.0")]
    chunks = {("p001", "c005"): "rule text"}
    out = WikiInjector.collectAndScore(recalled, links, chunks, _budget())
    assert len(out) == 1
    assert out[0].score == pytest.approx(0.8)


def test_score_multi_ontology_same_chunk_sums():
    recalled = [_rec("class", 12, 0.6), _rec("property", 99, 0.4)]
    links = [
        _lnk("p001", "c005", "class", 12, "1.0"),
        _lnk("p001", "c005", "property", 99, "1.0"),
    ]
    chunks = {("p001", "c005"): "rule text"}
    out = WikiInjector.collectAndScore(recalled, links, chunks, _budget())
    assert out[0].score == pytest.approx(1.0)


def test_dedup_same_chunk_kept_once():
    recalled = [_rec("class", 12, 0.8)]
    links = [
        _lnk("p001", "c005", "class", 12, "1.0"),
        _lnk("p001", "c005", "class", 12, "0.5"),  # 应被合并
    ]
    chunks = {("p001", "c005"): "rule text"}
    out = WikiInjector.collectAndScore(recalled, links, chunks, _budget())
    assert len(out) == 1


def test_dedup_page_level_and_chunk_level_different_keys():
    recalled = [_rec("class", 12, 0.8)]
    links = [
        _lnk("p001", None, "class", 12, "1.0"),  # 页面级
        _lnk("p001", "c005", "class", 12, "1.0"),  # 段落级
    ]
    chunks = {("p001", ""): "page text", ("p001", "c005"): "chunk text"}
    out = WikiInjector.collectAndScore(recalled, links, chunks, _budget())
    assert len(out) == 2


def test_filter_unrecalled_ontology():
    recalled = [_rec("class", 12, 0.8)]
    links = [_lnk("p001", "c005", "class", 99, "1.0")]  # id=99 未召回
    chunks = {("p001", "c005"): "rule text"}
    out = WikiInjector.collectAndScore(recalled, links, chunks, _budget())
    assert out == []


def test_max_chars_truncates_tail():
    recalled = [_rec("class", 12, 0.8)]
    links = [
        _lnk("p001", "c001", "class", 12, "1.0"),
        _lnk("p002", "c001", "class", 12, "0.5"),  # score 较低
    ]
    chunks = {
        ("p001", "c001"): "a" * 1500,
        ("p002", "c001"): "b" * 1500,
    }
    out = WikiInjector.collectAndScore(recalled, links, chunks, _budget(maxChars=2000, maxChunks=5))
    # 第一个全保留，第二个被截断到剩余额度
    total = sum(len(c.text) for c in out)
    assert total <= 2000 + 200  # meta overhead 余量
    assert out[0].text == "a" * 1500
    assert "…" in out[1].text


def test_max_chunks_caps_count():
    recalled = [_rec("class", i, 0.5) for i in range(10)]
    links = [_lnk(f"p{i:03d}", "c001", "class", i, "1.0") for i in range(10)]
    chunks = {(f"p{i:03d}", "c001"): f"text{i}" for i in range(10)}
    out = WikiInjector.collectAndScore(recalled, links, chunks, _budget(maxChars=100000, maxChunks=3))
    assert len(out) == 3


def test_negative_budget_uses_default():
    recalled = [_rec("class", 12, 0.8)]
    links = [_lnk("p001", "c001", "class", 12, "1.0")]
    chunks = {("p001", "c001"): "text"}
    out = WikiInjector.collectAndScore(recalled, links, chunks, WikiBudget(maxChars=-1, maxChunks=-1))
    # 走 default 2000 / 5
    assert len(out) == 1


def test_renderer_includes_meta_lines():
    recalled = [_rec("class", 12, 0.8)]
    links = [_lnk("p001", "c005", "class", 12, "1.0")]
    chunks = {("p001", "c005"): "rule text"}
    page_index = {"p001": "收货作业 SOP"}
    scored = WikiInjector.collectAndScore(recalled, links, chunks, _budget())
    block = WikiInjector.renderPromptBlock(scored, 2000, page_index)
    assert "### 业务规则补充" in block
    assert "[wiki:p001:c005]" in block
    assert "DIM_SUPPLIER" in block or "class=12" in block


def test_renderer_omits_block_when_empty():
    block = WikiInjector.renderPromptBlock([], 2000, {})
    assert block == ""


def test_renderer_caps_chars():
    recalled = [_rec("class", 12, 0.8)]
    links = [_lnk("p001", "c001", "class", 12, "1.0")]
    chunks = {("p001", "c001"): "x" * 5000}
    scored = WikiInjector.collectAndScore(recalled, links, chunks, _budget(maxChars=200, maxChunks=5))
    block = WikiInjector.renderPromptBlock(scored, 200, {"p001": "title"})
    assert len(block) <= 300  # 含 meta


def test_empty_pairs_returns_empty():
    out = WikiInjector.collectAndScore([], [], {}, _budget())
    assert out == []


def test_metric_type_is_accepted():
    """metric 必须在 _VALID_TYPES 内 —— 否则 A 档刚修好的链在注入器处又被静默丢弃。

    这是「六处枚举」里最隐蔽的一处：wiki_injector 拦掉之后既无日志也无前端提示，
    表现与本次要修的 A 档缺陷完全一样。
    """
    recalled = [_rec("metric", 8, 0.7)]
    links = [_lnk("p001", None, "metric", 8, "1.0")]
    chunks = {("p001", ""): "指标口径"}
    out = WikiInjector.collectAndScore(recalled, links, chunks, _budget())
    assert len(out) == 1
    assert out[0].applied_to == [("metric", 8)]


def test_unknown_type_is_dropped():
    """反例：非法类型仍被丢弃 —— 别为了放 metric 把闸门整个拆了。"""
    recalled = [_rec("join", 8, 0.7)]
    links = [_lnk("p001", None, "join", 8, "1.0")]
    chunks = {("p001", ""): "x"}
    assert WikiInjector.collectAndScore(recalled, links, chunks, _budget()) == []