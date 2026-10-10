use serde::Serialize;
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Manager, State};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogResult};

mod local_caller;
mod local_core_bridge;
mod managed_core;

use local_core_bridge::BridgeResponse;
use managed_core::ManagedCore;

const CORE_ADDR: SocketAddr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 8000);

struct CoreEndpoint {
    addr: SocketAddr,
    url: String,
}

fn parse_dev_core_url(value: &str) -> Result<CoreEndpoint, String> {
    let port = value
        .strip_prefix("http://127.0.0.1:")
        .filter(|text| !text.is_empty() && text.bytes().all(|byte| byte.is_ascii_digit()))
        .and_then(|text| text.parse::<u16>().ok())
        .filter(|port| *port != 0)
        .ok_or("OPERANT_CORE_URL must be http://127.0.0.1:<port>")?;
    Ok(CoreEndpoint {
        addr: SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port),
        url: format!("http://127.0.0.1:{port}"),
    })
}

fn configured_endpoint() -> Result<CoreEndpoint, String> {
    #[cfg(debug_assertions)]
    if let Ok(value) = std::env::var("OPERANT_CORE_URL") {
        return parse_dev_core_url(&value);
    }
    Ok(CoreEndpoint {
        addr: CORE_ADDR,
        url: "http://127.0.0.1:8000".into(),
    })
}

#[derive(Default)]
struct CoreProcess(Arc<Mutex<Option<ManagedCore>>>);

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CoreStatus {
    reachable: bool,
    managed_process_running: bool,
    endpoint: String,
}

fn installed_core_executable() -> Result<PathBuf, String> {
    // The installed CLI and operator-owned PATH are trust premises. Resolve
    // the fixed command once; no WebView value or shell text chooses the binary.
    #[cfg(debug_assertions)]
    if let Some(path) = std::env::var_os("OPERANT_CORE_EXECUTABLE") {
        let path = PathBuf::from(path);
        if path.is_absolute() && path.is_file() {
            return path
                .canonicalize()
                .map_err(|_| "无法定位已安装的 Core。".into());
        }
        return Err("Core 开发入口须为已安装的绝对文件路径。".into());
    }
    let name = if cfg!(windows) {
        "operant.exe"
    } else {
        "operant"
    };
    for directory in std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()) {
        if !directory.is_absolute() {
            continue;
        }
        let candidate = directory.join(name);
        if candidate.is_file() {
            return candidate
                .canonicalize()
                .map_err(|_| "无法定位已安装的 Core。".into());
        }
    }
    Err("未找到已安装的 Operant Core。".into())
}

fn core_data_directory(app: &AppHandle) -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    if let Some(path) = std::env::var_os("OPERANT_CORE_DATA_DIR") {
        let path = PathBuf::from(path);
        if !path.is_absolute() {
            return Err("Core 开发数据目录须为绝对路径。".into());
        }
        fs::create_dir_all(&path).map_err(|_| "无法创建 Core 数据目录。")?;
        return path
            .canonicalize()
            .map_err(|_| "无法定位 Core 数据目录。".into());
    }
    let path = app
        .path()
        .app_data_dir()
        .map_err(|_| "无法定位 Core 数据目录。")?
        .join("core");
    fs::create_dir_all(&path).map_err(|_| "无法创建 Core 数据目录。")?;
    path.canonicalize()
        .map_err(|_| "无法定位 Core 数据目录。".into())
}

fn core_storage(app: &AppHandle) -> Result<(PathBuf, PathBuf), String> {
    // Preserve an installation's explicit database selection. Development
    // isolation takes precedence, so a candidate cannot inherit that database.
    #[cfg(debug_assertions)]
    if std::env::var_os("OPERANT_CORE_DATA_DIR").is_some() {
        let directory = core_data_directory(app)?;
        return Ok((directory.clone(), directory.join("operant.sqlite3")));
    }
    if let Some(path) = std::env::var_os("OPERANT_DB_PATH") {
        let database = PathBuf::from(path);
        if !database.is_absolute() || database.file_name().is_none() {
            return Err("Core 数据库须使用绝对文件路径。".into());
        }
        let directory = database
            .parent()
            .ok_or("Core 数据库目录无效。")?
            .canonicalize()
            .map_err(|_| "无法定位 Core 数据库目录。")?;
        if !directory.is_dir() {
            return Err("Core 数据库目录无效。".into());
        }
        return Ok((directory, database));
    }
    let directory = core_data_directory(app)?;
    Ok((directory.clone(), directory.join("operant.sqlite3")))
}

