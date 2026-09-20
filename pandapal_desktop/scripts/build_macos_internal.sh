#!/usr/bin/env bash
#
# PandaPal macOS 内部分发打包脚本（免 Apple 签名 / 免公证）
#
# 做的事：
#   1. 构建 Python sidecar（pnpm sidecar:build）
#   2. 打包 .app + .dmg（pnpm tauri build）
#   3. 对 .app 补全 adhoc 签名（封存 Info.plist / Resources），
#      这样接收方「右键 → 打开」即可绕过 Gatekeeper，而不会报“已损坏”
#   4. 把 PandaPal.app + 安装.command + 安装说明.txt 打成一个 zip
#      （用 ditto，正确保留 .app 内的符号链接，避免 Python.framework 被解引用）
#
# 用法：
#   cd pandapal_desktop
#   bash scripts/build_macos_internal.sh
#
# 产物：pandapal_desktop/PandaPal_<version>_<arch>_内部版.zip
# 把 zip 发给内部同事即可，对方解压后按「安装说明.txt」操作。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_DIR="$(dirname "$SCRIPT_DIR")"          # pandapal_desktop/
TAURI_DIR="$DESKTOP_DIR/src-tauri"

# 版本号（以 tauri.conf.json 为准，与 Cargo.toml / package.json 保持一致）
VERSION=$(grep -m1 '"version"' "$TAURI_DIR/tauri.conf.json" \
  | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')
if [[ -z "$VERSION" ]]; then
  echo "❌ 无法从 tauri.conf.json 解析版本号"
  exit 1
fi

# 架构 → Tauri target triple 命名后缀
ARCH=$(uname -m)
case "$ARCH" in
  arm64)  TRIPLE="aarch64" ;;
  x86_64) TRIPLE="x86_64" ;;
  *)
    echo "❌ 不支持的架构: $ARCH（本脚本只支持 Apple Silicon / Intel 的 Mac）"
    exit 1
    ;;
esac

APP_PATH="$TAURI_DIR/target/release/bundle/macos/PandaPal.app"

cd "$DESKTOP_DIR"

echo "==> [1/4] 构建 sidecar"
pnpm sidecar:build

echo "==> [2/4] 打包 .app + .dmg"
pnpm tauri build

if [[ ! -d "$APP_PATH" ]]; then
  echo "❌ 未找到打包产物: $APP_PATH"
  exit 1
fi

echo "==> [3/4] 对 .app 补全 adhoc 签名（封存 Info.plist / Resources）"
# 先清掉本机可能残留的隔离属性，再递归重签整个 bundle（含 sidecar 内嵌二进制）
xattr -cr "$APP_PATH" 2>/dev/null || true
codesign --force --deep --sign - "$APP_PATH"

echo "==> [4/4] 生成内部分发 zip"
DIST_DIR="$DESKTOP_DIR/dist-internal"
ZIP_PATH="$DESKTOP_DIR/PandaPal_${VERSION}_${TRIPLE}_内部版.zip"

rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

cp -R "$APP_PATH" "$DIST_DIR/"
cp "$SCRIPT_DIR/install_internal.command" "$DIST_DIR/安装.command"
chmod +x "$DIST_DIR/安装.command"
find "$DIST_DIR" -name '.DS_Store' -delete

cat > "$DIST_DIR/安装说明.txt" <<'EOF'
PandaPal 内部版安装说明
========================

适用机型：Apple Silicon（M 系列）Mac
（Intel Mac 需在 Intel 机器上重新打包，本包无法直接运行）

安装步骤（任选其一）：

方式 A（推荐，最简单）
  1. 双击「安装.command」
  2. 若提示「无法打开 / 无法验证开发者」：右键点它 → 打开 → 再点「打开」
  3. 脚本会自动去除系统隔离、补全签名、安装到「应用程序」并启动

方式 B（终端）
  1. 打开「终端」
  2. 把「安装.command」拖进终端窗口，按回车

方式 C（只想临时跑，不安装）
  1. 右键 PandaPal.app → 打开 → 再点「打开」即可

说明：本包未做 Apple 签名与公证，macOS 首次打开可能拦截，属正常现象，
按上面任一步骤绕过即可。覆盖安装不会影响已有聊天数据。
EOF

rm -f "$ZIP_PATH"
# ditto 打包：正确保留 .app 内的符号链接（避免 Python.framework 被解引用导致体积暴涨）
(cd "$DIST_DIR" && ditto -c -k --sequesterRsrc . "$ZIP_PATH")

rm -rf "$DIST_DIR"

echo ""
echo "✅ 打包完成"
echo "   分发文件: $ZIP_PATH"
echo "   把该 zip 发给内部同事即可（微信 / AirDrop / 网盘均可）"
echo "   对方解压后按「安装说明.txt」操作"
