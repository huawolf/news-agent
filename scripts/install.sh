#!/usr/bin/env bash
# Daily News - 系统服务一键安装
# 用法：./scripts/install.sh
# 不要用 sudo 直接调用本脚本；脚本会在需要时自行 sudo 提权。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ─── 用户检测 ────────────────────────────────────────────────
if [[ $EUID -eq 0 ]]; then
    if [[ -z "${SUDO_USER:-}" ]]; then
        echo "❌ 请勿直接以 root 用户运行本脚本。请以普通用户运行，或以 sudo 方式运行：sudo ./scripts/install.sh"
        exit 1
    fi
    RUN_USER="$SUDO_USER"
else
    RUN_USER="$USER"
fi
RUN_USER_HOME="$(eval echo "~$RUN_USER")"
RUN_GROUP="$(id -gn "$RUN_USER")"

# ─── uv 检测 ─────────────────────────────────────────────────
UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" ]]; then
    # 尝试在目标普通用户的家目录下寻找 uv
    if [[ -x "$RUN_USER_HOME/.local/bin/uv" ]]; then
        UV_BIN="$RUN_USER_HOME/.local/bin/uv"
    elif [[ -x "$RUN_USER_HOME/.cargo/bin/uv" ]]; then
        UV_BIN="$RUN_USER_HOME/.cargo/bin/uv"
    fi
fi

if [[ -z "$UV_BIN" ]]; then
    echo "❌ 找不到 uv 命令，请先安装：https://docs.astral.sh/uv/"
    exit 1
fi

# ─── 执行 uv 的辅助函数（保持以普通用户身份和权限运行 uv） ──────
run_uv() {
    if [[ $EUID -eq 0 ]]; then
        # 如果当前是 root，则使用 sudo -u 普通用户来执行 uv，并把普通用户的 local bin 加入 PATH
        sudo -u "$RUN_USER" env PATH="$RUN_USER_HOME/.local/bin:$PATH" "$UV_BIN" "$@"
    else
        "$UV_BIN" "$@"
    fi
}

# ─── systemd 检测 ────────────────────────────────────────────
if ! command -v systemctl >/dev/null 2>&1; then
    echo "❌ 系统未检测到 systemctl（非 systemd 系统），本脚本仅支持 systemd Linux"
    exit 1
fi

cd "$PROJECT_DIR"

echo "🚀 Daily News - 系统服务安装"
echo "────────────────────────────────────────"
echo "  项目目录: $PROJECT_DIR"
echo "  运行身份: $RUN_USER:$RUN_GROUP"
echo "  uv 路径:  $UV_BIN"
echo "────────────────────────────────────────"
echo ""

# 提前提权一次，后续 sudo 命令直接复用凭据缓存
if [[ $EUID -ne 0 ]]; then
    echo "🔐 需要 sudo 权限来写入 /etc/systemd/system/"
    sudo -v
    echo ""
fi

# ─── [1/6] 同步依赖 ──────────────────────────────────────────
echo "📦 [1/6] 同步依赖 (uv sync)..."
run_uv sync
echo "✓ 依赖已就绪"
echo ""

# ─── [2/6] 验证 .env ────────────────────────────────────────
echo "🔐 [2/6] 验证 .env..."
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
    echo "❌ .env 不存在，请先复制 .env.example 并填入 API Key"
    exit 1
fi
echo "✓ .env 存在"
echo ""

# ─── [3/6] LLM 健康检查 ─────────────────────────────────────
echo "🔍 [3/6] 校验 LLM 接口（仅在此处校验，运行时不再校验）..."
if ! run_uv run python -m src.main check; then
    echo ""
    echo "❌ LLM 校验失败，已中止安装"
    echo "请检查 config.json 中的 llm.baseUrl/model 以及 .env 中的 API Key"
    exit 1
fi
echo ""

# ─── [4/6] 生成单元文件 ─────────────────────────────────────
echo "📝 [4/6] 渲染 systemd 单元模板..."
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

run_uv run python "$PROJECT_DIR/scripts/_gen_units.py" \
    --project-dir "$PROJECT_DIR" \
    --user "$RUN_USER" \
    --group "$RUN_GROUP" \
    --uv-bin "$UV_BIN" \
    --output-dir "$STAGE_DIR"

# 同时渲染 daily-news 包装脚本（/usr/local/bin/daily-news）
sed "s|{{PROJECT_DIR}}|$PROJECT_DIR|g" \
    "$PROJECT_DIR/scripts/daily-news.tmpl" > "$STAGE_DIR/daily-news"
echo "  ✓ daily-news"
echo ""

# ─── [5/6] 安装到 systemd ───────────────────────────────────
echo "📥 [5/6] 安装到系统..."
sudo install -m 644 "$STAGE_DIR/dnews-fetch.service" /etc/systemd/system/
sudo install -m 644 "$STAGE_DIR/dnews-fetch.timer"   /etc/systemd/system/
sudo install -m 644 "$STAGE_DIR/dnews-push.service"  /etc/systemd/system/
sudo install -m 644 "$STAGE_DIR/dnews-push.timer"    /etc/systemd/system/

# 日志保留策略（journald 命名空间 drop-in）
sudo mkdir -p /etc/systemd/journald@dnews.conf.d
sudo install -m 644 "$STAGE_DIR/journald-dnews.conf" \
    /etc/systemd/journald@dnews.conf.d/retention.conf

# daily-news 包装脚本到系统 PATH
sudo install -m 755 "$STAGE_DIR/daily-news" /usr/local/bin/daily-news

sudo systemctl daemon-reload

# 命名空间 journald 实例首次会在 fetch/push 首次产出日志时自动拉起；
# 若已存在（重装场景），重启以加载新保留策略。
sudo systemctl restart systemd-journald@dnews.service 2>/dev/null || true

echo "✓ 单元文件 + daily-news 包装脚本已安装"
echo ""

# ─── [6/6] 启用并启动 timer ─────────────────────────────────
echo "🚦 [6/6] 启用并（重）启动 timer..."
# 先 enable 注册开机自启
sudo systemctl enable dnews-fetch.timer dnews-push.timer
# 再 restart 强制按新单元文件重置内存中的 timer 状态
# （enable --now 对已运行的 timer 是 no-op，无法应用新 OnCalendar/OnActiveSec 等改动）
sudo systemctl restart dnews-fetch.timer dnews-push.timer
echo ""

# ─── 完成 ───────────────────────────────────────────────────
echo "✅ 安装完成！"
echo ""
echo "下次触发时间："
systemctl list-timers 'dnews-*' --no-pager || true
echo ""
echo "常用命令（系统 PATH 中可直接调用）："
echo "  daily-news status      查看状态 + 最近日志"
echo "  daily-news logs        实时跟随日志"
echo "  daily-news stop        停止"
echo "  daily-news start       启动"
echo "  daily-news restart     重启 timer"
echo ""
echo "手动立即触发一次任务（不影响下次调度）："
echo "  sudo systemctl start dnews-fetch.service"
echo "  sudo systemctl start dnews-push.service"
echo ""
echo "卸载：$PROJECT_DIR/scripts/uninstall.sh"
