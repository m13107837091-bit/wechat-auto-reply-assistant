# 桌面壳（Tauri v2）说明

> 本文记录 `desktop/` 这个桌面壳的进程模型、启动时序、WebView2 数据目录策略，
> 以及本机一条**会直接决定项目能否运行**的环境约束。

## 1. 进程模型

~~~text
desktop.exe (Rust / Tauri v2)
├─ 系统托盘 ──> 菜单 / 三态图标 / 气泡提示        （批次 1 新增）
├─ 打开 ─────> WebView2 窗口 → http://127.0.0.1:8000/?token=<UI_TOKEN>
└─ 拉起 ─────> python.exe  <仓库根>/src/main.py   （后端：页面 + API，stdlib http.server）
~~~

壳**不含任何业务逻辑**，只负责三件事：托盘、窗口、后端子进程的生命周期。
页面与 API 全部由 Python 后端（`src/webui.py`）提供。

后端句柄存放在 `Backend(Mutex<Option<Child>>)` 里交给 Tauri 状态管理：
`Drop` 会兜底回收，建窗失败时也会显式 `kill` + `wait` —— 两者都走 `kill_child`，
因为 std 的 `Child` **不会**在 Drop 时杀子进程，漏掉就会留下孤儿 python 占着 8000 端口。

### 1.1 托盘（批次 1）

| 项 | 行为 |
| --- | --- |
| 菜单 | 打开控制面板 / 暂停·恢复自动回复 / 打开日志目录 / 退出 |
| 左键单击 | 直接打开面板（菜单只在右键弹） |
| 图标 | 绿 = 运行中，灰 = 已暂停，红 = 连不上后端或 `last_error` 非空 |
| 提示文字 | 随状态变化；出错时直接带上 `last_error` 原文 |
| 刷新 | 后台线程每 2 秒拉一次 `/api/status` |
| 关窗 | **不退出**，只隐藏窗口（`prevent_close` + `hide`） |
| 退出 | 回收后端子进程后 `app.exit(0)` |

**退出码是契约**：`0` = 用户主动退出，非 `0` = 崩溃。`scripts/backend-loop.ps1`
据此决定"停止看门狗"还是"退避后重启"—— 所以从托盘点了退出，不会被过一会儿偷偷拉起来。

图标由 `scripts/make-tray-icons.py` 生成（纯标准库画 PNG，不依赖 Pillow），
用 `include_bytes!` 内嵌进 exe，运行时不依赖任何外部文件。

### 1.2 端口已被占用时不再起第二个后端

壳启动时先探一下 `127.0.0.1:8000`：

- **没人听** → 照旧拉起自己的 Python 子进程，并记进 `Backend`；
- **已经有人听** → 说明后端已经跑着了（例如看门狗先拉起过），这时**直接复用**，
  不 spawn，也**不**记进 `Backend`（那不是本壳的子进程，本壳无权杀它）。

这条很关键：`webui.py` 关掉了 `SO_REUSEADDR`，第二个后端会在 `bind` 时以
`[WinError 10048]` 退出。复用它既少一个短命进程，也避免日志被误读成"壳把后端搞崩了"。

### 1.3 ⚠️ 窗口标题不能含「微信」

本壳主窗口的标题是 **「自动回复助手」**，不是「微信自动回复」。这不是取名偏好，是硬约束。

`wechatauto` 定位微信主窗口的办法是「给所有可见顶层窗口打分，取最高分，门槛 5」
（`site-packages/wechatauto/guia.py` 的 `_find_main_window` / `_looks_like_main_window`）：

| 特征 | 加分 |
| --- | --- |
| 标题含 `微信` / `Weixin` / `WeChat` | **+5** |
| 类名前缀 `Qt51514QWindowIcon` | +4 |
| 进程名 `weixin.exe` | +3 |
| 尺寸够大（宽或高 ≥ 800，或竖屏高 ≥ 600） | +2 |

**光靠标题就 +5，刚好踩到门槛。** 实测（2026-10-05 13:20）：

| 窗口 | 分数 |
| --- | --- |
| 真微信 `hwnd=133096`（类名 `Qt51514QWindowIcon`，进程 `weixin.exe`） | 14 |
| 本壳 `hwnd=197194`（类名 `Tauri Window`，进程 `desktop.exe`） | 7 |

只要微信主窗口那一刻**不可见**（最小化到托盘、正在重建、还没登录），本壳窗口就成了
唯一过门槛的候选 —— 后端会绑定到它，之后所有 UI 自动化点击都会打进控制面板。

