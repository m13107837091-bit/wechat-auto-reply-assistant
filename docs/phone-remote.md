# 手机远程使用手册（无窗口常驻 + 跨网络访问）

## 核心结论：你不需要那个桌面壳

后端（`src/main.py`）本身就是完整的服务——页面和 API 都由它提供，桌面壳只负责
"开一个本地窗口"。既然你主要是**用手机**控制，那窗口就是多余的：

| | 桌面壳（desktop.exe） | 无窗口后端（pythonw.exe） |
| --- | --- | --- |
| 手机能开面板 | ✅ | ✅ 一样 |
| 依赖 WebView2 | ✅ 是（本机还被工作区降权坑过） | ❌ 完全不依赖 |
| 关掉窗口后还在跑 | ❌ 退出即回收后端 | ✅ 后台常驻 |
| 资源占用 | 多一个 Chromium | 只有 Python |

已验证：工作区外的 `python.exe` 跑工作区内的脚本，写 `%TEMP%` 成功——
所以无窗口运行不受那条"工作区降权"影响。

## 一次性配置

### 第 1 步：注册登录自启（隐藏窗口）

~~~powershell
# 先看它要做什么（不落盘）
powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Plan

# 确认后注册
powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1

# 想撤销随时可卸载
powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1 -Uninstall
~~~

注册的任务叫 `wechat-bot-backend`：**你登录时**在隐藏窗口里拉起后端；
后端若退出，等 30 秒自动重来。后端日志写在 `wechatauto_logs\app_YYYYMMDD.log`，
循环脚本自己的日志写在 `wechatauto_logs\backend-loop.log`。

登录时后端与微信（`HKCU\...\Run` 里的自启项）几乎同时启动。`backend-loop.ps1` 会
**先等微信进程出现、再等 15 秒**才拉后端，避免后端抢跑导致连接失败
（失败时 `main.py` 不会自动重试）。

> ⚠️ 触发条件是"登录时"。**电脑重启后如果没人登录，后端不会自己起来。**
> 要做到重启也无人值守，需要在 `netplwiz` 或注册表配置自动登录——本脚本不碰这一项。

### 第 2 步：装 Tailscale（这一步必须在电脑和手机上各做一次）

图书馆不在你家局域网里，直接连 `192.168.0.100:8000` 是连不通的。Tailscale 把两台设备
放进同一个私有网络，之后手机在任何网络下都能直接开面板，而且不用动路由器。

电脑上（winget 已可用）：

~~~powershell
winget install --id Tailscale.Tailscale -e
# 注意大小写：ID 必须是 Tailscale.Tailscale。全小写的 tailscale.tailscale 配 -e
#（精确匹配）会报「找不到与输入条件匹配的程序包」。
~~~

装完启动 Tailscale、用你的账号登录。手机上装 Tailscale App（应用商店搜 Tailscale），
**登录同一个账号**。

然后查电脑的 Tailscale 地址：

~~~powershell
tailscale ip -4        # 形如 100.x.y.z
~~~

### 第 3 步：保持常醒（笔记本必须插电）

当前电源设置：**插电（AC）永不待机**，但**用电池 15 分钟就睡**（`0x384` = 900 秒）。
所以无人值守期间必须一直插着电。更保险的话把电池下也设成不睡：

~~~powershell
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-dc 0
~~~

另外确认**合盖动作**不是"睡眠"：控制面板 → 硬件和声音 → 电源选项 →
选择关闭盖子的功能 → 插电和电池都设为"不采取任何操作"。

## 手机上怎么用

浏览器打开。`computer333` 是 Tailscale 的 MagicDNS 名字（实测可用），
等价写法是 `tailscale ip -4` 输出的 `100.x.y.z`：

~~~text
http://computer333:8000/?token=<UI_TOKEN>
~~~

`UI_TOKEN` 在仓库根的 `.env` 里。面板第一次打开会把令牌收进 localStorage 并从地址栏
抹掉，之后 `http://computer333:8000/` 直接就能开。

> ⚠️ 令牌是按「来源」存的：`computer333:8000` 和 `100.x.y.z:8000` 是两个来源。
> **固定用一个地址**，换地址就得重输令牌。

