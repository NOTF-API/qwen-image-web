# start.ps1 — Qwen-Image-2.1 服务启动器（启动时选择量化模型）
# 用法:
#   .\start.ps1            交互菜单选择模型
#   .\start.ps1 Q5_K_S     直接指定（支持部分匹配: Q4 / Q5_K_S / Q5_K_M 或完整文件名）
param(
    [string]$Quant = ""
)

$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py   = Join-Path $Base "venv\Scripts\python.exe"
$Tf   = Join-Path $Base "model\transformer"

if (-not (Test-Path $Py)) {
    Write-Host "[错误] 找不到 venv: $Py" -ForegroundColor Red
    exit 1
}

$models = @(Get-ChildItem -Path $Tf -Filter *.gguf -ErrorAction SilentlyContinue | Sort-Object Name)
if ($models.Count -eq 0) {
    Write-Host "[错误] $Tf 下没有 .gguf 模型，请先运行 scripts/download_gguf.ps1" -ForegroundColor Red
    exit 1
}

# 默认量化（与 server.py 默认一致: Q4_K_M）
$default = $models | Where-Object { $_.Name -like "*Q4_K_M*" } | Select-Object -First 1
if (-not $default) { $default = $models[0] }

$chosen = $null
if ($Quant -ne "") {
    $chosen = $models | Where-Object { $_.Name -like "*$Quant*" } | Select-Object -First 1
    if (-not $chosen) {
        Write-Host "[错误] 没有匹配的模型: $Quant" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host ""
    Write-Host "===== 选择量化模型 =====" -ForegroundColor Cyan
    for ($i = 0; $i -lt $models.Count; $i++) {
        $tag = ""
        if ($models[$i].Name -eq $default.Name) { $tag = "  (默认)" }
        $gb = [math]::Round($models[$i].Length / 1GB, 2)
        Write-Host ("  {0}) {1,-42} {2,6} GB{3}" -f ($i + 1), $models[$i].Name, $gb, $tag)
    }
    Write-Host ""
    $ans = Read-Host "输入编号后回车（直接回车 = 默认: $($default.Name)）"
    if ($ans -ne "") {
        $n = 0
        if ([int]::TryParse($ans, [ref]$n) -and $n -ge 1 -and $n -le $models.Count) {
            $chosen = $models[$n - 1]
        } else {
            Write-Host "[错误] 无效输入: $ans" -ForegroundColor Red
            exit 1
        }
    } else {
        $chosen = $default
    }
}

# 端口占用检查
$port = "8091"
if ($env:QWEN_PORT) { $port = $env:QWEN_PORT }
$c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($c) {
    $pids = @($c.OwningProcess | Sort-Object -Unique)
    Write-Host "[提示] 端口 $port 已被占用 (PID: $($pids -join ', '))" -ForegroundColor Yellow
    $k = Read-Host "是否结束旧进程并继续? [y/N]"
    if ($k -ne "y" -and $k -ne "Y") {
        Write-Host "已取消"
        exit 0
    }
    $pids | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
}

$env:QWEN_GGUF = $chosen.Name
Write-Host ""
Write-Host "启动服务: 模型=$($chosen.Name)  端口=$port" -ForegroundColor Green
Write-Host "网页: http://127.0.0.1:$port/    API: POST /v1/images/generations" -ForegroundColor Green
Write-Host "停止服务: 在本窗口按 Ctrl+C" -ForegroundColor Green
Write-Host ""
Set-Location $Base
& $Py -u server.py
