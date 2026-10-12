// 桌面壳：启动时拉起 Python 后端（仓库根 src/main.py），等它在本地端口就绪后打开窗口；
// 关闭窗口时回收后端子进程。页面与 API 都由 Python 后端直接提供，本壳只负责进程生命周期。
use std::io::{BufRead, BufReader, Read, Write};
use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

const BACKEND_PORT: u16 = 8000;

/// 后端子进程句柄，存进 Tauri 状态；无论正常关窗还是 panic，都由 Drop 兜底回收。
struct Backend(Mutex<Option<Child>>);

/// 杀掉并回收槽里的子进程（幂等：取走句柄后重复调用无副作用）。
///
/// 注意 std 的 `Child` **不会**在 Drop 时杀子进程，所以每个提前退出的分支
/// 都必须显式走这里，否则会留下一个孤儿 python 继续占着 8000 端口。
fn kill_child(slot: &mut Option<Child>) {
    if let Some(mut child) = slot.take() {
        let _ = child.kill();
        let _ = child.wait();
    }
}

impl Backend {
    /// 杀掉并回收后端子进程（幂等：杀完句柄置 None，重复调用无副作用）。
    fn kill(&self) {
        let mut slot = self.0.lock().unwrap();
        if slot.is_some() {
            kill_child(&mut slot);
            eprintln!("[backend] 已回收 Python 后端子进程");
        }
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        self.kill();
    }
}

/// 托盘的固定 id：刷新状态时靠它把托盘取回来。
const TRAY_ID: &str = "main-tray";

/// 托盘三态图标 + 那个文案会变的「暂停/恢复」菜单项。
///
/// 存进 Tauri 状态：轮询线程每 2 秒按后端状态取回来更新图标、提示与菜单文案。
struct TrayUi {
    /// [运行中, 已暂停, 出错]
    icons: [Image<'static>; 3],
    toggle: MenuItem<tauri::Wry>,
}

/// 后端 `/api/status` 里我们关心的字段。
#[derive(serde::Deserialize)]
struct BackendStatus {
    #[serde(default)]
    running: bool,
    #[serde(default)]
    last_error: String,
}

/// 向后端发一个本机 HTTP 请求，返回响应体。
///
/// 刻意不引入 HTTP 客户端库：目标只有 127.0.0.1 上的三个固定端点，而后端
/// （http.server）总是带 Content-Length 并把连接关掉，所以「发完读到 EOF、切掉响应头」
/// 就够了。少一个依赖 = 少一份编译期与供应链风险。
fn backend_request(method: &str, path: &str) -> Result<String, String> {
    let token = read_ui_token();
    let mut stream = TcpStream::connect(("127.0.0.1", BACKEND_PORT)).map_err(|e| e.to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .map_err(|e| e.to_string())?;
    stream
        .set_write_timeout(Some(Duration::from_secs(3)))
        .map_err(|e| e.to_string())?;

    let auth = if token.is_empty() {
        String::new()
    } else {
        format!("Authorization: Bearer {}\r\n", token)
    };
    let request = format!(
        "{} {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n{}Content-Length: 0\r\nConnection: close\r\n\r\n",
        method, path, BACKEND_PORT, auth
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|e| e.to_string())?;

    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).map_err(|e| e.to_string())?;
    let text = String::from_utf8_lossy(&raw).into_owned();
    text.split("\r\n\r\n")
        .nth(1)
        .map(|s| s.to_string())
        .ok_or_else(|| "响应缺少正文".to_string())
}

fn fetch_status() -> Result<BackendStatus, String> {
    let body = backend_request("GET", "/api/status")?;
    serde_json::from_str::<BackendStatus>(&body).map_err(|e| e.to_string())
}

/// 显示并聚焦主窗口（托盘「打开控制面板」与左键单击都走这里）。
fn show_main_window(app: &AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

/// 托盘点「暂停/恢复」：先问后端当前状态，再发相反的那个请求。
fn toggle_auto_reply(app: &AppHandle) {
    let running = fetch_status().map(|s| s.running).unwrap_or(false);
    let path = if running { "/api/stop" } else { "/api/start" };
    match backend_request("POST", path) {
        Ok(_) => eprintln!("[tray] 已{}自动回复", if running { "暂停" } else { "开启" }),
        Err(e) => eprintln!("[tray] 切换自动回复失败：{}", e),
    }
    refresh_tray(app);
}

/// 打开日志目录（不存在就先建，免得只反馈一句「打开失败」）。
fn open_logs_dir(app: &AppHandle) {
    let dir = project_root().join("wechatauto_logs");
    let _ = std::fs::create_dir_all(&dir);
    if let Err(e) = open_path(app, &dir) {
        eprintln!("[tray] 打开日志目录失败（{}）：{}", dir.display(), e);
    }
}

/// 用系统默认程序打开一个路径（经 tauri-plugin-opener）。
fn open_path(app: &AppHandle, path: &Path) -> Result<(), String> {
    use tauri_plugin_opener::OpenerExt;
    app.opener()
        .open_path(path.to_string_lossy().to_string(), None::<&str>)
        .map_err(|e| e.to_string())
}

/// 按后端当前状态刷新托盘图标、提示文字与「暂停/恢复」文案。
fn refresh_tray(app: &AppHandle) {
    let Some(tray) = app.tray_by_id(TRAY_ID) else {
        return;
    };
    let Some(ui) = app.try_state::<TrayUi>() else {
        return;
    };

    let (idx, tip, toggle_text) = match fetch_status() {
        Ok(s) if s.running => (0usize, "微信自动回复 · 运行中".to_string(), "暂停自动回复"),
        // 连上了后端但停着：分清「正常暂停」与「想开却开不起来（last_error 非空）」
        Ok(s) if !s.last_error.is_empty() => (
            2usize,
            format!("微信自动回复 · 出错：{}", s.last_error),
            "恢复自动回复",
        ),
        Ok(_) => (1usize, "微信自动回复 · 已暂停".to_string(), "恢复自动回复"),
        Err(e) => (
            2usize,
            format!("微信自动回复 · 连不上后端（{}）", e),
            "恢复自动回复",
        ),
    };

    let _ = tray.set_icon(Some(ui.icons[idx].clone()));
    let _ = tray.set_tooltip(Some(tip));
    if ui.toggle.text().unwrap_or_default() != toggle_text {
        let _ = ui.toggle.set_text(toggle_text);
    }
}

/// 每 2 秒刷新一次托盘状态；主线程结束（正在退出）时本线程自行收工。
fn spawn_tray_status_poller(app: &AppHandle) {
    let handle = app.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(2));
        let h = handle.clone();
        if handle.run_on_main_thread(move || refresh_tray(&h)).is_err() {
            break;
        }
    });
}