#[tauri::command]
async fn core_status(state: State<'_, CoreProcess>) -> Result<CoreStatus, String> {
    let endpoint = configured_endpoint()?;
    let bridge = {
        let mut guard = state.0.lock().map_err(|_| "无法读取 Core 状态。")?;
        let bridge = match guard.as_mut() {
            Some(owned) => {
                if owned.running()? {
                    Some(Arc::clone(&owned.bridge))
                } else {
                    None
                }
            }
            None => None,
        };
        if bridge.is_none() {
            *guard = None;
        }
        bridge
    };
    let running = bridge.is_some();
    let reachable = match bridge {
        Some(bridge) => {
            tauri::async_runtime::spawn_blocking(move || bridge.verify_identity().is_ok())
                .await
                .map_err(|_| "无法读取 Core 状态。")?
        }
        None => false,
    };
    Ok(CoreStatus {
        reachable,
        managed_process_running: running,
        endpoint: endpoint.url,
    })
}

#[tauri::command]
async fn start_local_core(
    app: AppHandle,
    state: State<'_, CoreProcess>,
) -> Result<CoreStatus, String> {
    let process = CoreProcess(Arc::clone(&state.0));
    tauri::async_runtime::spawn_blocking(move || start_owned_core(&app, &process))
        .await
        .map_err(|_| "无法启动本机 Core。".to_string())?
}

fn start_owned_core(app: &AppHandle, process: &CoreProcess) -> Result<CoreStatus, String> {
    let endpoint = configured_endpoint()?;
    let mut guard = process.0.lock().map_err(|_| "无法读取 Core 状态。")?;
    if let Some(owned) = guard.as_mut() {
        if owned.running()? && owned.verified() {
            return Ok(CoreStatus {
                reachable: true,
                managed_process_running: true,
                endpoint: endpoint.url,
            });
        }
        return Err("已有托管 Core 不可用，请先停止并核对状态。".into());
    }
    let executable = installed_core_executable()?;
    let (directory, database) = core_storage(app)?;
    let owned = ManagedCore::spawn(&executable, &directory, &database, endpoint.addr)?;
    *guard = Some(owned);
    Ok(CoreStatus {
        reachable: true,
        managed_process_running: true,
        endpoint: endpoint.url,
    })
}

#[tauri::command]
fn stop_managed_core(state: State<'_, CoreProcess>) -> Result<(), String> {
    let mut guard = state.0.lock().map_err(|_| "无法读取 Core 状态。")?;
    // ManagedCore drop reaps only the child this shell created.
    *guard = None;
    Ok(())
}

#[tauri::command]
async fn local_core_request(
    state: State<'_, CoreProcess>,
    operation_id: String,
    path_params: BTreeMap<String, String>,
    raw_query: String,
    headers: BTreeMap<String, String>,
    body: String,
) -> Result<BridgeResponse, String> {
    let bridge = {
        let mut guard = state.0.lock().map_err(|_| "无法读取 Core 状态。")?;
        let owned = guard.as_mut().ok_or("尚未连接本机 Core。")?;
        if !owned.running()? {
            return Err("本机 Core 已停止。".into());
        }
        Arc::clone(&owned.bridge)
    };
    // Blocking HTTP runs off the event loop, without holding the child lock.
    // The user can still stop the owned child while a request is in flight.
    tauri::async_runtime::spawn_blocking(move || {
        let params: Vec<_> = path_params
            .iter()
            .map(|(k, v)| (k.as_str(), v.as_str()))
            .collect();
        let request_headers: Vec<_> = headers
            .iter()
            .map(|(k, v)| (k.as_str(), v.as_str()))
            .collect();
        bridge
            .request(&operation_id, &params, &raw_query, &request_headers, &body)
            .map_err(|error| error.message().into())
    })
    .await
    .map_err(|_| "无法完成本机 Core 请求。".to_string())?
}

