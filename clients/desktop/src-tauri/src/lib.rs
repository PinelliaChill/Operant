use serde::Serialize;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Manager, State};

const CORE_ADDR: SocketAddr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 8000);

struct CoreEndpoint {
    addr: SocketAddr,
    url: String,
    external_dev_core: bool,
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
        external_dev_core: true,
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
        external_dev_core: false,
    })
}

#[derive(Default)]
struct CoreProcess(Mutex<Option<Child>>);

impl Drop for CoreProcess {
    fn drop(&mut self) {
        if let Ok(child_slot) = self.0.get_mut() {
            if let Some(child) = child_slot.as_mut() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct CoreStatus {
    reachable: bool,
    managed_process_running: bool,
    endpoint: String,
}

fn core_reachable(endpoint: &CoreEndpoint) -> bool {
    let Ok(mut stream) = TcpStream::connect_timeout(&endpoint.addr, Duration::from_millis(250))
    else {
        return false;
    };
    let timeout = Some(Duration::from_millis(500));
    if stream.set_read_timeout(timeout).is_err() || stream.set_write_timeout(timeout).is_err() {
        return false;
    }
    if stream
        .write_all(
            format!(
                "GET /healthz HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n\r\n",
                endpoint.addr
            )
            .as_bytes(),
        )
        .is_err()
    {
        return false;
    }
    let mut response = String::new();
    if stream.take(4096).read_to_string(&mut response).is_err() {
        return false;
    }
    (response.starts_with("HTTP/1.1 200") || response.starts_with("HTTP/1.0 200"))
        && response.contains("\r\n\r\n{\"status\":\"ok\"}")
}

#[tauri::command]
fn core_status(state: State<'_, CoreProcess>) -> Result<CoreStatus, String> {
    let endpoint = configured_endpoint()?;
    let mut guard = state.0.lock().map_err(|_| "Core process lock failed")?;
    let managed_process_running = match guard.as_mut() {
        Some(child) => child
            .try_wait()
            .map_err(|_| "Could not inspect managed Core")?
            .is_none(),
        None => false,
    };
    if !managed_process_running {
        *guard = None;
    }
    Ok(CoreStatus {
        reachable: core_reachable(&endpoint),
        managed_process_running,
        endpoint: endpoint.url,
    })
}

#[tauri::command]
fn start_local_core(state: State<'_, CoreProcess>) -> Result<CoreStatus, String> {
    let endpoint = configured_endpoint()?;
    if core_reachable(&endpoint) {
        return core_status(state);
    }
    if endpoint.external_dev_core {
        return Err("Configured local development Core is unavailable".into());
    }
    let mut guard = state.0.lock().map_err(|_| "Core process lock failed")?;
    if guard.is_none() {
        // No user-controlled executable, host, port, workspace, or shell text
        // crosses this boundary. Agent actions remain REST Commands handled by
        // Core and Action Gateway.
        let child = Command::new("operant")
            .args([
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--desktop",
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|error| format!("Could not launch local Operant Core: {error}"))?;
        *guard = Some(child);
    }
    Ok(CoreStatus {
        reachable: core_reachable(&endpoint),
        managed_process_running: true,
        endpoint: endpoint.url,
    })
}

#[tauri::command]
fn stop_managed_core(state: State<'_, CoreProcess>) -> Result<(), String> {
    let mut guard = state.0.lock().map_err(|_| "Core process lock failed")?;
    if let Some(mut child) = guard.take() {
        child
            .kill()
            .map_err(|error| format!("Could not stop managed Core: {error}"))?;
        child
            .wait()
            .map_err(|error| format!("Could not reap managed Core: {error}"))?;
    }
    Ok(())
}

fn valid_secret_ref(value: &str) -> bool {
    let mut chars = value.chars();
    matches!(chars.next(), Some('A'..='Z' | '_'))
        && value.len() <= 128
        && chars.all(|character| matches!(character, 'A'..='Z' | '0'..='9' | '_'))
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
        .manage(CoreProcess::default())
        .setup(|app| {
            start_local_core(app.state::<CoreProcess>())?;
            let endpoint = configured_endpoint()?;
            for _ in 0..50 {
                if core_reachable(&endpoint) {
                    return Ok(());
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            Err("Operant Core did not become ready within five seconds".into())
        })
        .invoke_handler(tauri::generate_handler![
            core_status,
            start_local_core,
            stop_managed_core,
            persist_secret_reference
        ])
        .run(tauri::generate_context!())
        .expect("error while running Operant desktop shell");
}

#[cfg(test)]
mod tests {
    use super::{parse_dev_core_url, valid_secret_ref};

    #[test]
    fn dev_core_url_accepts_only_explicit_loopback_http_port() {
        let endpoint = parse_dev_core_url("http://127.0.0.1:18769").unwrap();
        assert_eq!(endpoint.addr.port(), 18769);
        assert_eq!(endpoint.url, "http://127.0.0.1:18769");
        assert!(endpoint.external_dev_core);
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
