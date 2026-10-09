# 下载 Noct-Q (Noctaluna/Noct-Q-Uncensored-Qwen-Image-2.1) 的 int8 单文件权重。
#
# 该仓库是 ComfyUI 格式(int8 convrot)，不是 diffusers 目录布局，不能直接喂给
# server.py；需要先用 scripts/noctq_to_gguf.py 转成 GGUF（8GB 卡放不下 7GB int8）。
# 本脚本只负责把原始文件拉到 model/noctq/，转换后原始文件可自行删除。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\download_noctq.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\download_noctq.ps1 -Name NoctQ_V3_base_int8_convrot.safetensors
#
# 断点续传: 已存在的 .part.seg* 分段会复用，只需重跑同一命令。
# 国内走 hf-mirror.com 镜像（server.py 里 HF_ENDPOINT 默认也是它）。
param(
  [string]$Name = "NoctQ_V4_int8_convrot.safetensors",
  [string]$OutDir = "",
  [int]$Segments = 6,
  [string]$Endpoint = "https://hf-mirror.com"
)
$ErrorActionPreference = "Stop"
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot "..\model\noctq" }  # $PSScriptRoot 在 param 默认值里为空，须在体内解析
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$repo = "Noctaluna/Noct-Q-Uncensored-Qwen-Image-2.1"

# 已知文件大小（字节）；不在表里的文件会退化成单连接下载（无 size 校验）
$sizes = @{
  "NoctQ_V4_int8_convrot.safetensors"        = 7256784368
  "NoctQ_V3_base_int8_convrot.safetensors"  = 7256784376
  "NoctQ_V4_workflow.json"                   = 7251
  "NoctQ_V3_base_workflow.json"              = 7668
  "NOTICE"                                   = 456
  "LICENSE"                                  = 7831
  "README.md"                                = 2890
}

if (-not $sizes.ContainsKey($Name)) {
  Write-Output "ERROR: unknown file '$Name'. Known:"
  $sizes.Keys | ForEach-Object { Write-Output "  $_" }
  exit 1
}
$total = [int64]$sizes[$Name]
$dest = Join-Path $OutDir $Name
$part = "$dest.part"

Write-Output "=== Noct-Q: $Name ($([math]::Round($total/1GB,2)) GB) -> $OutDir ==="

if (Test-Path $dest) {
  if ((Get-Item $dest).Length -eq $total) { Write-Output "already downloaded and verified by size: $dest"; exit 0 }
  Write-Output "existing file has wrong size, re-downloading"
  Remove-Item $dest -Force
}

# 已经拼完但没改名的情况
if ((Test-Path $part) -and ((Get-Item $part).Length -eq $total)) {
  Move-Item -Force $part $dest
  Write-Output "completed from .part: $dest"
  exit 0
}

Get-ChildItem "$part.seg*" -ErrorAction SilentlyContinue |
  Where-Object { $_.Length -eq 0 } | Remove-Item -Force

$start = 0
$remain = $total
if (Test-Path $part) { $start = (Get-Item $part).Length }
Write-Output "resume from: $start bytes"

$remain = $total - $start
$chunk = [math]::Ceiling($remain / $Segments)
$url = "$Endpoint/$repo/resolve/main/$Name"
Write-Output "url: $url"

$procs = @()
$segs = @()
for ($i = 0; $i -lt $Segments; $i++) {
  $s = $start + [int64]$i * $chunk
  if ($s -ge $total) { break }
  $e = [math]::Min($s + $chunk - 1, $total - 1)
  $exp = $e - $s + 1
  $seg = "$part.seg$i"
  # 已下完整的分段直接复用（上次可能只掉了某一段）
  if ((Test-Path $seg) -and ((Get-Item $seg).Length -eq $exp)) {
    Write-Output "seg$i already complete, skip"
    $segs += ,@($i, $seg, $s, $e)
    continue
  }
  if (Test-Path $seg) { Remove-Item $seg -Force }
  $segs += ,@($i, $seg, $s, $e)
  $procs += Start-Process -FilePath "curl.exe" -ArgumentList "-sS", "-L", "--fail", "-r", "$s-$e", "-o", "$seg", "--retry", "5", "--retry-delay", "2", $url -PassThru -NoNewWindow
}

$sw = [Diagnostics.Stopwatch]::StartNew()
if ($procs.Count -gt 0) {
  Write-Output "launched $($procs.Count) new segments, waiting..."
  Wait-Process -Id $procs.Id
} else {
  Write-Output "all segments already complete"
}
$sw.Stop()

$ok = $true
foreach ($x in $segs) {
  $i = $x[0]; $seg = $x[1]; $s = [int64]$x[2]; $e = [int64]$x[3]
  $exp = $e - $s + 1
  if (-not (Test-Path $seg)) { Write-Output "MISSING seg$i"; $ok = $false; continue }
  $got = (Get-Item $seg).Length
  if ($got -ne $exp) { Write-Output "SIZE MISMATCH seg$i got=$got exp=$exp"; $ok = $false }
}
if (-not $ok) { exit 1 }

$list = @()
if (Test-Path $part) { $list += $part }
foreach ($x in $segs) { $list += $x[1] }
$concat = ($list -join "+")
cmd /c copy /b "$concat" "$dest.new" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Output "concat failed"; exit 1 }
$flen = (Get-Item "$dest.new").Length
if ($flen -ne $total) { Write-Output "FINAL SIZE MISMATCH got=$flen exp=$total"; exit 1 }
Move-Item -Force "$dest.new" $dest
$list | ForEach-Object { Remove-Item $_ -Force }
Write-Output "DONE in $([math]::Round($sw.Elapsed.TotalSeconds,1))s: $dest ($([math]::Round($flen/1GB,2)) GB, $($segs.Count) segments verified)"
