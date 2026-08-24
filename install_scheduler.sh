#!/bin/bash
# 安装 launchd 定时任务（macOS）。自动把 plist 模板里的 __PROJECT_DIR__ 换成本机绝对路径。
# launchd 不展开 ~ 和环境变量，所以必须在安装时做替换。
set -euo pipefail
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST=~/Library/LaunchAgents/com.astock.agent.plist

case "$BASE" in
  "$HOME/Desktop"/*|"$HOME/Documents"/*|"$HOME/Downloads"/*)
    echo "🔴 项目位于 macOS TCC 保护目录（Desktop/Documents/Downloads）下。"
    echo "   launchd 拉起的进程读不到这里的文件，任务会每轮静默失败。"
    echo "   请把项目移到不受保护的位置（如 ~/AI_Projects/）后再安装。详见 README。"
    exit 1 ;;
esac

[ -x "$BASE/.venv/bin/python" ] || { echo "🔴 未找到 .venv，请先按 README「环境搭建」创建虚拟环境"; exit 1; }

mkdir -p ~/Library/LaunchAgents "$BASE/logs"
sed "s|__PROJECT_DIR__|$BASE|g" "$BASE/com.astock.agent.plist" > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✓ 已安装：$PLIST"
echo "  项目路径：$BASE"
echo
echo "⚠️ 别只看 load 成功就收工——立刻实测一次，确认真的能拉起脚本："
echo "    launchctl kickstart -k gui/\$(id -u)/com.astock.agent"
echo "    然后检查 $BASE/logs/launchd.err.log 是否为空、tick 日志是否生成。"
