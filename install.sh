#!/bin/bash

set -e

APP=/opt/hax-bot-6.4

echo "🚀 HAX BOT 6.4 INSTALL"

apt update -y
apt install -y python3 python3-pip python3-venv git

rm -rf $APP
mkdir -p $APP
cp -r . $APP
cd $APP

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

mkdir -p data logs

# 🔥 强制交互关键修复
exec < /dev/tty

echo "===================="
read -p "请输入 TOKEN: " TOKEN
echo $TOKEN > token.txt

echo "===================="
read -p "采集时间(秒): " INTERVAL
INTERVAL=${INTERVAL:-30}
echo $INTERVAL > interval.txt

echo "🚀 启动服务..."

nohup python -m app.collector.runner > logs/collector.log 2>&1 &
nohup python -m app.bot.main > logs/bot.log 2>&1 &

echo "✅ 完成"
