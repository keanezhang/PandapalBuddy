# 后台启动 eval-runner 隔离基准运行
$ErrorActionPreference = "Stop"
$root = "C:\Users\keanezhang\PycharmProjects\pandapal_buddy"
$skillDir = Join-Path $root ".pandapal\skills\prd-design"
$runner = Join-Path $root ".pandapal\skills\eval-runner\scripts\run_isolated.py"
$outLog = Join-Path $skillDir "eval-run.log"
$errLog = Join-Path $skillDir "eval-run.err.log"

# UTF-8 环境，避免 GBK 无法打印 emoji
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$proc = Start-Process -FilePath "python" `
    -ArgumentList @($runner, $skillDir, "--samples", "3") `
    -WorkingDirectory $root `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -WindowStyle Hidden `
    -PassThru

Write-Output ("PID=" + $proc.Id)
