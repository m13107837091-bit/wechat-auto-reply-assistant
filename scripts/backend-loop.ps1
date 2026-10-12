<#
.SYNOPSIS
    让后端在无人值守时保持存活：等微信就绪 → 拉起后端 → 退出就等一会儿再拉起。

.DESCRIPTION
    由「登录时自动启动」的计划任务以隐藏窗口调用，所以它本身不会退出。

    为什么要先等微信：登录时本脚本与微信（HKCU 的 Run 项）几乎同时启动。若后端先跑
    起来，wechat_client.prepare() 会找不到微信窗口而失败，而 main.py 只尝试一次、
    不会自动重试 —— 结果就是「后端在跑，但自动回复没开」。所以这里先等微信进程出现，
    再留一点时间让它把主窗口建出来。

    批次 1 起，本脚本拉起的对象是桌面壳 desktop.exe（托盘程序），由壳再拉起后端。
    壳的退出码被当作信号使用：
        0    = 用户从托盘点了「退出」→ 本脚本停止循环，不再把它拉起来；
        非 0 = 崩溃 → 等 BackoffSeconds 秒后重来。

    本脚本的日志写在 <仓库根>\wechatauto_logs\backend-loop.log。

    注意：本脚本会一直循环。不要手动双击运行 —— 要停止请直接结束该进程，
    或用 scripts\install-autostart.ps1 -Uninstall 卸载计划任务。

.PARAMETER BackoffSeconds
    后端退出后重新拉起的等待秒数（防止配置错误导致疯狂重启）。

.PARAMETER WeChatWaitSeconds
    最多等微信进程出现多少秒；超时后仍会启动后端（面板会显示错误，可手动重试）。

.PARAMETER WeChatSettleSeconds
    微信进程出现后，再等多少秒才启动后端，留给微信创建主窗口。

.PARAMETER SkipWeChatWait
    跳过等待微信，立即启动后端（手动调试用）。
#>
[CmdletBinding()]
param(
    [int]$BackoffSeconds = 30,
    [int]$WeChatWaitSeconds = 180,
    [int]$WeChatSettleSeconds = 15,
    [switch]$SkipWeChatWait
)

$ErrorActionPreference = 'Continue'

$repo = Split-Path -Parent $PSScriptRoot
$py   = if ($env:BOT_PYTHON) { $env:BOT_PYTHON } else { 'C:\Users\22069\anaconda3\envs\pytorch\pythonw.exe' }
$main = Join-Path $repo 'src\main.py'
$logFile = Join-Path $repo 'wechatauto_logs\backend-loop.log'

if (-not (Test-Path $py))   { throw "找不到 Python：$py（可用 BOT_PYTHON 环境变量指定）" }
if (-not (Test-Path $main)) { throw "找不到后端入口：$main" }

function Write-LoopLog {
    param([string]$Message)
    $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
    try {
        $dir = Split-Path -Parent $logFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        if ((Test-Path $logFile) -and ((Get-Item $logFile).Length -gt 1MB)) {
            Move-Item -Path $logFile -Destination ($logFile -replace '\.log$', '.1.log') -Force
        }
        Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    } catch { }
}

function Test-WeChatRunning {
    return [bool](Get-Process -Name 'Weixin' -ErrorAction SilentlyContinue)
}

# ---- 等微信就绪：见 .DESCRIPTION 里的竞态说明 ----
if ($SkipWeChatWait) {
    Write-LoopLog 'SkipWeChatWait 已指定，跳过等待微信。'
} elseif (Test-WeChatRunning) {
    Write-LoopLog "微信已在运行；等待 $WeChatSettleSeconds 秒让主窗口稳定后启动后端。"
    Start-Sleep -Seconds $WeChatSettleSeconds
} else {
    Write-LoopLog "未发现微信进程，最多等待 $WeChatWaitSeconds 秒……"
    $deadline = (Get-Date).AddSeconds($WeChatWaitSeconds)
    while (-not (Test-WeChatRunning) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
    }
    if (Test-WeChatRunning) {
        Write-LoopLog "微信已出现；再等 $WeChatSettleSeconds 秒后启动后端。"
        Start-Sleep -Seconds $WeChatSettleSeconds
    } else {
        Write-LoopLog "等待微信超时（$WeChatWaitSeconds 秒）；仍尝试启动后端，失败时可在面板手动重试。"
    }
}

# ---- 要拉起的目标：优先桌面壳（批次 1 起它才是「软件」本体），没有就退回裸后端 ----
# 壳自己会拉起并接管 Python 后端，所以这里只管壳一个进程。
# BOT_SHELL_EXE 是给测试/调试用的覆盖点：指向一个 stub exe 就能验证看门狗的退出码分支
# （0 → 停止循环，非 0 → 退避重启），不必真的去点托盘图标。
$appExe = if ($env:BOT_SHELL_EXE) { $env:BOT_SHELL_EXE } else { Join-Path $env:USERPROFILE 'wechat-bot\app\desktop.exe' }

