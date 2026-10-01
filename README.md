# 🤖 微信自动回复聊天助手（WeChat Auto-Reply Assistant）

一个跑在 **Windows 版 PC 微信**上的 AI 自动回复助手：监听私聊 / 群聊新消息，调用大模型（DeepSeek / OpenAI / Ollama 等任意 OpenAI 兼容接口）生成回复并自动发送。轻量、可配置、易于二次开发。

> ## ⚠️ 重要免责声明（务必先读）
>
> 本项目通过 **UI 自动化**操作个人微信，**并非微信官方授权的接口**，可能违反《微信软件许可及服务协议》，存在**账号被限制或封禁**的风险。请务必：
>
> - 使用 **小号 / 测试号**，不要用主力账号；
> - 遵守内置的**回复限速**，避免群聊刷屏；
> - 仅用于学习与个人研究，**自行承担一切后果**。
>
> 本项目仅供技术学习交流，作者不对任何封号、数据丢失等问题负责。

## ✨ 特性

-  私聊自动回复、群聊**被 @ 自动回复**（可配置）
-  任意 OpenAI 兼容大模型：DeepSeek / OpenAI / Ollama / 通义 / Kimi …
-  白名单 / 黑名单、关键词触发、回复限速、夜间静默
-  可配置人设 / 说话风格 + 按需联网搜索（天气 / 新闻等实时问题有据可答，可选）
-  按会话的多轮上下文记忆（LRU + TTL）
-  大模型异常时兜底话术或静默，主循环不崩
-  密钥走 `.env`（不入库），行为策略走 `config.yaml`

##  工作原理

```
PC 微信 ──(本地库解密 + UIA 发送)──▶ wechatauto ──▶ 收到新消息
                                        │
                                   过滤策略（Bot）
                       ┌────────────────┼────────────────┐
                白名单/黑名单      群聊@/关键词      限速/夜间静默
                        │
                   组装上下文（系统提示 + 会话记忆 + 本条消息）
                        │
                   大模型（OpenAI 兼容接口）
                        │
                   回复文本 ──▶ wechatauto ──▶ 发送到微信
```

## 📚 参考项目

本项目在设计时参考了以下开源项目（按 Star 排序）：

