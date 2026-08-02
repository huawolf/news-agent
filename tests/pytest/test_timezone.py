"""时区处理测试"""

import pytest
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from config import get_timezone


class TestTimezoneBasics:
    """测试时区基础功能"""

    def test_get_timezone_positive_hours(self):
        config = {"schedule": {"timezone_hours": 8}}
        tz = get_timezone(config)
        offset = tz.utcoffset(datetime.now())
        assert offset.total_seconds() == 8 * 3600

    def test_get_timezone_negative_hours(self):
        config = {"schedule": {"timezone_hours": -5}}
        tz = get_timezone(config)
        offset = tz.utcoffset(datetime.now())
        assert offset.total_seconds() == -5 * 3600

    def test_get_timezone_zero_hours(self):
        config = {"schedule": {"timezone_hours": 0}}
        tz = get_timezone(config)
        offset = tz.utcoffset(datetime.now())
        assert offset.total_seconds() == 0

    def test_get_timezone_missing_schedule(self):
        config = {}
        tz = get_timezone(config)
        assert tz is not None

    def test_get_timezone_none_config(self):
        tz = get_timezone(None)
        assert tz is not None


class TestTimezoneConversions:
    """测试时区转换"""

    def test_utc_to_local(self):
        config = {"schedule": {"timezone_hours": 8}}
        tz = get_timezone(config)

        utc_time = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        local_time = utc_time.astimezone(tz)

        assert local_time.hour == 18

    def test_cross_day_conversion(self):
        config = {"schedule": {"timezone_hours": 8}}
        tz = get_timezone(config)

        utc_time = datetime(2024, 1, 15, 20, 0, 0, tzinfo=timezone.utc)
        local_time = utc_time.astimezone(tz)

        assert local_time.day == 16

    def test_negative_timezone(self):
        config = {"schedule": {"timezone_hours": -5}}
        tz = get_timezone(config)

        utc_time = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        local_time = utc_time.astimezone(tz)

        assert local_time.hour == 5


class TestTimezoneAwareDatetime:
    """测试带时区的datetime操作"""

    def test_now_in_config_timezone(self):
        config = {"schedule": {"timezone_hours": 8}}
        tz = get_timezone(config)

        now_local = datetime.now(tz)
        assert now_local.tzinfo == tz

    def test_timezone_aware_comparison(self):
        config = {"schedule": {"timezone_hours": 8}}
        tz = get_timezone(config)

        dt1 = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        dt2 = datetime(2024, 1, 15, 18, 0, 0, tzinfo=tz)

        assert dt1 == dt2

    def test_naive_to_aware(self):
        config = {"schedule": {"timezone_hours": 8}}
        tz = get_timezone(config)

        naive = datetime(2024, 1, 15, 10, 0, 0)
        aware = naive.replace(tzinfo=tz)

        assert aware.tzinfo == tz