手机上建议：把 Tailscale 设为「始终开启的 VPN」，再把上面的地址**添加到主屏幕**，
之后每天就是「点一下图标」。

### 添加到主屏幕（现在有图标了）

Chromium 内核的浏览器（Chrome / Edge / 华为浏览器 / 小米浏览器）打开面板后：

1. 右上角「⋮」→「**添加到主屏幕**」/「安装应用」；
2. 图标是绿底白气泡里的「三」，名字「自动回复」；
3. 点图标直接进面板，**没有地址栏**（全屏），观感跟 App 一样。

> 令牌按「来源」存在 localStorage 里，所以只要一直用同一个地址（推荐 `computer333:8000`），
> 装出来的图标就永远是已登录状态。换地址（例如改用 IP）要重新输一次令牌。

防火墙已经有现成规则 `wechat-bot-ui`（TCP 8000 / Allow / 配置文件 Any），不用再配。

## 日常流程

1. 电脑在家插电开着，你登录着 → 后端自动在跑。
2. 你在图书馆，手机点开书签 → 面板。
3. 面板里开关自动回复，和在家操作完全一样。
4. 关掉手机浏览器不影响后端，它继续跑。
5. 万一在电脑托盘点过「退出」：双击桌面上的「**启动自动回复**」图标就行（约 20 秒后可用）。
   只要没点退出，开机登录后它会自己启动，不用管。

## 重启电脑后会怎样（已实测）

| 时刻 | 发生什么 |
| --- | --- |
| 开机 | Tailscale 服务自动启动（启动类型 `Automatic`，实测确认） |
| 你登录进桌面 | 计划任务 `wechat-bot-backend` 触发 → 运行 `scripts\backend-loop.ps1` |
| 之后几秒 | 循环脚本用 `pythonw.exe` 拉起 `src\main.py`，开始监听 `0.0.0.0:8000` |
| 约 45 秒后 | 连上微信；**自动回复保持暂停**（`.env` 里 `BOT_AUTOREPLY_ON_START=0`），等你点开启 |

**你不需要运行任何 .cmd 文件** —— 那两个 .cmd 只是手动启动用的。

后端万一崩了或被关掉也无所谓：`backend-loop.ps1` 每 30 秒会重新拉起它。

### ⚠️ 前提：必须真的登录

本机**没有配置自动登录**（`AutoAdminLogon` 未设置）。所以：

- 重启后你**输入密码登录进桌面** → 任务触发，后端自动起来，你什么都不用做 ✅
- 重启后停在登录界面、没人输密码 → 任务不会触发，后端不会起 ❌

如果希望「没人管也能跑」（例如 Windows 更新半夜自己重启），就得配自动登录——
那要把密码交给系统保存，安全性会下降一点。

## ⚠️ 两个必须知道的点

1. **自动回复默认是「暂停」的**（本机已按此配置）。后端启动后只连微信、不回复，
   要等你在手机面板里点「开启」才开始替你回消息——这样你不在场时它不会擅自发消息。

   开关在仓库根的 `.env` 里（由 `src/main.py` 读取；`1` = 启动即开，即历史行为）：

   ~~~ini
   BOT_AUTOREPLY_ON_START=0
   ~~~
2. **面板是明文 HTTP**，只有 32 位令牌保护。Tailscale 那层是加密的，所以
   走 Tailscale 没问题；**千万不要**改成路由器端口转发直接暴露到公网。

## 故障对照

| 现象 | 检查 |
| --- | --- |
| 手机打不开面板 | 电脑上 `tailscale ip -4` 有没有地址；手机 Tailscale 是否登录同一账号 |
| 电脑上打开 `http://127.0.0.1:8000` 也不行 | 后端没跑：查 `Get-NetTCPConnection -LocalPort 8000 -State Listen` |
| 后端反复退出 | 看 `wechatauto_logs\app_YYYYMMDD.log` 末尾；backend-loop 每 30 秒重试一次 |
| 重启电脑后连不上 | 因为没配自动登录，没人登录 → 任务没跑。手动登录一次即可 |
| 面板一直转圈 | 后端在连微信（正常要 50 秒左右），或 `微信没开/没登录` |
