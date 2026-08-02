"""测试早报判定:push_cron 最近最早匹配"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.main import is_morning_push


TZ = timezone(timedelta(hours=8))


def _cfg(push_cron):
    return {"schedule": {"push_cron": push_cron, "timezone_hours": 8}}


def test_returns_false_when_push_cron_empty():
    assert is_morning_push(datetime(2026, 5, 17, 8, 0, tzinfo=TZ), {"schedule": {}}) is False
    assert is_morning_push(datetime(2026, 5, 17, 8, 0, tzinfo=TZ), _cfg([])) is False


def test_single_cron_always_morning():
    """push_cron 只有一项时,任何触发(含手动 / 非整点)都视为早报"""
    cfg = _cfg(["0 8 * * *"])
    assert is_morning_push(datetime(2026, 5, 17, 8, 0, tzinfo=TZ), cfg) is True
    assert is_morning_push(datetime(2026, 5, 17, 12, 30, tzinfo=TZ), cfg) is True
    assert is_morning_push(datetime(2026, 5, 17, 23, 59, tzinfo=TZ), cfg) is True


def test_earliest_cron_match_is_morning():
    """触发时刻离最早 cron 最近 → 早报"""
    cfg = _cfg(["0 8 * * *", "0 17 * * *"])
    assert is_morning_push(datetime(2026, 5, 17, 8, 0, tzinfo=TZ), cfg) is True
    assert is_morning_push(datetime(2026, 5, 17, 8, 30, tzinfo=TZ), cfg) is True
    assert is_morning_push(datetime(2026, 5, 17, 6, 0, tzinfo=TZ), cfg) is True


def test_later_cron_match_not_morning():
    """触发时刻离非最早 cron 最近 → 默认"""
    cfg = _cfg(["0 8 * * *", "0 17 * * *"])
    assert is_morning_push(datetime(2026, 5, 17, 17, 0, tzinfo=TZ), cfg) is False
    assert is_morning_push(datetime(2026, 5, 17, 16, 30, tzinfo=TZ), cfg) is False
    assert is_morning_push(datetime(2026, 5, 17, 22, 0, tzinfo=TZ), cfg) is False


def test_drift_tolerance_via_closest_match():
    """无显式容差,但「最近匹配」自动吸附小幅漂移 (08:01 仍归 08:00)"""
    cfg = _cfg(["0 8 * * *", "0 17 * * *"])
    assert is_morning_push(datetime(2026, 5, 17, 8, 1, tzinfo=TZ), cfg) is True
    assert is_morning_push(datetime(2026, 5, 17, 17, 1, tzinfo=TZ), cfg) is False


def test_three_crons_only_earliest_is_morning():
    """三条 cron 时,只有最早那条对应的触发是早报"""
    cfg = _cfg(["0 8 * * *", "0 12 * * *", "0 20 * * *"])
    assert is_morning_push(datetime(2026, 5, 17, 8, 0, tzinfo=TZ), cfg) is True
    assert is_morning_push(datetime(2026, 5, 17, 12, 0, tzinfo=TZ), cfg) is False
    assert is_morning_push(datetime(2026, 5, 17, 20, 0, tzinfo=TZ), cfg) is False
