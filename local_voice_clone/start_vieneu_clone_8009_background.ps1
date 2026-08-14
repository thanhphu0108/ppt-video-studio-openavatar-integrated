$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$serviceBat = Join-Path $root "start_vieneu_clone_8009.bat"
if (-not (Test-Path -LiteralPath $serviceBat -PathType Leaf)) {
    throw "Không tìm thấy $serviceBat"
}

$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$stdoutLog = Join-Path $logDir "vieneu_service_console.log"
$stderrLog = Join-Path $logDir "vieneu_service_error.log"

$child = Start-Process `
    -FilePath $env:ComSpec `
    -ArgumentList @("/d", "/c", "call `"$serviceBat`"") `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

Write-Output "VieNeu service đã chạy nền. PID launcher: $($child.Id)"
Write-Output "Log: $stdoutLog"
Write-Output "Lỗi: $stderrLog"
