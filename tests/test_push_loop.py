#!/usr/bin/env python3
"""测试 push_loop 时间逻辑 - 直接调用 main.py"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import get_timezone, load_config

# 记录调用时间
call_times = []

async def mock_run_push_job(config):
    """模拟 push job"""
    now = datetime.now(get_timezone(config))
    call_times.append(now)
    print(f"\n{'='*40}")
    print(f"📤 Mock Push Job 被调用 | {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*40}\n")

async def main():
    print("🧪 测试 push_loop 时间逻辑")
    print("="*50)

    # 加载配置
    config = load_config()
    tz = get_timezone(config)

    # 动态设置 cron：约 30 秒后 和 60 秒后触发
    # 标准 cron 是5字段（分 时 日 月 周），通过计算实现秒级等待
    now = datetime.now(tz)

    # 计算等待时间：
    # 第一次：约 30 秒后（下一分钟，等待 = 60 - 当前秒数）
    # 第二次：约 60 秒后（下两分钟，等待 = 120 - 当前秒数）
    sec_to_wait_1 = 60 - now.second  # 到下一分钟的剩余秒数
    sec_to_wait_2 = sec_to_wait_1 + 60  # 再加一分钟

    min_1 = (now.minute + 1) % 60
    min_2 = (now.minute + 2) % 60

    cron1 = f"{min_1} {now.hour} * * *"
    cron2 = f"{min_2} {now.hour} * * *"

    config['schedule']['push_cron'] = [cron1, cron2]

    print(f"\n当前时间: {now.strftime('%H:%M:%S')}")
    print(f"测试配置:")
    print(f"  - 第1次推送: {cron1} (约 {sec_to_wait_1}s 后)")
    print(f"  - 第2次推送: {cron2} (约 {sec_to_wait_2}s 后)")
    print()

    # 使用 patch mock run_push_job，设置超时 3 分钟
    from src import main as main_module

    test_task = None
    push_task = None

    async def run_test():
        with patch.object(main_module, 'run_push_job', mock_run_push_job):
            await main_module.push_loop(config)

    async def timeout_guard():
        await asyncio.sleep(180)  # 3分钟超时
        print("\n⏱️ 测试超时")
        if push_task:
            push_task.cancel()

    try:
        # 同时运行 push_loop 和超时守卫
        push_task = asyncio.create_task(run_test())
        timeout_task = asyncio.create_task(timeout_guard())

        # 等待 push_task 完成或超时
        while push_task and not push_task.done():
            if len(call_times) >= 2:
                push_task.cancel()
                break
            await asyncio.sleep(0.1)

        timeout_task.cancel()

    except asyncio.CancelledError:
        pass

    # 验证结果
    print("\n" + "="*50)
    print("📊 测试结果验证")
    print("="*50)

    if len(call_times) >= 2:
        print(f"✅ 成功调用 {len(call_times)} 次")
        for i, t in enumerate(call_times, 1):
            print(f"  第{i}次: {t.strftime('%H:%M:%S')}")

        interval = (call_times[1] - call_times[0]).total_seconds()
        print(f"\n实际间隔: {interval:.1f} 秒")
        if 55 <= interval <= 65:
            print("✅ 间隔正确 (约60秒)")
        else:
            print(f"⚠️ 间隔异常 (期望 ~60秒)")
    else:
        print(f"❌ 只调用了 {len(call_times)} 次，期望 2 次")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 测试已取消")
        sys.exit(130)