fn valid_secret_ref(value: &str) -> bool {
    let mut chars = value.chars();
    matches!(chars.next(), Some('A'..='Z' | '_'))
        && value.len() <= 128
        && chars.all(|character| matches!(character, 'A'..='Z' | '0'..='9' | '_'))
}

#[tauri::command]
async fn create_caller_pairing_ticket(
    app: AppHandle,
    state: State<'_, CoreProcess>,
) -> Result<serde_json::Value, String> {
    let parent = app
        .get_webview_window("main")
        .ok_or("无法打开配对确认窗口。")?;
    let bridge = {
        let mut guard = state.0.lock().map_err(|_| "无法读取 Core 状态。")?;
        let owned = guard.as_mut().ok_or("尚未连接本机 Core。")?;
        if !owned.running()? {
            return Err("本机 Core 已停止。".into());
        }
        Arc::clone(&owned.bridge)
    };
    tauri::async_runtime::spawn_blocking(move || {
        let (send_choice, receive_choice) = std::sync::mpsc::sync_channel(1);
        app
            .dialog()
            .message("允许配对的客户端查看、添加和删除技能目录吗？文件操作仍需按现有规则审批。配对码 2 分钟后失效，可随时撤销客户端。")
            .title("连接其他客户端")
            .parent(&parent)
            .buttons(MessageDialogButtons::OkCancel)
            .show_with_result(move |choice| { let _ = send_choice.send(choice); });
        let choice = receive_choice.recv().map_err(|_| "配对确认未完成。".to_string())?;
        if !matches!(choice, MessageDialogResult::Ok) {
            return Err("已取消配对。".to_string());
        }
        let response = bridge
            .request_approved_pairing()
            .map_err(|error| error.message().to_string())?;
        if response.status != 200 {
            return Err("无法生成配对码，请核对本机 Core 状态。".into());
        }
        serde_json::from_str(&response.text)
            .map_err(|_| "配对码响应无效，请核对本机 Core 状态。".into())
    })
    .await
    .map_err(|_| "无法完成配对确认。".to_string())?
}

fn validate_model_oauth_url(value: &str, core_port: u16) -> Result<tauri::Url, String> {
    if value.len() > 16_384 || value.chars().any(char::is_control) {
        return Err("Invalid model sign-in URL".into());
    }
    let url = tauri::Url::parse(value).map_err(|_| "Invalid model sign-in URL")?;
    if url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
        || url.fragment().is_some()
        || !matches!(
            (url.host_str(), url.path()),
            (Some("auth.openai.com"), "/api/accounts/authorize")
                | (Some("accounts.google.com"), "/o/oauth2/v2/auth")
        )
    {
        return Err("Only official model sign-in pages can be opened".into());
    }
    let provider = if url.host_str() == Some("auth.openai.com") {
        "chatgpt"
    } else {
        "gemini"
    };
    let callbacks: Vec<_> = url
        .query_pairs()
        .filter(|(key, _)| key == "redirect_uri")
        .collect();
    let expected_callback =
        format!("http://127.0.0.1:{core_port}/internal/model-auth/{provider}/callback");
    if callbacks.len() != 1 || callbacks[0].1 != expected_callback {
        return Err("Model sign-in must return to this local Core".into());
    }
    Ok(url)
}

#[tauri::command]
fn open_model_oauth(url: String) -> Result<(), String> {
    let url = validate_model_oauth_url(&url, configured_endpoint()?.addr.port())?;
    tauri_plugin_opener::open_url(url.as_str(), None::<&str>)
        .map_err(|_| "Could not open the model sign-in page".into())
}