| 项目 | 说明 |
| --- | --- |
| [zhayujie/CowAgent](https://github.com/zhayujie/CowAgent)（原 chatgpt-on-wechat） | 多通道·多模型 AI 助手，功能与架构设计的核心参考 |
| [fanyuantaier/wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) | 本项目采用的底层微信接入库（微信 4.x：消息库解密读取 + UIA/坐标-OCR 发送） |
| [cluic/wxauto](https://github.com/cluic/wxauto) | 旧版接入库，仅支持微信 3.9.x（本项目已弃用） |
| [lich0821/WeChatFerry](https://github.com/lich0821/WeChatFerry) | hook 微信 4.x 的方案（仅参考功能清单，仓库已归档、封号风险高） |
| [zynsync/Zyn-iLink-ChatBox](https://github.com/zynsync/Zyn-iLink-ChatBox) | 基于官方 iLink 接口的思路参考 |

## 🗂 目录结构

```
wechat-auto-reply-assistant/
├── README.md
├── LICENSE                 # MIT
├── config.yaml             # 行为策略（白名单/限速/夜间静默等）
├── .env.example            # 密钥模板（复制为 .env）
├── requirements.txt
├── requirements-dev.txt
├── pytest.ini
├── src/
│   ├── main.py             # 入口
│   ├── config.py           # 加载 .env + config.yaml
│   ├── wechat_client.py    # wechatauto 封装（收/发消息）
│   ├── llm.py              # OpenAI 兼容大模型客户端
│   ├── bot.py              # 过滤 + 组装 + 回复逻辑
│   ├── controller.py       # 可启停的机器人核心（供控制面板调用）
│   ├── webui.py            # 本地/远程 Web 控制面板（含可选令牌鉴权）
│   └── session.py          # 会话记忆
├── web/
│   └── index.html          # 控制面板页面（移动端自适应）
├── desktop/                # Tauri 桌面壳（可选，启动时拉起 Python 后端）
└── tests/
    └── test_bot.py         # 核心逻辑单元测试
```

## 🚀 快速开始

### 前置条件

- Windows 系统，已安装并**登录 PC 微信**
- Python 3.10+
- 一个大模型的 API Key（DeepSeek 等），或本机 Ollama

### 1. 安装依赖

```bash
cd wechat-auto-reply-assistant
python -m venv .venv
.venv\Scripts\activate        # Windows PowerShell
pip install -r requirements.txt
```

### 2. 配置

```bash
copy .env.example .env
# 编辑 .env，填入你的 LLM API Key（以及 base_url / model）
# 按需编辑 config.yaml（白名单、限速、群聊@、夜间静默等）
```

`.env` 关键项：

| 变量 | 说明 |
| --- | --- |
| `LLM_PROVIDER` | `openai`（任意 OpenAI 兼容接口）或 `ollama`（本地） |
| `LLM_BASE_URL` | 接口地址，DeepSeek 为 `https://api.deepseek.com` |
| `LLM_API_KEY` | API Key |
| `LLM_MODEL` | 模型名，如 `deepseek-chat` |
| `SEARCH_PROVIDER` | 联网搜索后端，目前支持 `tavily`（可选，留空则关闭搜索） |
| `SEARCH_API_KEY` | Tavily 的 API Key（`https://tavily.com` 有免费额度） |
| `UI_HOST` | 控制面板监听地址：`127.0.0.1`（默认，仅本机）/ `0.0.0.0`（手机等外部设备可访问） |
| `UI_TOKEN` | 远程访问令牌；设了才启用鉴权，`UI_HOST=0.0.0.0` 时**必须**设 |

### 3. 运行

```bash
python -m src.main
```

也可以直接在 **PyCharm 里右键运行 `src/main.py`**（已支持脚本方式启动，无需额外配置）。

启动后程序会连接 PC 微信并开始监听；给机器人发消息即可测试，`Ctrl+C` 退出。

> ⚠️ 运行期间请**保持 PC 微信窗口可用**（不要最小化到托盘、不要锁屏），并确保微信版本与 wechatauto-replica 支持版本匹配（见下）。

## 📱 手机远程控制（可选）

机器人必须留在 PC（依赖电脑版微信登录），但控制面板可以在**手机浏览器**里远程打开——开关自动回复、改人设、填 API Key、看状态。

**1. 在 `.env` 里打开远程监听并设一个令牌：**

```ini
UI_HOST=0.0.0.0
UI_TOKEN=一串随机长令牌
```

令牌生成：`python -c "import secrets;print(secrets.token_urlsafe(24))"`

**2. 放行防火墙（管理员 CMD / PowerShell，只需一次）：**

```bat
netsh advfirewall firewall add rule name="wechat-bot-ui" dir=in action=allow protocol=TCP localport=8000
```

**3. 启动后端，手机打开：**

```bash
python -m src.main
```

- **同一 Wi-Fi**：手机浏览器开 `http://<电脑局域网IP>:8000/?token=<你的令牌>`（电脑 IP 用 `ipconfig` 查）。
- **随时随地（推荐 Tailscale）**：电脑和手机各装 [Tailscale](https://tailscale.com/) 并登录同一账号，手机用电脑的 Tailscale IP（`100.x.x.x`）访问同一地址。零端口映射、全程加密、不暴露公网。
- 也可用 `ngrok http 8000` 快速拿一个临时公网地址（免费版地址每次重启会变，务必靠令牌挡人）。

> 🔐 只要 `UI_HOST=0.0.0.0` 就**务必设 `UI_TOKEN`**：不设则同网/公网任何人都能操控你的机器人。设了令牌后，页面本身可打开，但所有操作都需令牌；令牌会记在手机浏览器里，之后直接开 `http://<IP>:8000/` 即可。只想本机用就保持默认 `UI_HOST=127.0.0.1`（无需令牌）。

## 🎛 配置说明（`config.yaml`）

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `reply.whitelist` | `[]` | 只回复这些对象（备注名/群名）；空表示不限制 |
| `reply.blacklist` | `[]` | 永不回复这些对象 |
| `reply.match_mode` | `exact` | 名单匹配方式：`exact`（精确全名）/ `contains`（包含即可命中） |
| `reply.keyword_trigger` | `[]` | 仅当消息含这些关键词才回复；空表示全部 |
| `reply.max_reply_per_minute` | `20` | 每分钟最大回复数（防封号） |
| `reply.min_interval_seconds` | `1.0` | 两次回复最小间隔（秒） |
| `reply.night_silence` | 开 | 夜间静默时段（23:30–07:00 内不回复） |
| `reply.group.enabled` | `false` | 是否回复群聊（默认关：**只回私聊**；需要回群再改 `true`） |
| `reply.group.only_when_mentioned` | `真` | 群聊仅被 @ 时回复 |
| `reply.group.self_nickname` | `""` | 你的微信昵称，用于精确判断是否「@ 了我」 |
| `reply.friend.enabled` | `true` | 是否回复私聊 |
| `reply.fallback_reply` | 见配置 | 大模型出错时的兜底回复 |
| `session.max_turns` | `8` | 每会话保留的上下文轮数 |
| `session.ttl_seconds` | `3600` | 会话记忆有效期（秒） |
| `persona.name` | `小助手` | 机器人自称 |
| `persona.style` | `自然活泼` | 说话风格（简洁 / 活泼 / 正式 / 幽默 …） |
| `persona.emoji` | `true` | 是否适度使用 emoji |
| `persona.length` | `auto` | 回复长度：`auto` / `short` / `detailed` |
| `search.enabled` | `true` | 是否启用按需联网搜索（仍需 `.env` 配 key） |
| `search.keywords` | 见配置 | 命中这些关键词才触发搜索（保持「适当」、不每条都搜） |

## 🧪 测试

```bash
pip install -r requirements-dev.txt
pytest
```

## 🔌 关于微信接入（wechatauto-replica）

- 本项目基于 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica)（`import wechatauto`，PyPI 包名 `wechatauto-replica`）收发消息，对应微信 **4.1.12+**（作者实测 4.1.15.13）。
  - **读消息**：解密本地消息库（SQLCipher），无需依赖旧的 UIA 控件树，因此支持微信 4.x。
  - **发消息**：UIA + 坐标/OCR 混合发送，运行时请**保持微信窗口可见、不要锁屏**。
- **为什么弃用 wxauto**：旧版 [cluic/wxauto](https://github.com/cluic/wxauto) 依赖微信 3.9.x 的 UIA 控件树，微信 4.x 移除了该树导致「连接失败」；wxauto 也已不在 PyPI 上。
- **微信版本敏感**：若报「连接 PC 微信失败」，请确认微信已登录、窗口可用，且版本在 4.1.12 以上；升级微信后需重新运行（密钥缓存会因版本变化失效，程序会自动重新提取）。
- 仅支持**文本消息**自动回复；图片、语音、文件等消息暂不处理（可自行扩展）。
- 机器人自己发的消息、系统提示会被自动忽略，避免「自己回自己」造成死循环。
- 已知限制：当前版本未从消息里解析「@ 提及」信息，因此 `group.only_when_mentioned` 对 4.x 后端不可靠；默认 `group.enabled: false`，如需回群请留意。

## 🛠 常见问题

- **连不上微信**：请确认 PC 微信已登录、窗口可用、微信版本为 4.1.12+（与 wechatauto-replica 匹配）。
- **只想在某个群生效**：先把 `reply.group.enabled` 改为 `true`，再把群名填进 `reply.whitelist`。
- **只想回复/不回复某几个人**：填 `reply.whitelist` / `reply.blacklist`，备注名带后缀时把 `reply.match_mode` 改为 `contains`。
- **不想在晚上打扰**：保持 `night_silence.enabled: true`（默认开启）。
- **API 报错**：检查 `.env` 的 key / model / base_url 是否正确、是否有额度。

## 📄 License

[MIT](./LICENSE) © wechat-auto-reply-assistant contributors

> 再次提醒：请遵守相关服务的条款与《微信软件许可及服务协议》，谨慎使用，风险自负。