# 壳的 stderr 重定向到文件，原因有二：
#   1) 计划任务是以「隐藏窗口」跑本脚本的，壳的 eprintln! 写进那个隐藏控制台等于丢了，
#      一旦出问题就无从查起；
#   2) release 版是 GUI 子系统（windows_subsystem="windows"），万一 stderr 句柄无效，
#      Rust 的 eprintln! 会直接 panic。这里给它一个必定有效的句柄，把这个风险抹掉。
# 壳会把 Python 子进程的 stdout/stderr 接管后转发到同一路 stderr，所以这个文件里也能看到 [py] 行。
$shellLog = Join-Path $repo 'wechatauto_logs\desktop-shell.log'
$useShell = Test-Path $appExe
if ($useShell) {
    Write-LoopLog "开始拉起桌面壳：$appExe"
} else {
    Write-LoopLog "未找到桌面壳（$appExe），回退为直接拉起后端：$py $main"
}

# ---- 拉壳并等它退出，拿回真正的退出码 ----
# 不能写成 Start-Process -PassThru -RedirectStandardError：那种写法取 .ExitCode 恒为 $null
#（实测 2026-10-05，五种写法对比见 docs/desktop.md §1.5）。而 $ErrorActionPreference='Continue'
# 会把这个属性访问异常静默吞掉，于是「用户主动退出(0)」被判成崩溃、30 秒后又把程序拉回来。
function Start-ShellAndWait {
    param([string]$Exe)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.WorkingDirectory = (Split-Path -Parent $Exe)
    $psi.UseShellExecute = $false
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    # 壳写的是 UTF-8。不显式指定的话，StreamReader 会按「控制台代码页」解码 ——
    # 计划任务的隐藏控制台是 GBK，中文日志就全成乱码了（2026-10-05 实测）。
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    try { $null = $p.Start() }
    catch { Write-LoopLog "拉起桌面壳失败：$($_.Exception.Message)"; return -1 }
    # 必须持续把 stderr 抽走：管道缓冲区一满，子进程写日志时就会永久卡死。
    # ReadToEndAsync 内部就是后台读取，等价于一个抽水线程。
    $errTask = $p.StandardError.ReadToEndAsync()
    $p.WaitForExit()
    $code = $p.ExitCode
    $errText = ''
    try { $errText = $errTask.Result } catch { }
    $null = Save-ShellLog -Text $errText -Code $code
    return $code
}

function Save-ShellLog {
    param([string]$Text, [int]$Code)
    try {
        $dir = Split-Path -Parent $shellLog
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        if ((Test-Path $shellLog) -and ((Get-Item $shellLog).Length -gt 1MB)) {
            Move-Item -Path $shellLog -Destination ($shellLog -replace '\.log$', '.1.log') -Force
        }
        $nl = [Environment]::NewLine
        $head = '===== 桌面壳退出：' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '，退出码 ' + $Code + ' ====='
        Add-Content -LiteralPath $shellLog -Value ($nl + $head) -Encoding UTF8
        if ($Text) { Add-Content -LiteralPath $shellLog -Value $Text -Encoding UTF8 }
    } catch { }
}

# 已经有壳在跑就不再拉第二个：两个壳会抢 8000 端口（ControlServer 刻意关掉了
# SO_REUSEADDR，后启的绑定失败直接退出），看门狗于是进入"失败-重试"循环。
# 触发场景：用户双击桌面「启动自动回复」时程序其实已经在跑，或手动开了 desktop.exe。
# 这里选择"已经有就不再管"：宁可少一个看门狗，也不要重复拉壳或把用户主动的退出复活。
if ($useShell) {
    $alive = @(Get-Process -Name 'desktop' -ErrorAction SilentlyContinue)
    if ($alive.Count -gt 0) {
        Write-LoopLog "桌面壳已在运行（PID=$($alive[0].Id)），本次不再重复拉起；看门狗退出。"
        exit 0
    }
}

while ($true) {
    $started = Get-Date
    if ($useShell) {
        $code = Start-ShellAndWait -Exe $appExe
    } else {
        $proc = Start-Process -FilePath $py -ArgumentList @($main) -WorkingDirectory $repo -PassThru
        $proc.WaitForExit()
        $code = $proc.ExitCode
    }
    $ran = [int]((Get-Date) - $started).TotalSeconds

    # 退出码 0 = 用户从托盘点了「退出」。这是明确的意图，不能"贴心"地再拉起来 ——
    # 否则点了退出，过 30 秒它又回来了（对应 docs/plan-desktop-app.md 的 V10 与 D4）。
    # 下次登录时计划任务会重新运行本脚本，那天照常自启。
    if ($useShell -and $code -eq 0) {
        Write-LoopLog "桌面壳正常退出（用户主动退出，运行了 $ran 秒）；看门狗就此停止，下次登录时再自启。"
        # 必须显式 exit 0：本脚本由计划任务拉起，而任务配了 RestartCount=3（失败重试）。
        # 不给 0 的话，调度器会把"用户点了退出"当成脚本失败，约 30 秒后又把程序拉起来 ——
        # 就是「点了退出它自己又回来了」（2026-10-05 实测复现，对应 V10 / D4）。
        exit 0
    }

    Write-LoopLog "桌面壳异常退出（exit=$code，运行了 $ran 秒）；$BackoffSeconds 秒后重新拉起。"
    Start-Sleep -Seconds $BackoffSeconds
}
