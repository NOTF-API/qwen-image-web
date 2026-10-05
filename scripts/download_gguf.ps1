# 从 ModelScope (unsloth/Qwen-Image-2.1-GGUF) 分段并行下载单个 GGUF 文件。
# 单连接在 ModelScope 上可能被限速到 ~2MB/s，分段并行可跑满带宽。
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\download_gguf.ps1 -Name qwen-image-2.1-Q5_K_S.gguf
# 断点续传: 已存在的 .part 文件会作为第 1 段复用，只需重跑同一命令。
param(
  [Parameter(Mandatory = $true)][string]$Name,
  [string]$OutDir = "",
  [int]$Segments = 6
)
$ErrorActionPreference = "Stop"
if (-not $OutDir) { $OutDir = Join-Path $PSScriptRoot "..\model\transformer" }  # $PSScriptRoot 在 param 默认值里为空，须在体内解析
$OutDir = [System.IO.Path]::GetFullPath($OutDir)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$dest = Join-Path $OutDir $Name
$part = "$dest.part"

$repo = "unsloth/Qwen-Image-2.1-GGUF"
$r = Invoke-RestMethod -Uri "https://modelscope.cn/api/v1/models/$repo/repo/files?Revision=master" -TimeoutSec 30
$f = $r.data.files | Where-Object { $_.Path -eq $Name }
if (-not $f) { Write-Output "ERROR: $Name not found in $repo"; exit 1 }
$total = [int64]$f.Size
Write-Output "remote size: $total bytes ($([math]::Round($total/1GB,2)) GB)"

if (Test-Path $dest) {
  if ((Get-Item $dest).Length -eq $total) { Write-Output "already downloaded and verified by size: $dest"; exit 0 }
  Write-Output "existing file has wrong size, re-downloading"
  Remove-Item $dest -Force
}

Get-ChildItem "$part.seg*" -ErrorAction SilentlyContinue | Remove-Item -Force
if (Test-Path "$dest.new") { Remove-Item "$dest.new" -Force }

$start = 0
if (Test-Path $part) {
  $start = (Get-Item $part).Length
  if ($start -ge $total) { Move-Item -Force $part $dest; Write-Output "completed from .part: $dest"; exit 0 }
}
Write-Output "resume from: $start bytes"

$remain = $total - $start
$chunk = [math]::Ceiling($remain / $Segments)
$url = "https://modelscope.cn/api/v1/models/$repo/repo?Revision=master&FilePath=$Name"
$procs = @()
$sw = [Diagnostics.Stopwatch]::StartNew()
for ($i = 0; $i -lt $Segments; $i++) {
  $s = $start + [int64]$i * $chunk
  if ($s -ge $total) { break }
  $e = [math]::Min($s + $chunk - 1, $total - 1)
  $procs += Start-Process -FilePath "curl.exe" -ArgumentList "-sS", "-L", "--fail", "-r", "$s-$e", "-o", "$part.seg$i", "--retry", "5", "--retry-delay", "2", $url -PassThru -NoNewWindow
}
Write-Output "launched $($procs.Count) parallel segments, waiting..."
Wait-Process -Id $procs.Id
$sw.Stop()

$ok = $true
for ($i = 0; $i -lt $procs.Count; $i++) {
  $seg = "$part.seg$i"
  $s2 = $start + [int64]$i * $chunk
  $e2 = [math]::Min($s2 + $chunk - 1, $total - 1)
  $exp = $e2 - $s2 + 1
  if (-not (Test-Path $seg)) { Write-Output "MISSING seg$i"; $ok = $false; continue }
  $got = (Get-Item $seg).Length
  if ($got -ne $exp) { Write-Output "SIZE MISMATCH seg$i got=$got exp=$exp"; $ok = $false }
}
if (-not $ok) { exit 1 }

$list = @()
if (Test-Path $part) { $list += $part }
for ($i = 0; $i -lt $procs.Count; $i++) { $list += "$part.seg$i" }
$concat = ($list -join "+")
cmd /c copy /b "$concat" "$dest.new" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Output "concat failed"; exit 1 }
$flen = (Get-Item "$dest.new").Length
if ($flen -ne $total) { Write-Output "FINAL SIZE MISMATCH got=$flen exp=$total"; exit 1 }
Move-Item -Force "$dest.new" $dest
$list | ForEach-Object { Remove-Item $_ -Force }
Write-Output "DONE in $([math]::Round($sw.Elapsed.TotalSeconds,1))s: $dest ($([math]::Round($flen/1GB,2)) GB, $($Segments) segments verified)"