**这次是真的踩到了**：后端日志出现 `WeChatGUI 初始化成功：hwnd=197194`
（正是 `desktop.exe` 的窗口句柄），并且 `starting` 一直停在 True。

改成「自动回复助手」后，本壳只剩尺寸 +2，**永远过不了 5 分门槛**。
以后若要改标题，务必保持不含那三个关键字。

### 1.4 隐藏后端子进程的控制台窗口

release 版是 GUI 子系统（`windows_subsystem = "windows"`），由它拉起的
`python.exe` 是控制台程序，默认会**新建一个可见的黑窗口**。
实测（2026-10-05 13:20:26）：`python` 进程与一个新的 `conhost` 同秒出现，就是这个窗口。

所以 spawn 时加了 `CREATE_NO_WINDOW`（`0x0800_0000`）。它只是不给子进程分配控制台，
**不影响**我们已经用管道接管的两路输出；stdin 也一并设为空设备。

### 1.5 ⚠️ 取子进程退出码：别用 `Start-Process -RedirectStandardError`

`scripts/backend-loop.ps1` 靠壳的退出码区分「用户主动退出」与「崩了」，所以这个码必须真的拿到。
踩过的坑是：加上 `-RedirectStandardError` 之后，`.ExitCode` **恒为 `$null`**。

~~~powershell
# ❌ 这样写拿不到退出码
$p = Start-Process $exe -PassThru -RedirectStandardError $log
$p.WaitForExit()
$code = $p.ExitCode      # $null
~~~

实测五种写法（`cmd /c exit 7`，2026-10-05）：

| 写法 | 拿到 |
| --- | --- |
| `Start-Process -PassThru` + `WaitForExit()` | 7 ✅ |
| `Start-Process -PassThru -Wait` | 7 ✅ |
| `Start-Process -PassThru -RedirectStandardError` + `WaitForExit()` | **`$null`** ❌ |
| .NET `Process.Start`（`UseShellExecute=false`） | 7 ✅ |
| .NET `Process.Start` + `RedirectStandardError` | 7 ✅ |

更阴的是：属性访问抛的是**非终止错误**，而脚本里写着 `$ErrorActionPreference = 'Continue'`，
于是它被静默吞掉，日志里只剩一个空的 `exit=`。后果就是 2026-10-05 13:30 那次：
用户点了「退出」，30 秒后程序自己又回来了。

现在的写法走 .NET API，并用 `ReadToEndAsync()` 抽走 stderr。**抽水是必须的**：
不抽，管道缓冲区（约 4 KB）一满，子进程写日志时会永久卡死。

验证办法（脚本里留了 `BOT_SHELL_EXE` 覆盖点）：把它指向一个 stub exe，
退出码 0 → 日志「正常退出」且循环停止；退出码 5 → 日志「异常退出（exit=5）」并退避重启。
两条都已回路验证通过。

## 2. 启动时序

