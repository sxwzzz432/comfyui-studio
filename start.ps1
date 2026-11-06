<#
    start.ps1 — 一键启动 ComfyUI 出图台

    做四件事：
      1. 找一个能用的端口（默认 5500；被占用就复用已有的，或往后找空位）
      2. 在仓库根目录起静态服务器（页面要读 ../workflow/moss.json，所以根目录必须对）
      3. 探一下 ComfyUI 在不在，不在就提醒（不阻塞，页面照样能打开）
      4. 打开默认浏览器；按 Ctrl+C 停服务

    用法：
      .\start.ps1                             # 默认 http://127.0.0.1:5500/web/app.html
      .\start.ps1 -Port 5600                  # 换端口
      .\start.ps1 -Comfy 192.168.1.6:8188     # 顺带检查别的 ComfyUI 地址
      .\start.ps1 -NoBrowser                  # 只起服务，不开浏览器（做测试时用）

    或者直接双击 start.bat。
#>
[CmdletBinding()]
param(
  [int]$Port = 5500,
  [switch]$NoBrowser,
  [string]$Comfy = '127.0.0.1:8188',
  [int]$PortScan = 20,
  [switch]$Stop
)

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }

function Step($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[x] $m" -ForegroundColor Red }

function Find-Python {
  $cands = @(
    @{ Exe = 'python';  Pre = @() },
    @{ Exe = 'py';      Pre = @('-3') },
    @{ Exe = 'python3'; Pre = @() }
  )
  foreach ($c in $cands) {
    $cmd = Get-Command $c.Exe -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    try {
      $out = & $c.Exe @($c.Pre) '--version' 2>&1
      if ($LASTEXITCODE -eq 0 -and ("$out" -match 'Python \d+\.\d+')) {
        return @{ Exe = $c.Exe; Pre = $c.Pre; Version = ("$out").Trim() }
      }
    } catch { }
  }
  return $null
}

function Test-Page([string]$url, [int]$timeoutSec = 2) {
  try {
    $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $timeoutSec -ErrorAction Stop
    return ($r.StatusCode -eq 200)
  } catch { return $false }
}

Write-Host ''
Write-Host '  ComfyUI 出图台 · 一键启动' -ForegroundColor White
Write-Host '  ------------------------------------------------------------' -ForegroundColor DarkGray

# ---------- -Stop：停掉占着端口的 python ----------
if ($Stop) {
  $conns = @()
  try { $conns = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) } catch { }
  if (-not $conns -or $conns.Count -eq 0) {
    Warn ("端口 {0} 上没有服务在跑" -f $Port)
    exit 0
  }
  $killed = 0
  foreach ($c in $conns) {
    $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -match '^python') {
      Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
      Ok ("已停止 pid={0}（{1}）" -f $p.Id, $p.ProcessName)
      $killed++
    } elseif ($p) {
      Warn ("端口 {0} 被 {1}（pid={2}）占着，看着不像本项目的服务，没动它" -f $Port, $p.ProcessName, $p.Id)
    }
  }
  if ($killed -eq 0) { Warn '没有可停止的进程' }
  exit 0
}

# ---------- 1. python ----------
Step '查找 Python …'
$py = Find-Python
if (-not $py) {
  Fail '没找到可用的 Python。装一个 Python 3（勾选 Add to PATH）再试。'
  exit 1
}
Ok ("{0} {1}" -f $py.Exe, $py.Version)

# ---------- 2. 找端口 / 复用 ----------
Step ("准备端口 {0} …" -f $Port)
$httpPort = $null
$proc = $null
$reused = $false

for ($i = 0; $i -le $PortScan; $i++) {
  $p = $Port + $i
  $candidate = "http://127.0.0.1:$p/web/app.html"

  if (Test-Page $candidate 1) {
    $httpPort = $p
    $reused = $true
    Ok ("端口 {0} 上已经有这个页面在跑，直接复用（不重复起服务）" -f $p)
    break
  }

  $stamp = "comfyui-studio-$p"
  $outLog = Join-Path $env:TEMP "$stamp.out.log"
  $errLog = Join-Path $env:TEMP "$stamp.err.log"
  $args = @()
  if ($py.Pre.Count -gt 0) { $args += $py.Pre }
  $args += @('-m', 'http.server', "$p", '--bind', '127.0.0.1')

  $pr = Start-Process -FilePath $py.Exe -ArgumentList $args -WorkingDirectory $root `
        -NoNewWindow -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
  Start-Sleep -Milliseconds 900

  if ($pr.HasExited) { continue }        # 端口被别的程序占了，换下一个

  $ok = $false
  for ($k = 0; $k -lt 25; $k++) {
    if (Test-Page $candidate 1) { $ok = $true; break }
    Start-Sleep -Milliseconds 200
  }
  if ($ok) { $httpPort = $p; $proc = $pr; break }

  try { Stop-Process -Id $pr.Id -Force -ErrorAction SilentlyContinue } catch { }
}

if (-not $httpPort) {
  Fail ("端口 {0}-{1} 都被占用了，换一个： .\start.ps1 -Port 6000" -f $Port, ($Port + $PortScan))
  exit 1
}

$pageUrl = "http://127.0.0.1:$httpPort/web/app.html"
Ok ("服务已就绪：{0}（进程 {1}）" -f $pageUrl, $(if ($proc) { $proc.Id } else { '外部' }))

# ---------- 3. ComfyUI ----------
Step '探 ComfyUI …'
$comfyUrl = if ($Comfy -match '^https?://') { $Comfy } else { "http://$Comfy" }
if (Test-Page "$comfyUrl/system_stats" 2) {
  Ok ("ComfyUI 可达：{0}" -f $comfyUrl)
} else {
  Warn ("ComfyUI 没响应（{0}）" -f $comfyUrl)
  Write-Host '    页面能打开，但出图前要先启动 ComfyUI。' -ForegroundColor DarkGray
  Write-Host '    启动后如果地址不是默认的，点页面右上角那个地址改一下即可。' -ForegroundColor DarkGray
}

# ---------- 4. 打开浏览器 ----------
if ($NoBrowser) {
  Write-Host ''
  Ok ("-NoBrowser 已指定，请手动打开：{0}" -f $pageUrl)
} else {
  Start-Process $pageUrl
  Write-Host ''
  Ok '已在默认浏览器中打开'
}

Write-Host ''
Write-Host '  ------------------------------------------------------------' -ForegroundColor DarkGray
Write-Host ("  页面    {0}" -f $pageUrl)
Write-Host '  工作流  左上「载入工作流 (API)」或把 json 拖进窗口'
Write-Host '  参数    改完自动存快照，刷新不丢；点「恢复内置」回默认值'
if ($reused) {
  Write-Host ''
  Write-Host '  这次是复用已有服务，脚本退出，服务继续在原来的窗口里跑。' -ForegroundColor DarkGray
  Write-Host ''
  exit 0
}
Write-Host ''
Write-Host '  按 Ctrl+C 停止服务' -ForegroundColor Yellow
Write-Host ''

try {
  while ($true) { Start-Sleep -Seconds 1 }
}
finally {
  if ($proc -and -not $proc.HasExited) {
    try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch { }
    Write-Host ''
    Write-Host '  服务已停止。' -ForegroundColor DarkGray
  }
}
