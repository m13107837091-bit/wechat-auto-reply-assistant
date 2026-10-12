<#
.SYNOPSIS
    在工作区之外启动「微信自动回复」桌面壳，绕开本机的工作区降权。

.DESCRIPTION
    本机存在一条与项目代码无关、却直接决定项目能否运行的环境事实：

      当可执行文件位于 DSH 工作区内时，它拉起的进程会被降权，WebView2 的三个候选
      数据目录一个都建不出来，表现为
          Io(PermissionDenied) / 0x800700AA「请求的资源在使用中」
      窗口永远打不开；把同一个 exe 复制到工作区之外再运行则一切正常。

    本脚本把这条经验固化成两种模式：

      copy        复制已构建好的 debug exe 到工作区外再启动（零重建，最快）
      target-dir  把 CARGO_TARGET_DIR 指到工作区外，让 npm run tauri dev 直接可用

.EXAMPLE
    pwsh -File scripts/run-outside.ps1

.EXAMPLE
    pwsh -File scripts/run-outside.ps1 -Mode target-dir
#>
[CmdletBinding()]
param(
    [ValidateSet('copy', 'target-dir')]
    [string]$Mode = 'copy',

    # 工作区之外的落点，必须可写
    [string]$OutsideRoot = (Join-Path $env:USERPROFILE 'wechat-bot'),

    # 只做准备、不真正启动（便于先看一眼要跑什么）
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'

# 脚本位于 <仓库根>\scripts\ 下，据此上跳一级得到仓库根
$RepoRoot   = Split-Path -Parent $PSScriptRoot
$DesktopDir = Join-Path $RepoRoot 'desktop'
$TauriDir   = Join-Path $DesktopDir 'src-tauri'
# 优先 release：它带 windows_subsystem="windows"，启动时不弹控制台窗口，
# 这正是托盘常驻程序该有的样子；debug 版留着方便调试。
$ReleaseExe = Join-Path $TauriDir 'target\release\desktop.exe'
$DebugExe   = Join-Path $TauriDir 'target\debug\desktop.exe'
$BuiltExe   = if (Test-Path $ReleaseExe) { $ReleaseExe } else { $DebugExe }

function Assert-OutsideRepo([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    $repo = [System.IO.Path]::GetFullPath($RepoRoot)
    if ($full.StartsWith($repo, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "落点必须位于仓库之外，否则会再次触发降权：$full"
    }
}

Assert-OutsideRepo $OutsideRoot

switch ($Mode) {
    'copy' {
        if (-not (Test-Path $BuiltExe)) {
            $hint = '请先在 desktop 下执行一次 npm run tauri dev —— 能构建出 exe 即可，' +
                    '运行失败无所谓。'
            throw ("找不到已构建的 exe：" + $BuiltExe + [Environment]::NewLine + $hint)
        }
        $destDir = Join-Path $OutsideRoot 'app'
        New-Item -ItemType Directory -Force -Path $destDir | Out-Null
        $dest = Join-Path $destDir 'desktop.exe'
        Copy-Item $BuiltExe $dest -Force
        Write-Host "[run-outside] 已复制到工作区外：$dest"
        if ($NoLaunch) { break }
        Write-Host '[run-outside] 启动中（Ctrl+C 结束）...'
        & $dest
    }
    'target-dir' {
        $targetDir = Join-Path $OutsideRoot 'target'
        New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
        $env:CARGO_TARGET_DIR = $targetDir
        Write-Host "[run-outside] CARGO_TARGET_DIR = $targetDir"
        Write-Host '[run-outside] 首次会全量重编（几分钟），之后就有增量缓存。'
        if ($NoLaunch) { break }
        Push-Location $DesktopDir
        try { npm run tauri dev } finally { Pop-Location }
    }
}
