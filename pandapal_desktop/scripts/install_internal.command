#!/bin/bash
#
# PandaPal 内部版一键安装脚本（在接收方 Mac 上运行）
#
# 作用：退出旧实例、清理重复副本、去除 macOS 隔离属性、补全临时签名、
#       修复可执行权限、安装到「应用程序」并启动。免 Apple 签名 / 免公证。
#
# 用法：
#   双击运行；或终端执行  bash 安装.command
#   （若双击提示"无法打开"：右键 → 打开 → 再点「打开」）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="PandaPal.app"
SRC_APP="$SCRIPT_DIR/$APP_NAME"
DEST="/Applications/$APP_NAME"

if [[ ! -d "$SRC_APP" ]]; then
  echo "❌ 未找到 $APP_NAME"
  echo "   请把本脚本和 $APP_NAME 放在同一目录后再运行。"
  echo "   注意：请用 Finder 双击 zip 解压，不要用微信内置预览解压（会丢可执行权限）。"
  exit 1
fi

echo "==> 退出正在运行的 PandaPal（关窗只是隐藏到托盘，进程还在）"
osascript -e 'tell application "PandaPal" to quit' >/dev/null 2>&1 || true
pkill -f pandapal-desktop 2>/dev/null || true
pkill -f pandapal-sidecar 2>/dev/null || true
sleep 1

echo "==> 去除系统隔离属性（绕过 Gatekeeper）"
xattr -cr "$SRC_APP" 2>/dev/null || true

echo "==> 补全临时签名"
codesign --force --deep --sign - "$SRC_APP" >/dev/null 2>&1 || true

echo "==> 修复可执行权限（微信传输/部分解压工具会剥掉执行位）"
chmod +x "$SRC_APP/Contents/MacOS/"* 2>/dev/null || true
# sidecar 主程序（onedir 目录下的可执行文件）
find "$SRC_APP/Contents/Resources/bin" -type f -name 'pandapal-sidecar-*' \
  -exec chmod +x {} + 2>/dev/null || true

echo "==> 清理旧版本与重复副本（如 'PandaPal 2.app'）"
rm -rf "$DEST" 2>/dev/null || true
rm -rf "/Applications/PandaPal 2.app" 2>/dev/null || true

echo "==> 安装到 /Applications"
# 用 ditto 拷贝：完整保留符号链接与可执行权限
ditto "$SRC_APP" "$DEST"
xattr -cr "$DEST" 2>/dev/null || true
chmod +x "$DEST/Contents/MacOS/"* 2>/dev/null || true

echo "✅ 安装完成：$DEST"
echo "==> 启动 PandaPal"
open "$DEST"
