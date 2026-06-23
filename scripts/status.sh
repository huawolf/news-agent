#!/usr/bin/env bash
# Daily News - 状态查看
# 显示：timer 下次触发时间、service 上次结果、最近日志

set -euo pipefail

LINES="${1:-15}"

echo "═══ Timer ═══"
systemctl list-timers 'dnews-*' --no-pager || true
echo ""

echo "═══ Service 状态 ═══"
systemctl status dnews-fetch.service --no-pager --lines=0 || true
echo "──────────────────────────────"
systemctl status dnews-push.service --no-pager --lines=0 || true
echo ""

echo "═══ 最近 $LINES 行日志 ═══"
journalctl --namespace=dnews \
    -u dnews-fetch.service \
    -u dnews-push.service \
    -n "$LINES" \
    --no-pager
echo ""
echo "💡 实时跟随：journalctl --namespace=dnews -f"