#[tauri::command]
fn persist_secret_reference(app: AppHandle, secret_ref: String) -> Result<(), String> {
    if !valid_secret_ref(&secret_ref) {
        return Err("secret_ref must be an uppercase environment variable name".into());
    }
    let directory = app
        .path()
        .app_config_dir()
        .map_err(|error| format!("Could not locate app config directory: {error}"))?;
    fs::create_dir_all(&directory)
        .map_err(|error| format!("Could not create app config directory: {error}"))?;
    let target = directory.join("core-secret-ref.json");
    let temporary = directory.join(format!("core-secret-ref.{}.tmp", std::process::id()));
    let payload = serde_json::to_vec(&serde_json::json!({ "secret_ref": secret_ref }))
        .map_err(|error| format!("Could not encode secret reference: {error}"))?;
    let mut file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&temporary)
        .map_err(|error| format!("Could not create secret reference file: {error}"))?;
    file.write_all(&payload)
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("Could not persist secret reference: {error}"))?;
    fs::rename(temporary, target)
        .map_err(|error| format!("Could not publish secret reference: {error}"))?;
    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(CoreProcess::default())
        .setup(|app| {
            start_owned_core(app.handle(), &app.state::<CoreProcess>())?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            core_status,
            start_local_core,
            stop_managed_core,
            local_core_request,
            create_caller_pairing_ticket,
            persist_secret_reference,
            open_model_oauth
        ])
        .build(tauri::generate_context!())
        .expect("error while building Operant desktop shell")
        .run(|app, event| {
            // Tauri exits the process without dropping managed state. Reap
            // only the Core this shell launched; an existing Core is unowned.
            if matches!(event, tauri::RunEvent::Exit) {
                let _ = stop_managed_core(app.state::<CoreProcess>());
            }
        });
}

#[cfg(test)]
mod tests {
    use super::{parse_dev_core_url, valid_secret_ref, validate_model_oauth_url};

    #[test]
    fn model_sign_in_opener_rejects_other_origins_paths_and_url_credentials() {
        for valid in [
            "https://auth.openai.com/api/accounts/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Finternal%2Fmodel-auth%2Fchatgpt%2Fcallback",
            "https://accounts.google.com/o/oauth2/v2/auth?redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Finternal%2Fmodel-auth%2Fgemini%2Fcallback",
        ] {
            assert!(validate_model_oauth_url(valid, 8000).is_ok());
        }
        for invalid in [
            "http://auth.openai.com/api/accounts/authorize",
            "https://auth.openai.com.evil.example/api/accounts/authorize",
            "https://auth.openai.com@evil.example/api/accounts/authorize",
            "https://name:secret@auth.openai.com/api/accounts/authorize",
            "https://auth.openai.com:8443/api/accounts/authorize",
            "https://auth.openai.com/api/accounts/authorize/other",
            "https://accounts.google.com/other",
            "https://auth.openai.com/api/accounts/authorize#other",
            "file:///private/tmp/example",
            "javascript:alert(1)",
            "https://auth.openai.com/api/accounts/authorize?redirect_uri=https%3A%2F%2Fevil.example",
            "https://auth.openai.com/api/accounts/authorize?redirect_uri=http%3A%2F%2F127.0.0.1%3A8001%2Finternal%2Fmodel-auth%2Fchatgpt%2Fcallback",
        ] {
            assert!(validate_model_oauth_url(invalid, 8000).is_err(), "{invalid}");
        }
        assert!(validate_model_oauth_url(&"x".repeat(16_385), 8000).is_err());
    }

    #[test]
    fn dev_core_url_accepts_only_explicit_loopback_http_port() {
        let endpoint = parse_dev_core_url("http://127.0.0.1:18769").unwrap();
        assert_eq!(endpoint.addr.port(), 18769);
        assert_eq!(endpoint.url, "http://127.0.0.1:18769");
        for invalid in [
            "http://localhost:18769",
            "http://0.0.0.0:18769",
            "https://127.0.0.1:18769",
            "http://127.0.0.1:0",
            "http://127.0.0.1:65536",
            "http://127.0.0.1:18769/path",
            "http://127.0.0.1:18769?token=secret",
            "http://127.0.0.1:18769@evil.example",
        ] {
            assert!(parse_dev_core_url(invalid).is_err(), "{invalid}");
        }
    }

    #[test]
    fn secret_reference_accepts_only_environment_variable_names() {
        assert!(valid_secret_ref("OPERANT_API_KEY"));
        assert!(valid_secret_ref("_LOCAL_SECRET_2"));
        assert!(!valid_secret_ref("actual-secret-value"));
        assert!(!valid_secret_ref("lowercase"));
        assert!(!valid_secret_ref(""));
    }
}
