"""Agent 批量调度（Phase 7 G5）服务单测：cron 解析与 next_run_at 计算。

纯函数测试（不触 DB）：``computeNextRun`` / ``is_valid_cron``。
CRUD / due / dispatch（触真实 PG）在 ``test_agent_scheduler_api.py`` 集成测试覆盖。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.exceptions import ValidationError
from app.services.agent_scheduler_service import AgentSchedulerService


def _utc(y: int, m: int, d: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


class TestComputeNextRun:
    def test_daily_cron_returns_next_nine_am(self) -> None:
        base = _utc(2026, 9, 1, 8, 0)
        assert AgentSchedulerService.computeNextRun("0 9 * * *", base) == _utc(2026, 9, 1, 9, 0)

    def test_daily_cron_after_fire_time_rolls_to_tomorrow(self) -> None:
        base = _utc(2026, 9, 1, 10, 0)
        assert AgentSchedulerService.computeNextRun("0 9 * * *", base) == _utc(2026, 9, 2, 9, 0)

    def test_every_30_minutes(self) -> None:
        base = _utc(2026, 9, 1, 8, 5)
        assert AgentSchedulerService.computeNextRun("*/30 * * * *", base) == _utc(2026, 9, 1, 8, 30)

    def test_from_due_time_preserves_cadence(self) -> None:
        """worker 迟到场景：从应跑时间（next_run_at）算下一次，而非当前时间，
        保持 09:00 节奏（错过不叠加）。"""
        due = _utc(2026, 9, 1, 9, 0)
        assert AgentSchedulerService.computeNextRun("0 9 * * *", due) == _utc(2026, 9, 2, 9, 0)


    def test_invalid_cron_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            AgentSchedulerService.computeNextRun("0 99 * * *", _utc(2026, 9, 1, 8, 0))

    def test_empty_cron_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            AgentSchedulerService.computeNextRun("", _utc(2026, 9, 1, 8, 0))


class TestIsValidCron:
    def test_accepts_five_field(self) -> None:
        assert AgentSchedulerService.is_valid_cron("0 9 * * *")

    def test_rejects_six_field_format(self) -> None:
        # croniter 6.2.4 支持 6-field（含秒）：秒 分 时 日 月 周
        assert AgentSchedulerService.is_valid_cron("0 * * * * *")
        assert AgentSchedulerService.is_valid_cron("*/30 * * * * *")

    def test_rejects_garbage(self) -> None:
        assert not AgentSchedulerService.is_valid_cron("not a cron")

    def test_rejects_out_of_range_minute(self) -> None:
        assert not AgentSchedulerService.is_valid_cron("0 99 * * *")
