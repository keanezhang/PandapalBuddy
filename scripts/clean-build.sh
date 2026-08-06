#!/usr/bin/env bash
# ============================================================
# 打包前清理脚本 (macOS 版)
# 用法: ./scripts/clean-build.sh
# 安全: 仅删除构建产物和缓存，不触及源码和配置
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DESKTOP_DIR="$PROJECT_ROOT/pandapal_desktop"

echo "========================================"
echo "  打包前清理 (macOS)"
echo "  项目根目录: $PROJECT_ROOT"
echo "========================================"
echo ""

deleted_count=0
deleted_items=()

remove_item_safe() {
    local path="$1"
    local label="$2"
    if [ -e "$path" ] || [ -L "$path" ]; then
        if rm -rf "$path" 2>/dev/null; then
            echo "  [DEL] $label"
            echo "        $path"
            deleted_count=$((deleted_count + 1))
            deleted_items+=("$label")
        else
            echo "  [FAIL] $label" >&2
        fi
    else
        echo "  [SKIP] $label (not found)"
    fi
}

# ---- 1. Sidecar old artifacts ----
echo "1. Sidecar old artifacts"
shopt -s nullglob
sidecar_files=("$DESKTOP_DIR/src-tauri/bin/pandapal-sidecar-"*)
shopt -u nullglob
if [ ${#sidecar_files[@]} -gt 0 ]; then
    for f in "${sidecar_files[@]}"; do
        remove_item_safe "$f" "sidecar: $(basename "$f")"
    done
else
    echo "  [SKIP] no sidecar files found"
fi
echo ""

# ---- 2. PyInstaller work dir ----
echo "2. PyInstaller build dir"
remove_item_safe "$DESKTOP_DIR/build" "PyInstaller build/"
echo ""

# ---- 3. PyInstaller global cache ----
echo "3. PyInstaller global cache"
# macOS: pyinstaller 缓存位于 ~/Library/Caches/pyinstaller
pyinstaller_cache="$HOME/Library/Caches/pyinstaller"
remove_item_safe "$pyinstaller_cache" "~/Library/Caches/pyinstaller"
echo ""

# ---- 4. Python __pycache__ (exclude venv) ----
echo "4. Python __pycache__ (exclude venv)"
# 注：不使用 mapfile（bash 3.2 无此内建），改用 while-read 以兼容 macOS 自带 /bin/bash
pycache_dirs=()
while IFS= read -r dir; do
    [ -n "$dir" ] && pycache_dirs+=("$dir")
done < <(find "$PROJECT_ROOT" -type d -name "__pycache__" \
    \( -path "*/venv/*" -o -path "*/node_modules/*" -o -name "venv" \) -prune -o \
    -type d -name "__pycache__" -print 2>/dev/null)
if [ ${#pycache_dirs[@]} -gt 0 ]; then
    for dir in "${pycache_dirs[@]}"; do
        relative=".${dir#$PROJECT_ROOT}"
        remove_item_safe "$dir" "__pycache__: $relative"
    done
else
    echo "  [SKIP] no __pycache__ dirs found"
fi
echo ""

# ---- 5. Frontend dist ----
echo "5. Frontend dist"
remove_item_safe "$DESKTOP_DIR/dist" "frontend dist/"
echo ""

# ---- Summary ----
echo "========================================"
if [ "$deleted_count" -gt 0 ]; then
    echo "  Cleaned $deleted_count item(s):"
    for item in "${deleted_items[@]}"; do
        echo "    - $item"
    done
else
    echo "  Nothing to clean, project is already clean"
fi
echo "========================================"
