"""SSE 编码器序列化保真度。

RED: 现 json.dumps(default=str) 不挡 NaN/Infinity，且 Decimal/UUID/date 被静默字符串化。
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.services.stream_events import StreamEvent


def test_sse_payload_parses_as_standard_json_with_infinity():
    """Infinity 进 SSE → JSON parse 不抛错（非 NaN 字面量）。"""
    event = StreamEvent("data", {"value": float("inf")})
    text = event.toSse()
    # 提取 data 行
    data_line = next(line for line in text.split("\n") if line.startswith("data:"))
    payload = data_line.removeprefix("data: ")
    parsed = json.loads(payload)  # 不抛错（关键反向断言）
    # 同时数据语义保留为 non_finite 标记
    assert parsed["value"] == {"__non_finite__": "infinity"}


def test_sse_payload_parses_with_nan():
    event = StreamEvent("data", {"value": float("nan")})
    text = event.toSse()
    data_line = next(line for line in text.split("\n") if line.startswith("data:"))
    payload = data_line.removeprefix("data: ")
    parsed = json.loads(payload)
    assert parsed["value"] == {"__non_finite__": "nan"}


def test_sse_payload_preserves_decimal_as_string_with_precision():
    """Decimal → str（保留精度，不被 default=str 之外的 float 优化）。"""
    event = StreamEvent("data", {"amount": Decimal("1234567890.123456789")})
    text = event.toSse()
    data_line = next(line for line in text.split("\n") if line.startswith("data:"))
    payload = data_line.removeprefix("data: ")
    parsed = json.loads(payload)
    assert parsed["amount"] == "1234567890.123456789"


def test_sse_payload_preserves_uuid_and_date_iso():
    uid = uuid.uuid4()
    d = date(2025, 1, 15)
    event = StreamEvent("data", {"uid": uid, "date": d})
    text = event.toSse()
    data_line = next(line for line in text.split("\n") if line.startswith("data:"))
    payload = data_line.removeprefix("data: ")
    parsed = json.loads(payload)
    assert parsed["uid"] == str(uid)
    assert parsed["date"] == "2025-01-15"


def test_sse_payload_preserves_datetime_with_tz():
    dt = datetime(2025, 1, 15, 12, 30, tzinfo=timezone.utc)
    event = StreamEvent("data", {"ts": dt})
    text = event.toSse()
    data_line = next(line for line in text.split("\n") if line.startswith("data:"))
    payload = data_line.removeprefix("data: ")
    parsed = json.loads(payload)
    # Python 3.14 isoformat() uses space for naive datetime, T for aware datetime
    assert parsed["ts"] == "2025-01-15T12:30:00+00:00"


def test_sse_payload_deep_list_does_not_recurse():
    """H2: 200 层 list 不抛 RecursionError — P2-1 security review 落地。

    LLM 注入可产生深层嵌套 payload；_jsonSafe 必须有 _JSON_SAFE_MAX_DEPTH 截断。
    """
    payload = []
    inner = payload
    for _ in range(200):
        new = []
        inner.append(new)
        inner = new
    # 把 sentinel 放在最里层；截断后最里层字符串应能找到（不一定；看截断位置）
    inner.append("__SENTINEL__")
    event = StreamEvent("data", {"deep": payload})
    text = event.toSse()  # 不抛 RecursionError
    assert text  # 序列化成功
    data_line = next(line for line in text.split("\n") if line.startswith("data:"))
    payload_json = data_line.removeprefix("data: ")
    parsed = json.loads(payload_json)
    # 顶层 deep 是 list；深层某处应被截断为字符串
    assert isinstance(parsed["deep"], list)
    # 反向断言：被截断位置有 "<truncated:" 标记
    def _find_truncated(node):
        if isinstance(node, str) and node.startswith("<truncated:"):
            return True
        if isinstance(node, list):
            return any(_find_truncated(x) for x in node)
        if isinstance(node, dict):
            return any(_find_truncated(v) for v in node.values())
        return False
    assert _find_truncated(parsed["deep"]), "未发现 _JSON_SAFE_MAX_DEPTH 截断标记"


def test_sse_payload_deep_dict_does_not_recurse():
    """H2: dict 嵌套同样有深度上限。"""
    payload: dict = {}
    inner = payload
    for i in range(200):
        new = {}
        inner[f"k{i}"] = new
        inner = new
    inner["leaf"] = "value"
    event = StreamEvent("data", {"deep": payload})
    text = event.toSse()
    assert text
    data_line = next(line for line in text.split("\n") if line.startswith("data:"))
    parsed = json.loads(data_line.removeprefix("data: "))
    assert isinstance(parsed["deep"], dict)
