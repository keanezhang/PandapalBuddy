# ============================================================
# 打包前清理脚本
# 用法: .\scripts\clean-build.ps1
# 安全: 仅删除构建产物和缓存，不触及源码和配置
# ============================================================

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path "$ScriptDir\.."
$DesktopDir = "$ProjectRoot\pandapal_desktop"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  打包前清理" -ForegroundColor Cyan
Write-Host "  项目根目录: $ProjectRoot" -ForegroundColor DarkGray
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$deletedCount = 0
$deletedItems = @()

function Remove-ItemSafe {
    param([string]$Path, [string]$Label)
    if (Test-Path $Path) {
        try {
            Remove-Item -Path $Path -Recurse -Force -ErrorAction Stop
            Write-Host "  [DEL] $Label" -ForegroundColor Green
            Write-Host "        $Path" -ForegroundColor DarkGray
            $script:deletedCount++
            $script:deletedItems += $Label
        } catch {
            Write-Host "  [FAIL] $Label -- $($_.Exception.Message)" -ForegroundColor Red
        }
    } else {
        Write-Host "  [SKIP] $Label (not found)" -ForegroundColor DarkGray
    }
}

# ---- 1. Sidecar old artifacts ----
Write-Host "1. Sidecar old artifacts" -ForegroundColor Yellow
$sidecarPattern = "$DesktopDir\src-tauri\bin\pandapal-sidecar-*"
$sidecarFiles = Get-ChildItem -Path $sidecarPattern -ErrorAction SilentlyContinue
if ($sidecarFiles) {
    foreach ($f in $sidecarFiles) {
        Remove-ItemSafe -Path $f.FullName -Label "sidecar: $($f.Name)"
    }
} else {
    Write-Host "  [SKIP] no sidecar files found" -ForegroundColor DarkGray
}
Write-Host ""

# ---- 2. PyInstaller work dir ----
Write-Host "2. PyInstaller build dir" -ForegroundColor Yellow
Remove-ItemSafe -Path "$DesktopDir\build" -Label "PyInstaller build/"
Write-Host ""

# ---- 3. PyInstaller global cache ----
Write-Host "3. PyInstaller global cache" -ForegroundColor Yellow
$pyinstallerCache = "$env:APPDATA\pyinstaller"
Remove-ItemSafe -Path $pyinstallerCache -Label "%APPDATA%\pyinstaller"
Write-Host ""

# ---- 4. Python __pycache__ (exclude venv) ----
Write-Host "4. Python __pycache__ (exclude venv)" -ForegroundColor Yellow
$pycacheDirs = Get-ChildItem -Path $ProjectRoot -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\venv\\' -and $_.FullName -notmatch '\\node_modules\\' -and $_.FullName -notmatch '\\venv$' }

if ($pycacheDirs) {
    foreach ($dir in $pycacheDirs) {
        $relative = $dir.FullName.Replace($ProjectRoot, ".").Replace("\", "/")
        Remove-ItemSafe -Path $dir.FullName -Label "__pycache__: $relative"
    }
} else {
    Write-Host "  [SKIP] no __pycache__ dirs found" -ForegroundColor DarkGray
}
Write-Host ""

# ---- 5. Frontend dist ----
Write-Host "5. Frontend dist" -ForegroundColor Yellow
Remove-ItemSafe -Path "$DesktopDir\dist" -Label "frontend dist/"
Write-Host ""

# ---- Summary ----
Write-Host "========================================" -ForegroundColor Cyan
if ($deletedCount -gt 0) {
    Write-Host "  Cleaned $deletedCount item(s):" -ForegroundColor Green
    foreach ($item in $deletedItems) {
        Write-Host "    - $item" -ForegroundColor Green
    }
} else {
    Write-Host "  Nothing to clean, project is already clean" -ForegroundColor Green
}
Write-Host "========================================" -ForegroundColor Cyan
