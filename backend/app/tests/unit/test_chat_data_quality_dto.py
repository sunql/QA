"""Phase 1.4 DataQualityBadge + ChatResponse.data_quality DTO 单测。

目标：
- DataQualityBadge 字段命名/类型 roundtrip 正确（含 camelCase alias）
- evaluated=False 与 evaluated=True 两条路径都能构造
- ChatResponse.data_quality 字段可选，向后兼容旧调用方（None 默认）
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.schemas import ChatResponse, DataQualityBadge


class TestDataQualityBadge:
    def test_evaluated_true_roundtrip(self):
        """已评估的 badge：所有分数字段 + 时间戳有值，camelCase alias 正常。"""
        badge = DataQualityBadge(
            target_table="PORDER",
            overall_score=Decimal("76.84"),
            evaluated_at=datetime(2026, 8, 30, 2, 53, 54, tzinfo=timezone.utc),
            rules_count=5,
            evaluated=True,
        )
        # snake_case 字段访问
        assert badge.target_table == "PORDER"
        assert badge.overall_score == Decimal("76.84")
        assert badge.rules_count == 5
        assert badge.evaluated is True
        # camelCase JSON 序列化（model_dump_json 走 FastAPI 同一条 encoder 路径：
        # Decimal → str、datetime → ISO8601，与线上 API 行为一致）
        dumped = badge.model_dump(by_alias=True, mode="json")
        assert dumped["targetTable"] == "PORDER"
        assert dumped["overallScore"] == "76.84"
        assert dumped["evaluatedAt"] is not None
        assert dumped["rulesCount"] == 5
        assert dumped["evaluated"] is True

    def test_evaluated_false_roundtrip(self):
        """未评估的 badge：分数/时间戳/规则数全为 None，前端用 evaluated 区分。"""
        badge = DataQualityBadge(
            target_table="NEWTABLE",
            overall_score=None,
            evaluated_at=None,
            rules_count=None,
            evaluated=False,
        )
        assert badge.target_table == "NEWTABLE"
        assert badge.overall_score is None
        assert badge.evaluated_at is None
        assert badge.rules_count is None
        assert badge.evaluated is False
        # JSON 不暴露 internal 类型（无 Decimal/DateTime 字面量）
        dumped = badge.model_dump(by_alias=True)
        assert dumped == {
            "targetTable": "NEWTABLE",
            "overallScore": None,
            "evaluatedAt": None,
            "rulesCount": None,
            "evaluated": False,
        }

    def test_evaluated_required(self):
        """evaluated 字段是必填——前端用它区分「未评估」与「0 分」。"""
        with pytest.raises(ValidationError) as exc_info:
            DataQualityBadge(target_table="X")  # type: ignore[call-arg]
        assert "evaluated" in str(exc_info.value)

    def test_target_table_required(self):
        """target_table 必填（与 QueryPlan.selectedClasses 对齐）。"""
        with pytest.raises(ValidationError):
            DataQualityBadge(evaluated=False)  # type: ignore[call-arg]


class TestChatResponseDataQuality:
    def test_data_quality_default_none_backward_compat(self):
        """不传 data_quality 字段时默认 None —— 旧客户端不受影响。"""
        resp = ChatResponse(answer="hi", intent="chitchat")
        assert resp.data_quality is None
        # JSON 也应是 null（不是缺字段）
        dumped = resp.model_dump(by_alias=True)
        assert dumped["dataQuality"] is None

    def test_data_quality_with_badges(self):
        """带 badges 时顺序保留，每张表一个。"""
        badges = [
            DataQualityBadge(
                target_table="PORDER",
                overall_score=Decimal("76.84"),
                evaluated_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
                rules_count=5,
                evaluated=True,
            ),
            DataQualityBadge(
                target_table="BPSUPPLIER",
                overall_score=Decimal("95.00"),
                evaluated_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
                rules_count=2,
                evaluated=True,
            ),
        ]
        resp = ChatResponse(
            answer="查到了",
            intent="query",
            sql="SELECT 1",
            data_quality=badges,
        )
        assert resp.data_quality is not None
        assert len(resp.data_quality) == 2
        assert resp.data_quality[0].target_table == "PORDER"
        assert resp.data_quality[1].target_table == "BPSUPPLIER"
        # JSON 序列化字段名为 dataQuality
        dumped = resp.model_dump(by_alias=True)
        assert "dataQuality" in dumped
        assert len(dumped["dataQuality"]) == 2
        assert dumped["dataQuality"][0]["targetTable"] == "PORDER"

    def test_data_quality_with_mix_evaluated_states(self):
        """混合：已评估 + 未评估 同时存在——典型多表场景。"""
        badges = [
            DataQualityBadge(
                target_table="PORDER",
                overall_score=Decimal("76.84"),
                evaluated_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
                rules_count=5,
                evaluated=True,
            ),
            DataQualityBadge(
                target_table="NOT_EVALUATED_TABLE",
                overall_score=None,
                evaluated_at=None,
                rules_count=None,
                evaluated=False,
            ),
        ]
        resp = ChatResponse(answer="x", intent="query", data_quality=badges)
        assert resp.data_quality is not None
        assert resp.data_quality[1].evaluated is False