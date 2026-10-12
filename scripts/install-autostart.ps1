<#
.SYNOPSIS
    注册（或卸载）「登录时自动启动后端」的计划任务。

.DESCRIPTION
    为什么用计划任务 + 隐藏窗口，而不是那个桌面壳：

      · 后端本身就是完整的服务（页面 + API），桌面壳只提供本地窗口；
      · 手机远程使用根本不需要窗口，无窗口运行还顺带绕开了 WebView2 那一整套问题；
      · 任务以「交互式登录」身份运行——微信 UI 自动化必须有真实桌面会话，
        所以不能用「不管用户是否登录都运行」（那是 session 0，没有桌面）。

    注意：本任务在「登录时」触发。电脑重启后如果没人登录，后端不会自己起来；
    要做到重启也无人值守，需要另外配置自动登录（本脚本不碰这一项）。

.PARAMETER Uninstall
    删除已注册的计划任务。

.PARAMETER Plan
    只打印将要执行的动作，不实际注册。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Plan
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Plan,
    [string]$TaskName = 'wechat-bot-backend'
)

$ErrorActionPreference = 'Stop'

$repo  = Split-Path -Parent $PSScriptRoot
$loop  = Join-Path $PSScriptRoot 'backend-loop.ps1'
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$who   = "$env:USERDOMAIN\$env:USERNAME"

if ($Uninstall) {
    if ($Plan) { Write-Host "[plan] 将删除计划任务：$TaskName"; return }
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "计划任务 $TaskName 不存在，无需卸载。"; return }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除计划任务：$TaskName"
    return
}

if (-not (Test-Path $loop)) { throw "找不到 $loop" }

$argLine = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $loop + '"'

if ($Plan) {
    Write-Host '[plan] 将注册如下计划任务（本次不落盘）：'
    Write-Host "  名称     : $TaskName"
    Write-Host "  触发     : 用户 $who 登录时"
    Write-Host "  运行身份 : $who （交互式 Interactive，权限 Limited）"
    Write-Host "  执行     : $psExe"
    Write-Host "  参数     : $argLine"
    Write-Host "  工作目录 : $repo"
    Write-Host '  设置     : 允许电池下启动/继续、无执行时限、重复启动则忽略、失败重启 3 次'
    Write-Host ''
    Write-Host '  实际做的事：隐藏窗口里循环拉起 pythonw.exe src\main.py，进程退出就等 30 秒重来'
    Write-Host '  卸载命令  : powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Uninstall'
    return
}

$settingsArgs = @{
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    ExecutionTimeLimit         = [TimeSpan]::Zero
    MultipleInstances          = 'IgnoreNew'
    RestartCount               = 3
    RestartInterval            = (New-TimeSpan -Minutes 1)
    StartWhenAvailable         = $true
}

$action    = New-ScheduledTaskAction -Execute $psExe -Argument $argLine -WorkingDirectory $repo
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $who
$principal = New-ScheduledTaskPrincipal -UserId $who -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet @settingsArgs
$task      = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Write-Host "已注册计划任务：$TaskName"
Write-Host '  卸载：powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Uninstall'
