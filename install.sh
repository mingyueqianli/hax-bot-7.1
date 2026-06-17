#!/bin/bash

set -e

APP_NAME="hax-bot"
BASE_DIR="/opt"
SERVICE_NAME="hax-bot"

# =========================
# 颜色输出
# =========================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

echo "🚀 HAX BOT 7.7 安装脚本（优化版）"

# =========================
# 1. 基础环境
# =========================
apt update -y
apt install -y python3 python3-pip python3-venv git curl

# =========================
# 2. 清理旧版本
# =========================
if [ -d "$BASE_DIR/$APP_NAME" ]; then
    rm -rf "$BASE_DIR/$APP_NAME" 2>/dev/null || sudo rm -rf "$BASE_DIR/$APP_NAME"
fi

# =========================
# 3. Clone 仓库
# =========================
echo "📦 cloning repo..."

git config --global http.postBuffer 524288000

if ! git clone https://github.com/mingyueqianli/hax-bot-7.7.git "$BASE_DIR/$APP_NAME" 2>/dev/null; then
    log_warn "clone失败，尝试修复DNS并重试..."
    echo "nameserver 8.8.8.8" > /etc/resolv.conf
    git clone https://github.com/mingyueqianli/hax-bot-7.7.git "$BASE_DIR/$APP_NAME"
fi

cd "$BASE_DIR/$APP_NAME"
echo "📂 当前目录: $(pwd)"

# =========================
# 4. Python环境 + requirements.txt检查
# =========================
log_step "检查 requirements.txt..."

if [ ! -f "requirements.txt" ]; then
    log_error "❌ requirements.txt 不存在！"
    log_error "请确保仓库包含 requirements.txt 文件"
    exit 1
fi

log_info "✅ requirements.txt 存在，内容如下："
cat requirements.txt
echo ""

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip

log_info "安装依赖..."
pip install -r requirements.txt

mkdir -p data logs

# =========================
# 5. 用户输入
# =========================
echo "===================="
echo "请选择模式:"
echo "1) 一键模式（默认）"
echo "2) 交互模式（输入TOKEN和间隔）"
echo "===================="

read -p "输入: " MODE < /dev/tty

if [ "$MODE" = "2" ]; then
    while true; do
        read -p "🔑 TOKEN: " TOKEN < /dev/tty
        if [ -n "$TOKEN" ]; then
            break
        fi
        log_error "TOKEN不能为空，请重新输入"
    done
    
    while true; do
        read -p "⏱ INTERVAL（秒）: " INTERVAL < /dev/tty
        if [[ "$INTERVAL" =~ ^[0-9]+$ ]] && [ "$INTERVAL" -gt 0 ]; then
            break
        fi
        log_error "请输入有效的正整数"
    done
else
    while true; do
        read -p "🔑 TOKEN（必填）: " TOKEN < /dev/tty
        if [ -n "$TOKEN" ]; then
            break
        fi
        log_error "TOKEN不能为空"
    done
    INTERVAL=30
    log_info "使用默认间隔: ${INTERVAL}s"
fi

# =========================
# 6. 写入配置
# =========================
echo "$TOKEN" > token.txt
echo "$INTERVAL" > interval.txt
chmod 600 token.txt interval.txt

# =========================
# 7. 创建 systemd 服务（开机自启 + 进程守护）
# =========================
log_info "创建 systemd 服务（开机自启 + 进程守护）..."

# 停止旧服务
systemctl stop ${SERVICE_NAME}.service ${SERVICE_NAME}-collector.service 2>/dev/null || true

# Bot 服务
cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=HAX BOT 7.7 Service
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$BASE_DIR/$APP_NAME
Environment="PYTHONPATH=$BASE_DIR/$APP_NAME"
Environment="PATH=$BASE_DIR/$APP_NAME/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$BASE_DIR/$APP_NAME/venv/bin/python -m app.bot.main
ExecStop=/bin/kill -TERM \$MAINPID
Restart=always
RestartSec=10
StandardOutput=append:$BASE_DIR/$APP_NAME/logs/bot.log
StandardError=append:$BASE_DIR/$APP_NAME/logs/bot_error.log

[Install]
WantedBy=multi-user.target
EOF

# Collector 服务
cat > /etc/systemd/system/${SERVICE_NAME}-collector.service <<EOF
[Unit]
Description=HAX BOT 7.7 Collector
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$BASE_DIR/$APP_NAME
Environment="PYTHONPATH=$BASE_DIR/$APP_NAME"
Environment="PATH=$BASE_DIR/$APP_NAME/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$BASE_DIR/$APP_NAME/venv/bin/python -m app.collector.runner
ExecStop=/bin/kill -TERM \$MAINPID
Restart=always
RestartSec=10
StandardOutput=append:$BASE_DIR/$APP_NAME/logs/collector.log
StandardError=append:$BASE_DIR/$APP_NAME/logs/collector_error.log

[Install]
WantedBy=multi-user.target
EOF

# =========================
# 8. 启动服务
# =========================
log_info "启动服务..."

# 清理旧进程
pkill -f "python.*app.bot.main" 2>/dev/null || true
pkill -f "python.*app.collector.runner" 2>/dev/null || true

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}.service ${SERVICE_NAME}-collector.service
systemctl start ${SERVICE_NAME}.service ${SERVICE_NAME}-collector.service

sleep 2

# =========================
# 9. 验证状态
# =========================
check_status() {
    if systemctl is-active --quiet $1; then
        log_info "✅ $1 运行中"
        return 0
    else
        log_error "❌ $1 启动失败"
        systemctl status $1 --no-pager
        return 1
    fi
}

check_status ${SERVICE_NAME}.service
check_status ${SERVICE_NAME}-collector.service

# =========================
# 10. 状态输出
# =========================
echo ""
echo "================================"
echo "✅ HAX BOT 7.7 安装完成"
echo "================================"
echo "📦 路径: $BASE_DIR/$APP_NAME"
echo "🔑 TOKEN: ${TOKEN:0:8}...${TOKEN: -4}"
echo "⏱ INTERVAL: ${INTERVAL}s"
echo ""
echo "📋 systemd 服务管理:"
echo "  启动: systemctl start ${SERVICE_NAME}"
echo "  停止: systemctl stop ${SERVICE_NAME}"
echo "  状态: systemctl status ${SERVICE_NAME}"
echo "  重启: systemctl restart ${SERVICE_NAME}"
echo "  日志: journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "📁 应用日志:"
echo "  tail -f $BASE_DIR/$APP_NAME/logs/bot.log"
echo "  tail -f $BASE_DIR/$APP_NAME/logs/collector.log"
echo "================================"
