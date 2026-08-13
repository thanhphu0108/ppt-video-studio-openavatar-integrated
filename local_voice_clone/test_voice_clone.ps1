$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$jsonFile = Join-Path $root "test_voice.json"

Write-Host "== HEALTH ==" -ForegroundColor Cyan
Invoke-RestMethod "http://127.0.0.1:8009/health" | Format-List

Write-Host "== VOICES ==" -ForegroundColor Cyan
Invoke-RestMethod "http://127.0.0.1:8009/v1/voices" | ConvertTo-Json -Depth 10

if (-not (Test-Path $jsonFile)) {
    throw "Không tìm thấy $jsonFile"
}

Write-Host "== SYNTHESIZE ==" -ForegroundColor Cyan
$response = curl.exe -sS -X POST `
  "http://127.0.0.1:8009/v1/voice-clone/synthesize" `
  -H "Content-Type: application/json; charset=utf-8" `
  --data-binary "@$jsonFile"

$response