/// 解一张内嵌 PNG 成托盘图标。图案由 scripts/make-tray-icons.py 生成，
/// 解码失败只可能是构建时资源不对，属于该早报的错。
fn load_tray_icon(bytes: &'static [u8]) -> tauri::Result<Image<'static>> {
    Image::from_bytes(bytes)
}

/// 用哪个 Python 跑后端：优先环境变量 BOT_PYTHON，否则回退到本机 anaconda pytorch 环境。
fn python_exe() -> String {
    std::env::var("BOT_PYTHON")
        .unwrap_or_else(|_| r"C:\Users\22069\anaconda3\envs\pytorch\python.exe".to_string())
}

/// 后端入口脚本：以 CARGO_MANIFEST_DIR（desktop/src-tauri）为基准，上跳两级到仓库根。
fn project_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
}

fn backend_main_py() -> PathBuf {
    project_root().join("src").join("main.py")
}

/// 从仓库根的 .env 读取 UI_TOKEN。
///
/// 桌面壳和后端同机，走的是 127.0.0.1——本机访问不该让人再手填一次令牌。
/// 后端设了 UI_TOKEN（为了手机远程访问）时，这里把它取出来拼进 URL，
/// 窗口就能直接进面板。令牌只在运行时从 .env 读，不写进代码、不进仓库。
///
/// 读不到（文件不存在/没设令牌）就返回空串：后端未设令牌时本就不需要它，
/// 行为与从前完全一致。
fn read_ui_token() -> String {
    match std::fs::read_to_string(project_root().join(".env")) {
        Ok(text) => parse_ui_token(&text),
        Err(_) => String::new(),
    }
}

