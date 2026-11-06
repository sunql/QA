"""回答装配点的接入守卫（源码级锚点）。

这些路径各自需要 LLM / DB / 流式管线才能跑通，模块级单测成本远高于收益；
真正的内容断言由本任务的集成用例覆盖（单步非流式一路）。这里用源码锚点锁住
「四个装配点都调了同一个入口」，防止后续重构时漏接一路。
"""

from __future__ import annotations

from pathlib import Path

_SERVICE_DIR = Path(__file__).resolve().parents[2] / "services"


def _source(name: str) -> str:
    return (_SERVICE_DIR / name).read_text(encoding="utf-8")


def test_single_step_non_stream_appends_hints():
    source = _source("chat_service.py")
    assert "appendFieldHints(" in source
    assert "fieldHintsOf(option)" in source


def test_single_step_stream_emits_hint_token():
    source = _source("chat_stream_query.py")
    assert "composeFieldHintText(" in source
    assert 'EVENT_TOKEN, {"content": hintText}' in source
    # 提示必须在 join 之前进入 answerPieces，否则落库文本与前端所见不一致
    assert source.index("answerPieces.append(hintText)") < source.index('answer = "".join(answerPieces)')


def test_multistep_non_stream_merges_step_hints():
    source = _source("chat_multistep.py")
    assert "hintsFromSteps(completed)" in source
    # 仅出现调用不够：返回值必须写回 agg_content（丢弃返回值同样会落库原文）
    assert "agg_content = appendFieldHints(" in source


def test_multistep_stream_merges_step_hints():
    source = _source("chat_stream_multistep.py")
    assert "hintsFromSteps(completed)" in source
    assert "agg_content = appendFieldHints(" in source