| 步骤 | 位置（`desktop/src-tauri/src/lib.rs`） | 说明 |
| --- | --- | --- |
| 1 | `warn_if_non_ascii_exe_path()` | 路径含非 ASCII 时提前警告：WebView2 在该情况下建窗会报 `0x8000FFFF` |
| 2 | `python_exe()` | 优先环境变量 `BOT_PYTHON`，否则回退 `C:\Users\22069\anaconda3\envs\pytorch\python.exe` |
| 3 | `project_root()` / `backend_main_py()` | 以 `CARGO_MANIFEST_DIR` 上跳两级定位仓库根，后端入口 `<仓库根>/src/main.py` |
| 4 | `Command::new` + `spawn_output_forwarders` | 子进程 `cwd` = 仓库根；stdout/stderr **管道化**，逐行加 `[py]` 前缀转发 |
| 5 | `wait_for_backend(15s)` | 轮询 `127.0.0.1:8000`，最多 15 秒 |
| 6 | `read_ui_token()` | 从 `<仓库根>/.env` 读 `UI_TOKEN`，拼进 URL 查询参数（令牌不进代码、不进仓库） |
| 7 | 候选目录循环 | 依次尝试 3 个 WebView2 用户数据目录，任一成功即开窗 |
| 8 | `app.manage(Backend(...))` / `exit(1)` | 成功则交棒给 Tauri；全失败则回收后端、打印可操作提示、以 1 退出 |
| 9 | `TrayIconBuilder` + `Menu::with_items` | 建托盘与菜单，见 `1.1 |
| 10 | `spawn_tray_status_poller` | 起后台线程，每 2 秒按 `/api/status` 刷新图标、提示与菜单文案 |

## 3. WebView2 数据目录的三级候选

| 顺序 | 目录 | 何时才会用到 |
| --- | --- | --- |
| ① | Tauri 默认：`%LOCALAPPDATA%\com.example.wechatbot\EBWebView` | 正常情况就该由它命中 |
| ② | `%USERPROFILE%\wechat-bot\webview` | 默认位置不可写 |
| ③ | `%TEMP%\wechat-bot\webview-<pid>` | 前两个都被占用 |

实测（2026-10-01 16:43:03，成功那次）：**候选 ① 一次命中**，② 与 ③ 都没有被创建。

也就是说，这套降级链在健康环境下**根本不会触发**。它此前反复生效，全部是下面第 4 节
那条环境问题导致的——数据目录策略本身没有问题。

## 4. ⚠️ 本机约束：必须从工作区之外启动

### 现象

只要 `desktop.exe` 位于 DSH 工作区内（`…\deepseek-harness\workspace\…`），
三个候选数据目录**全部**建不出来，窗口永远打不开：

~~~text
[window] 用数据目录「Tauri 默认位置」建窗失败：Io(code: 5, kind: PermissionDenied, "拒绝访问。")
[window] 用数据目录「C:\Users\22069\wechat-bot\webview」建窗失败：0x800700AA 请求的资源在使用中
警告：创建 WebView2 数据目录失败 C:\Users\22069\AppData\Local\Temp\wechat-bot\webview-XXXX：拒绝访问。 (os error 5)
[window] 3 个数据目录全部失败，最后一次错误：Io(code: 5, kind: PermissionDenied)
[backend] 建窗失败，已回收 Python 后端子进程
~~~

### 关键对照

| 条件 | 结果 |
| --- | --- |
| 用户的**普通**终端（`whoami /groups` 显示 `Medium Mandatory Level` `S-1-16-8192`）里用 `New-Item` 建 `%TEMP%` / `%LOCALAPPDATA%` / `wechat-bot` 目录 | ✅ 全部成功 |
| 在**同一个**终端里直接 `& "…\workspace\…\target\debug\desktop.exe"` | ❌ 三条候选全失败 |
| 把**同一个 exe** 复制到 `C:\Users\22069\desktop-test.exe`（工作区外）再启动 | ✅ 窗口正常打开，后端 52 秒就绪 |
| 重启电脑之后再试工作区内那个 exe | ❌ 依旧失败 |

结论：**决定因素是可执行文件所在的路径**——不是 ACL、不是数据目录、不是锁、不是残留进程。

### 为什么 `0x800700AA` 会骗人

`0x800700AA`（"请求的资源在使用中"）通常意味着数据目录被另一个实例独占。
但在这里它**不是真的被占用**：被降权的进程写不了锁文件，Chromium 把它报成了"使用中"。

所以看到这个错误码时，应当**先排除"路径 / 权限"这一层，再去找残留实例**。
本仓库的 `webview_data_dir()` 注释里原来只写了"被别的实例占着"，那条经验在这台机器上
不足以定位问题。

### 两种解法

~~~powershell
# 零重建、最快：复制已构建好的 exe 到工作区外再启动
pwsh -File scripts/run-outside.ps1

# 让 npm run tauri dev 直接可用：把构建产物输出到工作区外
# （首次全量重编，几分钟；之后有增量缓存）
pwsh -File scripts/run-outside.ps1 -Mode target-dir
~~~

> 注：只在 `target\debug\desktop.exe` 这个**可执行文件**的路径上做文章即可——
> 源码留在工作区内没有任何影响。

## 5. 故障对照表

| 现象 | 含义 | 处置 |
| --- | --- | --- |
| `Io(code 5, PermissionDenied)` "拒绝访问。" | 数据目录对当前进程不可写 | 确认 exe **不在工作区内** |
| `0x800700AA` 资源在使用中 | 目录被独占，**或** 进程写不了该目录 | 先排除路径/权限，再结束残留的 `desktop.exe` / `msedgewebview2` |
| `0x8000FFFF` 灾难性故障 | 目录建不出 / 不可写 / 旧 profile 损坏 / exe 路径含非 ASCII | 删掉旧 `EBWebView`、换纯 ASCII 路径 |
| 窗口开了但面板一直转圈 | 后端没就绪或 8000 端口被占 | 看后端日志（见第 6 节） |
| 中文日志乱码 | 控制台代码页不是 UTF-8 | 在终端执行 `chcp 65001` |

## 6. 日志

- **壳**：`eprintln!` → stderr，前缀 `[window]` / `[backend]`
- **后端**：Python `logging` → 被壳接管、加 `[py]` 前缀转发到同一控制台；
  同时落盘 `<仓库根>/wechatauto_logs/app_YYYYMMDD.log`（1MB 轮转留 5 份，
  见 `src/log_rotate.py` 与 plan-desktop-app.md 批次 3）

`[py]` 前缀是 2026-10-01 新增的。此前子进程直接继承壳的控制台句柄，两个进程并发写
同一个句柄，会在任意字节边界互相覆盖——**不只是排版乱，整行会真的丢失**
（实测 `[guia.py:848] 已加载布局校准（wide）…` 那行永久消失，相邻两行被拼成一行），
恰好破坏最需要的诊断信息。现在两路输出收进管道、由壳单线程转发，
每次写整行时持有 stderr 全局锁，因此不再交织。

## 7. 附录：本次排查时间线

| 时间 | 事件 |
| --- | --- |
| 09-26 | `wechatauto` 在 `%TEMP%` 下的产物最后更新，此后未见再写入 |
| 09-28 00:51 | 工作区**外**的 `C:\Users\22069\wechat-bot\app\wechat-bot.exe` 运行成功，建出 `wechat-bot\webview`；17:04 WebView2 profile 最后写入 |
| 10-01 15:29 | 工作区内 `npm run tauri dev` 失败（当时 `lib.rs` 还是旧版） |
| 10-01 15:36 | `lib.rs` 加入"Tauri 默认位置"候选（仓库里唯一未提交的改动） |
| 10-01 16:19 / 16:32 | 工作区内直接运行 exe，三条候选全失败 |
| 10-01 16:30 | 用户 Medium 终端探针：`%TEMP%` / `%LOCALAPPDATA%` / `wechat-bot` 建目录**全部成功** |
| 10-01 16:40 | 重启电脑后，工作区内 `npm run tauri dev` **依旧失败** |
| 10-01 16:41 | 把同一个 exe 复制到工作区外 → **窗口打开** |
| 10-01 16:42 | 后端连接微信成功，`启动完成，耗时 52.0s`；面板持续轮询 `/api/status` |
| 10-01 16:43 | 验证：成功那次命中的是候选 ①（`%LOCALAPPDATA%\com.example.wechatbot\EBWebView`） |
| 10-05 10:25 | **真实重启**：计划任务自动触发 → 等微信出现 → 10:26:55 拉起后端，全程无人工干预 |
| 10-05 下午 | 批次 1：加系统托盘（见 `1.1）、看门狗改为按退出码决策（见第 8 节） |

