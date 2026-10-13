"""RequestDeadline 不可变 dataclass + is_expired / remaining_seconds（P2-1 RED/GREEN）。"""
from __future__ import annotations

import time

import pytest

from app.services.request_deadline import RequestDeadline


def test_is_expired_false_when_within_budget():
    deadline = RequestDeadline(ttl_seconds=10.0)
    assert deadline.isExpired() is False


def test_is_expired_true_after_budget():
    deadline = RequestDeadline(ttl_seconds=0.05)  # 50ms
    time.sleep(0.1)
    assert deadline.isExpired() is True


def test_remaining_seconds_decreases():
    deadline = RequestDeadline(ttl_seconds=10.0)
    r1 = deadline.remainingSeconds()
    time.sleep(0.05)
    r2 = deadline.remainingSeconds()
    assert r2 < r1
    assert r1 > 9.0  # 刚创建剩余接近 ttl
    assert r2 > 0.0


def test_remaining_seconds_clamped_to_zero_when_expired():
    deadline = RequestDeadline(ttl_seconds=0.01)
    time.sleep(0.05)
    assert deadline.remainingSeconds() == 0.0


def test_deadline_is_frozen_dataclass():
    """immutability guard: 不允许修改 ttl_seconds。"""
    deadline = RequestDeadline(ttl_seconds=10.0)
    with pytest.raises(Exception):  # FrozenInstanceError
        deadline.ttl_seconds = 5.0
