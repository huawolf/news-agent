#!/usr/bin/env bash
# Daily News - 卸载系统服务（不删数据）
set -euo pipefail

echo "🗑  Daily News - 卸载"
echo ""

if ! command -v systemctl >/dev/null 2>&1; then
    echo "❌ 系统未检测到 systemctl"
    exit 1
fi

echo "🛑 停止并禁用 timer..."
sudo systemctl disable --now dnews-fetch.timer dnews-push.timer 2>/dev/null || true

echo "🧹 清理单元文件..."
sudo rm -f /etc/systemd/system/dnews-fetch.service \
           /etc/systemd/system/dnews-fetch.timer \
           /etc/systemd/system/dnews-push.service \
           /etc/systemd/system/dnews-push.timer

echo "🧹 清理日志保留策略 drop-in..."
sudo rm -f /etc/systemd/journald@dnews.conf.d/retention.conf
sudo rmdir /etc/systemd/journald@dnews.conf.d 2>/dev/null || true

echo "🧹 清理 daily-news 包装脚本..."
sudo rm -f /usr/local/bin/daily-news

sudo systemctl daemon-reload
sudo systemctl stop systemd-journald@dnews.service 2>/dev/null || true

echo ""
echo "✅ 已卸载（news-data/ 与项目代码未删除）"
echo ""
echo "如需清理历史日志：sudo journalctl --namespace=dnews --vacuum-time=1s"
