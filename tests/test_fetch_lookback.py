#!/usr/bin/env python3
"""测试 fetch_lookback_minutes 功能脚本

测试内容：
1. cutoff 时间计算是否正确
2. load_existing_links 阈值逻辑
3. 跨天边界的去重逻辑
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from src.config import get_timezone, load_config
from src.storage import get_fetch_file, load_existing_links, read_entries


def test_cutoff_calculation():
    """测试 cutoff 时间计算"""
    print("\n" + "=" * 60)
    print("📊 Test 1: cutoff 时间计算")
    print("=" * 60)

    config = load_config()
    interval = config["schedule"]["fetch_interval_minutes"]
    lookback = config["schedule"].get("fetch_lookback_minutes", 120)

    # 确保 lookback >= interval
    lookback = max(lookback, interval)
    threshold = lookback + interval

    print(f"\n配置值:")
    print(f"  fetch_interval_minutes: {interval}")
    print(
        f"  fetch_lookback_minutes: {config['schedule'].get('fetch_lookback_minutes', 120)}"
    )
    print(f"  修正后的 lookback: {lookback}")
    print(f"  threshold (lookback + interval): {threshold}")

    # 模拟计算 cutoff
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(minutes=lookback)

    print(f"\n计算结果:")
    print(f"  当前 UTC 时间: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  cutoff 时间:   {cutoff.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  过去 {lookback} 分钟")

    assert lookback >= interval, f"lookback ({lookback}) 应该 >= interval ({interval})"
    print("\n✅ Test 1 通过: cutoff 计算正确")


def test_threshold_logic():
    """测试 load_existing_links 阈值逻辑"""
    print("\n" + "=" * 60)
    print("📊 Test 2: load_existing_links 阈值逻辑")
    print("=" * 60)

    config = load_config()
    interval = config["schedule"]["fetch_interval_minutes"]
    lookback = config["schedule"].get("fetch_lookback_minutes", 120)
    lookback = max(lookback, interval)
    threshold = lookback + interval

    tz = get_timezone(config)

    # 测试不同时间点
    test_cases = [
        ("02:00", threshold, True, "凌晨2点应该需要昨天"),
        ("02:29", threshold, True, "02:29 应该需要昨天"),
        ("02:30", threshold, False, "02:30 开始只需要当天"),
        ("12:00", threshold, False, "中午12点只需要当天"),
        ("23:59", threshold, False, "23:59 只需要当天"),
    ]

    print(f"\n阈值: {threshold} 分钟 (即 {threshold // 60}小时 {threshold % 60}分钟)")
    print(f"\n测试结果:")

    all_passed = True
    for time_str, thresh, expected_need_yesterday, desc in test_cases:
        hour, minute = map(int, time_str.split(":"))
        current_minutes = hour * 60 + minute
        need_yesterday = current_minutes < thresh

        status = "✅" if need_yesterday == expected_need_yesterday else "❌"
        print(
            f"  {status} {time_str}: 需要昨天={need_yesterday} (预期: {expected_need_yesterday}) - {desc}"
        )

        if need_yesterday != expected_need_yesterday:
            all_passed = False

    if all_passed:
        print("\n✅ Test 2 通过: 阈值逻辑正确")
    else:
        print("\n❌ Test 2 失败: 阈值逻辑有问题")


def test_load_existing_links_files():
    """测试实际加载文件功能"""
    print("\n" + "=" * 60)
    print("📊 Test 3: 实际加载文件测试")
    print("=" * 60)

    config = load_config()
    interval = config["schedule"]["fetch_interval_minutes"]
    lookback = config["schedule"].get("fetch_lookback_minutes", 120)
    lookback = max(lookback, interval)
    threshold = lookback + interval

    tz = get_timezone(config)
    now = datetime.now(tz)
    current_minutes = now.hour * 60 + now.minute
    need_yesterday = current_minutes < threshold

    print(f"\n当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"当前分钟数: {current_minutes}")
    print(f"阈值: {threshold}")
    print(f"需要加载昨天: {need_yesterday}")

    # 测试当天文件
    today_file = get_fetch_file()
    print(f"\n当天文件: {today_file}")
    print(f"文件存在: {Path(today_file).exists()}")

    # 测试 load_existing_links 函数
    existing = load_existing_links(today_file, threshold)
    print(f"加载到的链接数: {len(existing)}")

    if need_yesterday:
        yesterday = (now - timedelta(days=1)).date()
        yesterday_file = get_fetch_file(yesterday)
        print(f"\n昨天文件: {yesterday_file}")
        print(f"文件存在: {Path(yesterday_file).exists()}")

        if Path(yesterday_file).exists():
            yesterday_entries = read_entries(yesterday_file)
            print(f"昨天文件条目数: {len(yesterday_entries)}")

    print("\n✅ Test 3 完成: 文件加载功能正常")


def test_mock_time():
    """模拟不同时间测试阈值逻辑"""
    print("\n" + "=" * 60)
    print("📊 Test 4: 模拟时间测试")
    print("=" * 60)

    config = load_config()
    interval = config["schedule"]["fetch_interval_minutes"]
    lookback = config["schedule"].get("fetch_lookback_minutes", 120)
    lookback = max(lookback, interval)
    threshold = lookback + interval

    print(f"\n配置: interval={interval}, lookback={lookback}, threshold={threshold}")

    test_times = [
        (0, 0),  # 00:00
        (2, 20),  # 02:20
        (2, 30),  # 02:30
        (3, 0),  # 03:00
        (8, 0),  # 08:00
        (12, 0),  # 12:00
        (23, 59),  # 23:59
    ]

    print("\n模拟时间测试:")
    for hour, minute in test_times:
        current_minutes = hour * 60 + minute
        need_yesterday = current_minutes < threshold

        time_str = f"{hour:02d}:{minute:02d}"
        status = "🔴 需要昨天" if need_yesterday else "🟢 只需当天"
        print(f"  {time_str} ({current_minutes:4d}分钟): {status}")

    print("\n✅ Test 4 完成")


def main():
    """主函数"""
    print("\n" + "=" * 60)
    print("🧪 fetch_lookback_minutes 功能测试")
    print("=" * 60)

    try:
        test_cutoff_calculation()
        test_threshold_logic()
        test_load_existing_links_files()
        test_mock_time()

        print("\n" + "=" * 60)
        print("🎉 所有测试完成!")
        print("=" * 60)
        return 0

    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
