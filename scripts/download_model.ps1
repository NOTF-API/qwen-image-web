# Download Qwen-Image-2.1 weights from ModelScope (fast mirror in China)
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\download_model.ps1
# Idempotent: re-run to resume/repair (uses curl -C - resume, .part temp files)
param(
  [string]$OutDir = (Join-Path $PSScriptRoot "..\model"),
  [string]$Gguf = "qwen-image-2.1-Q4_K_M.gguf"
)
$ErrorActionPreference = "Stop"
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$base    = "https://modelscope.cn/api/v1/models/Qwen/Qwen-Image-2.1/repo?Revision=master&FilePath="
$ggufDir = "https://modelscope.cn/api/v1/models/unsloth/Qwen-Image-2.1-GGUF/repo?Revision=master&FilePath="

# Official diffusers-layout files (transformer safetensors are SKIPPED: replaced by GGUF)
$files = @(
  @{ p = "model_index.json"; s = 447 },
  @{ p = "scheduler/scheduler_config.json"; s = 485 },
  @{ p = "processor/added_tokens.json"; s = 707 },
  @{ p = "processor/chat_template.jinja"; s = 5292 },
  @{ p = "processor/merges.txt"; s = 1671853 },
  @{ p = "processor/preprocessor_config.json"; s = 782 },
  @{ p = "processor/special_tokens_map.json"; s = 613 },
  @{ p = "processor/tokenizer.json"; s = 11422654 },
  @{ p = "processor/tokenizer_config.json"; s = 5445 },
  @{ p = "processor/video_preprocessor_config.json"; s = 817 },
  @{ p = "processor/vocab.json"; s = 2776833 },
  @{ p = "text_encoder/config.json"; s = 1517 },
  @{ p = "text_encoder/generation_config.json"; s = 213 },
  @{ p = "text_encoder/model-00001-of-00004.safetensors"; s = 4998056552 },
  @{ p = "text_encoder/model-00002-of-00004.safetensors"; s = 4915962464 },
  @{ p = "text_encoder/model-00003-of-00004.safetensors"; s = 4915962496 },
  @{ p = "text_encoder/model-00004-of-00004.safetensors"; s = 2704357976 },
  @{ p = "text_encoder/model.safetensors.index.json"; s = 67795 },
  @{ p = "transformer/config.json"; s = 370 },
  @{ p = "vae/config.json"; s = 2079 },
  @{ p = "vae/diffusion_pytorch_model.safetensors"; s = 1350989512 }
)

function Get-OneFile($url, $dest, $expect) {
  $dir = Split-Path $dest -Parent
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  if (Test-Path $dest) {
    $len = (Get-Item $dest).Length
    if ($expect -le 0 -or $len -eq $expect) { Write-Host ("skip   {0}" -f $dest); return }
    Write-Host ("fix    {0}  ({1} != {2})" -f $dest, $len, $expect)
  }
  $tmp = "$dest.part"
  if (Test-Path $tmp) {
    $plen = (Get-Item $tmp).Length
    if ($expect -gt 0 -and $plen -eq $expect) { Move-Item -Force $tmp $dest; Write-Host ("done   {0}" -f $dest); return }
    if ($expect -gt 0 -and $plen -gt $expect) { Remove-Item -Force $tmp }
  }
  $mb = [math]::Round($expect / 1MB, 1)
  Write-Host ("get    {0}  ({1} MB)" -f $dest, $mb)
  & curl.exe -sS -L -C - --retry 5 --retry-delay 2 --fail -o "$tmp" "$url"
  if ($LASTEXITCODE -ne 0) { throw "curl failed ($LASTEXITCODE): $url" }
  $len = (Get-Item $tmp).Length
  if ($expect -gt 0 -and $len -ne $expect) { throw "size mismatch: $dest expect=$expect got=$len" }
  Move-Item -Force $tmp $dest
}

Write-Host "=== Qwen-Image-2.1 official files -> $OutDir ==="
foreach ($f in $files) { Get-OneFile ($base + $f.p) (Join-Path $OutDir ($f.p -replace "/", "\")) $f.s }

Write-Host "=== DiT GGUF ($Gguf) -> transformer ==="
Get-OneFile ($ggufDir + $Gguf) (Join-Path $OutDir ("transformer\" + $Gguf)) 4199565024

Write-Host "=== DONE ==="
Get-ChildItem $OutDir -Recurse -File | Measure-Object -Property Length -Sum |
  ForEach-Object { Write-Host ("total: {0} GB / {1} files" -f [math]::Round($_.Sum / 1GB, 2), $_.Count) }