/// 从 .env 文本里取出 UI_TOKEN 的值（纯函数，便于单测）。
fn parse_ui_token(text: &str) -> String {
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with('#') {
            continue;
        }
        let Some(rest) = line.strip_prefix("UI_TOKEN") else {
            continue;
        };
        // 必须紧跟 '='，避免把 UI_TOKEN_XXX 之类的键误当成它。
        let Some(value) = rest.trim_start().strip_prefix('=') else {
            continue;
        };
        let value = value.trim();
        // .env 惯例：'#' 前有空白才算行内注释。
        let value = match value.find(" #") {
            Some(i) => value[..i].trim(),
            None => value,
        };
        return value.trim_matches(|c| c == '"' || c == '\'').to_string();
    }
    String::new()
}

/// 拼后端 URL；令牌非空时带上 ?token=，前端会自动收进 localStorage。
fn build_backend_url(token: &str) -> url::Url {
    let mut url = format!("http://127.0.0.1:{}", BACKEND_PORT)
        .parse::<url::Url>()
        .expect("硬编码后端 URL 无效");
    if !token.is_empty() {
        url.query_pairs_mut().append_pair("token", token);
    }
    url
}

/// 备选 WebView2 用户数据目录：放在用户主目录下（非 AppData）自己可写的位置。
/// 默认位置在某些受限环境写入受限，所以留作第一备选；两者都不行还有临时目录兜底，
/// 候选顺序见 setup 里的 `candidates`。
fn webview_data_dir() -> PathBuf {
    std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .map(|d| PathBuf::from(d).join("wechat-bot").join("webview"))
        .unwrap_or_else(|_| std::env::temp_dir().join("wechat-bot").join("webview"))
}

/// 兜底 WebView2 数据目录：带进程号，保证与任何已存在的实例都不撞。
///
/// 只在常规目录建窗失败（被别的进程占用）时才会用到；正常情况下不会创建。
/// 里面没有任何需要保留的东西（令牌每次启动由壳从 .env 重新拼进 URL），
/// 攒多了可以直接删 `%TEMP%\wechat-bot\`。
fn webview_data_dir_alt() -> PathBuf {
    std::env::temp_dir()
        .join("wechat-bot")
        .join(format!("webview-{}", std::process::id()))
}

/// WebView2 在非 ASCII 路径（如含中文）下会创建控制器失败（0x8000FFFF），
/// 启动时检查 exe 所在路径，非 ASCII 就提前给出可操作的提示。
fn warn_if_non_ascii_exe_path() {
    if let Ok(exe) = std::env::current_exe() {
        if !exe.to_string_lossy().is_ascii() {
            eprintln!(
                "警告：程序所在路径包含非 ASCII 字符（如中文），WebView2 可能无法创建窗口：\n  {} \n  请把程序/项目放到纯英文（ASCII）路径下再启动。",
                exe.display()
            );
        }
    }
}

