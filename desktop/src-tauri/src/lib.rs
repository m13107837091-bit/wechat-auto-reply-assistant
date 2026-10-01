// 桌面壳：启动时拉起 Python 后端（仓库根 src/main.py），等它在本地端口就绪后打开窗口；
// 关闭窗口时回收后端子进程。页面与 API 都由 Python 后端直接提供，本壳只负责进程生命周期。
use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};

const BACKEND_PORT: u16 = 8000;

/// 后端子进程句柄，存进 Tauri 状态；无论正常关窗还是 panic，都由 Drop 兜底回收。
struct Backend(Mutex<Option<Child>>);

impl Backend {
    /// 杀掉并回收后端子进程（幂等：杀完句柄置 None，重复调用无副作用）。
    fn kill(&self) {
        if let Some(mut child) = self.0.lock().unwrap().take() {
            let _ = child.kill();
            let _ = child.wait();
            eprintln!("[backend] 已回收 Python 后端子进程");
        }
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        self.kill();
    }
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

/// WebView2 用户数据目录。
/// 放到用户主目录下（非 AppData）自己可写的位置，避免某些受限环境的写入限制。
fn webview_data_dir() -> PathBuf {
    std::env::var("USERPROFILE")
        .or_else(|_| std::env::var("HOME"))
        .map(|d| PathBuf::from(d).join("wechat-bot").join("webview"))
        .unwrap_or_else(|_| std::env::temp_dir().join("wechat-bot").join("webview"))
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    warn_if_non_ascii_exe_path();

    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let py = python_exe();
            let main_py = backend_main_py();
            let mut child = Command::new(&py)
                .arg(&main_py)
                .current_dir(project_root())
                .spawn()
                .unwrap_or_else(|e| {
                    panic!("无法启动 Python 后端（{} {}）: {}", py, main_py.display(), e)
                });

            // 后端现在先把端口绑好、再在后台连微信（见 src/main.py），所以这里通常
            // 毫秒级就绪。仍保留等待是兜底：万一端口被占或启动异常，也先等一会儿再开窗。
            if !wait_for_backend(Duration::from_secs(15)) {
                eprintln!("警告：Python 后端 15 秒内未就绪，窗口可能显示连接失败，请查看后端日志。");
            }

            // WebView2 数据目录提前创建好，避免默认路径 create_dir_all 报「拒绝访问」。
            let data_dir = webview_data_dir();
            if let Err(e) = std::fs::create_dir_all(&data_dir) {
                eprintln!("警告：创建 WebView2 数据目录失败 {}：{}", data_dir.display(), e);
            }

            // 后端设了 UI_TOKEN 时，本机窗口也免填：拼进查询参数，
            // 前端 bootstrapToken() 会把它收进 localStorage 并从地址栏抹掉。
            let url = WebviewUrl::External(build_backend_url(&read_ui_token()));

            if let Err(e) = WebviewWindowBuilder::new(app.handle(), "main", url)
                .title("微信自动回复")
                .inner_size(640.0, 860.0)
                .data_directory(data_dir)
                // 关掉 GPU 加速，避免本机 WebView2 GPU 进程崩溃（0x8000FFFF）；
                // 前面一段是 wry 的默认参数，必须保留。
                .additional_browser_args(
                    "--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --disable-gpu",
                )
                .build()
            {
                eprintln!("[window] 创建窗口失败，完整错误：{:#?}", e);
                // 建窗失败：显式回收后端，不依赖 Drop 兜底（panic 路径下 Drop 不保证触发）。
                let _ = child.kill();
                let _ = child.wait();
                eprintln!("[backend] 建窗失败，已回收 Python 后端子进程");
                return Err(e.into());
            }

            // 建窗成功后才把后端句柄交给 Tauri 状态，退出/关窗时统一回收。
            app.manage(Backend(Mutex::new(Some(child))));

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
    fn build_backend_url_encodes_special_chars() {
        // 令牌含需转义字符时也不能拼出非法 URL
        assert_eq!(
            build_backend_url("a b&c").as_str(),
            "http://127.0.0.1:8000/?token=a+b%26c"
        );
    }
}
