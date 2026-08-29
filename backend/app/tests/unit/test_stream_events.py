"""SSE 事件序列化单元测试（5.6）。

覆盖：
- toSse 帧格式（event + data JSON + 空行分隔）
- 中文内容不转义
- Decimal / 时间值经 default=str 安全序列化
"""

from __future__ import annotations

from decimal import Decimal

from app.services.stream_events import StreamEvent


class TestToSse:
    def test_formats_event_and_json_data(self) -> None:
        frame = StreamEvent(event="token", data={"content": "查"}).toSse()
        assert frame == 'event: token\ndata: {"content": "查"}\n\n'

    def test_multiple_fields_in_single_frame(self) -> None:
        frame = StreamEvent(
            event="done", data={"tokensUsed": 45, "cost": 0.0001}
        ).toSse()
        assert frame.startswith("event: done\ndata: ")
        assert '"tokensUsed": 45' in frame
        assert '"cost": 0.0001' in frame
        assert frame.endswith("\n\n")

    def test_serializes_decimal_via_default_str(self) -> None:
        frame = StreamEvent(event="done", data={"cost": Decimal("0.0012")}).toSse()
        assert '"cost": "0.0012"' in frame
