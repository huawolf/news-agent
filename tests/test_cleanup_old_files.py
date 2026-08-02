#!/usr/bin/env python3
"""测试 cleanup_old_files 函数

功能：
1. 在 tests/cleanup_test_data 文件夹内创建不同日期的测试文件
2. 运行 cleanup_old_files
3. 显示清理结果
"""

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.storage import cleanup_old_files


def create_test_files():
    """创建测试文件"""
    test_dir = Path("tests/cleanup_test_data")
    test_dir.mkdir(parents=True, exist_ok=True)

    today = date.today()
    created_files = []

    # 创建不同日期的文件
    file_specs = [
        # (文件名模式, 日期偏移, 文件类型)
        ("fetch", -10, "json"),  # 10天前 - 应该删除
        ("fetch", -8, "json"),  # 8天前 - 应该删除
        ("fetch", -7, "json"),  # 7天前 - 应该删除
        ("fetch", -6, "json"),  # 6天前 - 应该保留
        ("fetch", -3, "json"),  # 3天前 - 应该保留
        ("fetch", -1, "json"),  # 1天前 - 应该保留
        ("fetch", 0, "json"),  # 今天 - 应该保留
        ("push", -10, "md"),  # 10天前 - 应该删除
        ("push", -7, "md"),  # 7天前 - 应该删除
        ("push", -5, "md"),  # 5天前 - 应该保留
        ("push", -2, "md"),  # 2天前 - 应该保留
        ("push", 0, "md"),  # 今天 - 应该保留
        ("notify", -9, "md"),  # 9天前 - 应该删除
        ("notify", -6, "md"),  # 6天前 - 应该保留
        ("notify", -1, "md"),  # 1天前 - 应该保留
        ("notify", 0, "md"),  # 今天 - 应该保留
    ]

    print(f"\n📅 今天是: {today}")
    print(f"   cutoff: {today - timedelta(days=7)} (7天前)")
    print(
        f"   将删除 < {today - timedelta(days=6)} (< {(today - timedelta(days=6)).strftime('%m-%d')}) 的文件"
    )
    print(
        f"   保留 >= {today - timedelta(days=6)} (>= {(today - timedelta(days=6)).strftime('%m-%d')}) 的文件"
    )

    print(f"\n📂 创建测试文件到: {test_dir}")
    print("-" * 50)

    for prefix, offset, ext in file_specs:
        file_date = today + timedelta(days=offset)

        if prefix == "push":
            # push 文件带时间戳
            filename = f"push-{file_date.isoformat()}-08-00-00.{ext}"
        else:
            filename = f"{prefix}-{file_date.isoformat()}.{ext}"

        filepath = test_dir / filename

        # 创建文件并写入内容
        with open(filepath, "w") as f:
            f.write(f"测试文件 - {filename}")

        status = "🗑️ 将删除" if offset <= -7 else "✅ 将保留"
        print(f"   {status}: {filename}")
        created_files.append(filename)

    print("-" * 50)
    print(f"✅ 创建了 {len(created_files)} 个测试文件")

    return test_dir


def list_files_after_cleanup(test_dir: Path):
    """显示清理后的文件"""
    print(f"\n📂 清理后的文件列表:")
    print("-" * 50)

    files = sorted(test_dir.glob("*"))
    if not files:
        print("   (空目录)")
    else:
        for f in files:
            print(f"   ✅ {f.name}")

    print("-" * 50)
    print(f"   共 {len(files)} 个文件")


def main():
    """主函数"""
    print("=" * 60)
    print("🧪 cleanup_old_files 测试")
    print("=" * 60)

    # 1. 创建测试文件
    test_dir = create_test_files()

    # 2. 列出清理前的文件
    print(f"\n📂 清理前的文件列表:")
    print("-" * 50)
    for f in sorted(test_dir.glob("*")):
        print(f"   {f.name}")
    print("-" * 50)
    print(f"   共 {len(list(test_dir.glob('*')))} 个文件")

    # 3. 运行清理
    print("\n🚀 运行 cleanup_old_files...")
    cleanup_old_files(days=7, data_dir=str(test_dir))

    # 4. 列出清理后的文件
    list_files_after_cleanup(test_dir)

    print("\n" + "=" * 60)
    print("🎉 测试完成!")
    print("=" * 60)

    # 自动清理测试目录
    import shutil

    shutil.rmtree(test_dir)
    print(f"✅ 已删除测试目录: {test_dir}")


if __name__ == "__main__":
    main()