/// 轮询后端端口，直到能连上或超时。
fn wait_for_backend(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if TcpStream::connect(("127.0.0.1", BACKEND_PORT)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    false
}

/// 接管后端子进程的 stdout/stderr，按行加 `[py]` 前缀转发到壳的 stderr。
///
/// 不接管的话，子进程会直接继承壳的控制台句柄：Python 的 logging 与壳的 eprintln!
/// 是两个进程在并发写同一个句柄，输出会在任意字节边界互相覆盖。实测后果不只是
/// 排版错乱——整行会**真的丢失**（`[guia.py:848] 已加载布局校准（wide）…` 那行
/// 就永久消失了），相邻两行还会被拼成一行，正好破坏最需要的诊断信息。
///
/// 改法：把两路输出收进管道，由本进程的线程逐行转发，于是"写控制台"这件事
/// 重新回到单一进程内。每次写整行时持有 stderr 的全局锁（`StderrLock`），
/// 因此转发线程与壳自己的 `eprintln!` 之间也不会互相插入。
fn spawn_output_forwarders(child: &mut Child) {
    // ChildStdout 与 ChildStderr 是两个不同的类型，不能合并进同一个数组，
    // 所以各转发一次；两者都 impl Read + Send，共用下面这个泛型函数。
    if let Some(out) = child.stdout.take() {
        forward_lines(out);
    }
    if let Some(err) = child.stderr.take() {
        forward_lines(err);
    }
}

/// 起一个线程，把 `pipe` 的每一行加 `[py]` 前缀写到壳的 stderr。
fn forward_lines<R: std::io::Read + Send + 'static>(pipe: R) {
    std::thread::spawn(move || {
        for line in BufReader::new(pipe).lines() {
            let Ok(line) = line else { break };
            // 持锁整行写出：和壳自身的 eprintln! 串行，不再交织。
            let mut err = std::io::stderr().lock();
            let _ = writeln!(err, "[py] {}", line);
        }
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    warn_if_non_ascii_exe_path();

    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        // 关窗 ≠ 退出：托盘程序关掉窗口后要留在托盘里，点「退出」才真的结束。
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(|app| {
            let py = python_exe();
            let main_py = backend_main_py();

            // 端口已经在听 → 后端已经跑着了（比如看门狗刚拉起来的那个）。
            // 这时绝不能再起第二个：webui.py 关掉了 SO_REUSEADDR，第二个会在 bind 时
            // 直接以 10048 退出，白多一个短命进程，日志还容易被误读成「壳把后端搞崩了」。
            // 复用它，并且**不**记进 Backend —— 它不是本壳的子进程，本壳也无权杀它。
            let reused = TcpStream::connect(("127.0.0.1", BACKEND_PORT)).is_ok();
            let mut child: Option<Child> = if reused {
                eprintln!(
                    "[backend] 端口 {} 上已有后端在运行，本次直接复用，不再启动新的",
                    BACKEND_PORT
                );
                None
            } else {
                let mut cmd = Command::new(&py);
                cmd.arg(&main_py)
                    .current_dir(project_root())
                    // 空设备而不是继承：壳不需要 stdin，也不该把隐藏控制台的句柄传下去。
                    .stdin(Stdio::null())
                    // 接管后端两路输出，避免两个进程抢同一个控制台句柄而丢行/拼行。
                    // 见 spawn_output_forwarders 的说明。
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped());
                // 桌面壳是 GUI 子系统进程（release 下 windows_subsystem = "windows"），
                // 由它拉起的控制台子进程（python.exe）默认会**新建一个可见的黑窗口**。
                // 实测（2026-10-05 13:20:26）：python 进程与一个新 conhost 同秒出现，就是这个窗口。
                // CREATE_NO_WINDOW 把它藏掉，且不影响上面那两路管道。
                #[cfg(windows)]
                {
                    use std::os::windows::process::CommandExt;
                    cmd.creation_flags(0x0800_0000);
                }
                let mut c = cmd.spawn().unwrap_or_else(|e| {
                    panic!("无法启动 Python 后端（{} {}）: {}", py, main_py.display(), e)
                });
                spawn_output_forwarders(&mut c);

                // 后端现在先把端口绑好、再在后台连微信（见 src/main.py），所以这里通常
                // 毫秒级就绪。仍保留等待是兜底：万一端口被占或启动异常，也先等一会儿再开窗。
                if !wait_for_backend(Duration::from_secs(15)) {
                    eprintln!("警告：Python 后端 15 秒内未就绪，窗口可能显示连接失败，请查看后端日志。");
                }
                Some(c)
            };

            // 后端设了 UI_TOKEN 时，本机窗口也免填：拼进查询参数，
            // 前端 bootstrapToken() 会把它收进 localStorage 并从地址栏抹掉。
            let url = WebviewUrl::External(build_backend_url(&read_ui_token()));

            // WebView2 的用户数据目录在真机上未必处处可用，而且失败时的错误码完全看不出原因：
            //   0x800700AA「资源在使用中」= 目录被别的实例占着（该目录是独占的）；
            //   0x8000FFFF「灾难性故障」  = 目录建不出来 / 不可写 / 旧 profile 不可用；
            //   Io(PermissionDenied)      = 目录本身没权限。
            // 与其赌某个位置一定能用，不如按可靠性依次试，任一成功就开窗：
            //   1) Tauri 默认位置（%LOCALAPPDATA%\<identifier>\EBWebView）—— 最标准
            //   2) 用户主目录 wechat-bot\webview —— 默认位置受限时的备选
            //   3) 临时目录（带进程号）—— 绝不与任何已有实例相撞
            // 全都不行才退出，并把每个位置的失败原因都打出来，便于对症处理。
            let candidates: [Option<PathBuf>; 3] = [
                None, // None = 不设 data_directory，交给 Tauri 用默认位置
                Some(webview_data_dir()),
                Some(webview_data_dir_alt()),
            ];
            let mut last_err = None;
            let mut opened = false;
            for (i, dir) in candidates.iter().enumerate() {
                let label = match dir {
                    Some(d) => d.display().to_string(),
                    None => "Tauri 默认位置".to_string(),
                };
                // 提前建好目录：让 WebView2 自己建时报错很难懂，这里先建能拿到准确的错误。
                if let Some(d) = dir {
                    if let Err(e) = std::fs::create_dir_all(d) {
                        eprintln!("警告：创建 WebView2 数据目录失败 {}：{}", d.display(), e);
                    }
                }

                let mut builder = WebviewWindowBuilder::new(app.handle(), "main", url.clone())
                    // ⚠️ 标题里**绝不能**出现「微信 / Weixin / WeChat」。
                    //
                    // wechatauto 靠「标题含微信关键字(+5) / 类名前缀 Qt51514QWindowIcon(+4)
                    // / 进程名 weixin.exe(+3) / 尺寸够大(+2)」给可见顶层窗口打分，
                    // 取最高分且门槛恰好是 5（见 site-packages/wechatauto/guia.py 的
                    // _find_main_window）。原标题「微信自动回复」光靠标题就 +5 直接过门槛。
                    //
                    // 实测 2026-10-05 13:20：真微信 hwnd=133096 得 14 分、本壳 hwnd=197194 得 7 分，
                    // 正常时微信赢；但**只要微信主窗口那一刻不可见**（最小化到托盘、正在重建），
                    // 本壳窗口就成了唯一过门槛的候选 —— 后端会绑定到我们自己的窗口上，
                    // 自动回复的点击全部打进控制面板。改掉标题后本壳只有 +2（尺寸），永不过门槛。
                    .title("自动回复助手")
                    .inner_size(640.0, 860.0)
                    // 关掉 GPU 加速，避免本机 WebView2 GPU 进程崩溃（0x8000FFFF）；
                    // 前面一段是 wry 的默认参数，必须保留。
                    .additional_browser_args(
                        "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --disable-gpu",
                    );
                if let Some(d) = dir {
                    builder = builder.data_directory(d.clone());
                }

                match builder.build() {
                    Ok(_) => {
                        opened = true;
                        if i > 0 {
                            eprintln!("[window] 第 {} 个数据目录（{}）建窗成功", i + 1, label);
                        }
                        break;
                    }
                    Err(e) => {
                        eprintln!("[window] 用数据目录「{}」建窗失败：{:#?}", label, e);
                        last_err = Some(e);
                    }
                }
            }

            if !opened {
                // 建窗失败：显式回收后端，不依赖 Drop 兜底。
                if child.is_some() {
                    kill_child(&mut child);
                    eprintln!("[backend] 建窗失败，已回收 Python 后端子进程");
                }
                if let Some(e) = last_err {
                    eprintln!(
                        "[window] {} 个数据目录全部失败，最后一次错误：{:#?}",
                        candidates.len(),
                        e
                    );
                }
                eprintln!(
                    "提示：错误码若是 0x800700AA（资源在使用中），多半是另一个「微信自动回复」\n\
                     窗口已经开着 —— 关掉它再启动即可。若反复出现，把下面两个目录删掉后重试：\n  {}\n  {}",
                    webview_data_dir().display(),
                    webview_data_dir_alt().display()
                );
                // 用可读的提示退出，而不是让 tauri 抛出 setup 失败再打一堆 Rust backtrace。
                std::process::exit(1);
            }

            // 建窗成功后才把后端句柄交给 Tauri 状态，退出/关窗时统一回收。
            app.manage(Backend(Mutex::new(child)));

            // ---- 托盘：批次 1 的核心，让它在电脑上「是一个软件」而不是一个进程 ----
            let icons = [
                load_tray_icon(include_bytes!("../icons/tray-run.png"))?,
                load_tray_icon(include_bytes!("../icons/tray-paused.png"))?,
                load_tray_icon(include_bytes!("../icons/tray-error.png"))?,
            ];

            let open_i = MenuItem::with_id(app, "open", "打开控制面板", true, None::<&str>)?;
            let toggle_i = MenuItem::with_id(app, "toggle", "暂停自动回复", true, None::<&str>)?;
            let logs_i = MenuItem::with_id(app, "logs", "打开日志目录", true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let sep1 = PredefinedMenuItem::separator(app)?;
            let sep2 = PredefinedMenuItem::separator(app)?;
            let sep3 = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(
                app,
                &[&open_i, &sep1, &toggle_i, &sep2, &logs_i, &sep3, &quit_i],
            )?;

            TrayIconBuilder::with_id(TRAY_ID)
                // 先按「已暂停」显示：后端起来时的默认就是暂停，等第一次轮询再纠正。
                .icon(icons[1].clone())
                .tooltip("微信自动回复 · 正在启动…")
                .menu(&menu)
                // 左键单击直接开面板，所以菜单只在右键弹（Windows 上的习惯）。
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "open" => show_main_window(app),
                    "toggle" => toggle_auto_reply(app),
                    "logs" => open_logs_dir(app),
                    "quit" => {
                        // 退出码 0 = 用户主动退出。看门狗据此区分「主动关」和「崩了」，
                        // 于是点退出之后不会被过一会儿偷偷拉起来（见 scripts/backend-loop.ps1）。
                        eprintln!("[tray] 用户从托盘退出，回收后端后结束进程");
                        app.state::<Backend>().kill();
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main_window(tray.app_handle());
                    }
                })
                .build(app)?;

            app.manage(TrayUi {
                icons,
                toggle: toggle_i,
            });
            spawn_tray_status_poller(app.handle());

            Ok(())
        });

    let app = builder
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app, event| {
        if let RunEvent::Exit = event {
            // 正常退出回收后端；异常路径（建窗 panic 等）由 Backend::drop 兜底。
            app.state::<Backend>().kill();
        }
    });
}
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_ui_token_basic() {
        assert_eq!(parse_ui_token("UI_TOKEN=abc123\n"), "abc123");
    }

    #[test]
    fn parse_ui_token_ignores_comments_and_other_keys() {
        let text = "# UI_TOKEN=nope\nLLM_API_KEY=sk-x\nUI_TOKEN=real\n";
        assert_eq!(parse_ui_token(text), "real");
    }

    #[test]
    fn parse_ui_token_strips_quotes_and_inline_comment() {
        assert_eq!(parse_ui_token("UI_TOKEN=\"abc\"\n"), "abc");
        assert_eq!(parse_ui_token("UI_TOKEN=abc # 说明\n"), "abc");
        assert_eq!(parse_ui_token("  UI_TOKEN = spaced  \n"), "spaced");
    }

    #[test]
    fn parse_ui_token_not_confused_by_prefix() {
        // UI_TOKEN_EXTRA 不该被当成 UI_TOKEN
        assert_eq!(parse_ui_token("UI_TOKEN_EXTRA=wrong\nUI_TOKEN=right\n"), "right");
    }

    #[test]
    fn parse_ui_token_empty_when_absent_or_blank() {
        assert_eq!(parse_ui_token("LLM_API_KEY=x\n"), "");
        assert_eq!(parse_ui_token("UI_TOKEN=\n"), ""); // 未设令牌 → 空，行为同从前
        assert_eq!(parse_ui_token(""), "");
    }

    #[test]
    fn build_backend_url_without_token_has_no_query() {
        assert_eq!(build_backend_url("").as_str(), "http://127.0.0.1:8000/");
    }

    #[test]
    fn build_backend_url_with_token_appends_query() {
        assert_eq!(
            build_backend_url("test-token-abc123").as_str(),
            "http://127.0.0.1:8000/?token=test-token-abc123"
        );
    }

    #[test]
    fn fallback_data_dir_is_process_specific() {
        let alt = webview_data_dir_alt();
        // 必须和常规目录不同，否则「换目录重试」等于没换
        assert_ne!(alt, webview_data_dir());
        assert!(alt.to_string_lossy().contains(&std::process::id().to_string()));
    }

    #[test]
    fn build_backend_url_encodes_special_chars() {
        // 令牌含需转义字符时也不能拼出非法 URL
        assert_eq!(
            build_backend_url("a b&c").as_str(),
            "http://127.0.0.1:8000/?token=a+b%26c"
        );
    }
}
