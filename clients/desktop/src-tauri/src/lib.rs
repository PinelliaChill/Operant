use serde::Serialize;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Manager, State};

const CORE_ADDR: SocketAddr = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 8000);

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
    endpoint: &'static str,
}

fn core_reachable() -> bool {
    TcpStream::connect_timeout(&CORE_ADDR, Duration::from_millis(250)).is_ok()
}

#[tauri::command]
fn core_status(state: State<'_, CoreProcess>) -> Result<CoreStatus, String> {
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
        reachable: core_reachable(),
        managed_process_running,
        endpoint: "http://127.0.0.1:8000",
    })
}

#[tauri::command]
fn start_local_core(state: State<'_, CoreProcess>) -> Result<CoreStatus, String> {
    if core_reachable() {
        return core_status(state);
    }
    let mut guard = state.0.lock().map_err(|_| "Core process lock failed")?;
    if guard.is_none() {
        // No user-controlled executable, host, port, workspace, or shell text
        // crosses this boundary. Agent actions remain REST Commands handled by
        // Core and Action Gateway.
        let child = Command::new("uvicorn")
            .args(["operant.api:app", "--host", "127.0.0.1", "--port", "8000"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|error| format!("Could not launch local Operant Core: {error}"))?;
        *guard = Some(child);
    }
    Ok(CoreStatus {
        reachable: core_reachable(),
        managed_process_running: true,
        endpoint: "http://127.0.0.1:8000",
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
    use super::valid_secret_ref;

    #[test]
    fn secret_reference_accepts_only_environment_variable_names() {
        assert!(valid_secret_ref("OPERANT_API_KEY"));
        assert!(valid_secret_ref("_LOCAL_SECRET_2"));
        assert!(!valid_secret_ref("actual-secret-value"));
        assert!(!valid_secret_ref("lowercase"));
        assert!(!valid_secret_ref(""));
    }
}