## 8. 维护提示：`.ps1` 必须以 UTF-8 BOM 开头

`scripts/*.ps1` 含中文注释与中文字符串，**必须**以 UTF-8 BOM（`EF BB BF`）开头。

本机 `pwsh` 实际是 **PowerShell 5.1**。无 BOM 时它按系统 ANSI 代码页（本机 GBK）解码
`.ps1`：UTF-8 的中文字节被解成别的字符，其中某些字节会**把紧随其后的右引号当成自己的
续字节吃掉**（例：`）` 的 UTF-8 是 `EF BC 89`，其中的 `89` 是合法 GBK 前导字节，
于是后面的右引号成了它的尾字节）—— 字符串的右引号凭空消失，脚本直接报语法错误。

**2026-10-05 实测**：用编辑工具改完 `backend-loop.ps1` 后 BOM 被剥掉，
`Parser::ParseFile` 报 1 个语法错误；补回 BOM 后立刻变成 0 个。

改完任何 `.ps1`，都要复查 BOM 与语法：

~~~powershell
$p = 'scripts\backend-loop.ps1'
$b = [System.IO.File]::ReadAllBytes($p)
'BOM: ' + (($b[0..2] | ForEach-Object { $_.ToString('X2') }) -join ' ')   # 期望 EF BB BF

# 没有就用带 BOM 的 UTF-8 重写一遍
[System.IO.File]::WriteAllText($p,
    [System.IO.File]::ReadAllText($p, [System.Text.UTF8Encoding]::new($false)),
    [System.Text.UTF8Encoding]::new($true))

$err = $null
[System.Management.Automation.Language.Parser]::ParseFile($p, [ref]$null, [ref]$err) | Out-Null
$err.Count   # 期望 0
~~~
