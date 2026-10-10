//! Owned Core lifecycle. The WebView cannot choose an executable or receive a key.

use std::io::ErrorKind;
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use crate::local_caller::LocalCallerKey;
use crate::local_core_bridge::LocalCoreBridge;

pub(crate) struct ManagedCore {
    child: Child,
    pub(crate) bridge: Arc<LocalCoreBridge>,
}

impl ManagedCore {
    /// Inputs come from the native installation/configuration, never JS.
    /// The installed executable and the owning OS account are trust premises.
    pub(crate) fn spawn(
        executable: &Path,
        directory: &Path,
        database: &Path,
        addr: SocketAddr,
    ) -> Result<Self, String> {
        if !addr.ip().is_loopback() || addr.port() == 0 {
            return Err("本机 Core 地址无效。".into());
        }
        if !executable.is_absolute() || !executable.is_file() {
            return Err("未找到已安装的 Operant Core。".into());
        }
        if !directory.is_absolute() || !directory.is_dir() || !database.is_absolute() {
            return Err("本机 Core 数据目录无效。".into());
        }
        ensure_port_available(addr)?;
        let key = LocalCallerKey::generate().map_err(|_| "无法初始化本机连接。")?;
        let mut child = Command::new(executable)
            .args([
                "serve",
                "--host",
                &addr.ip().to_string(),
                "--port",
                &addr.port().to_string(),
                "--desktop",
                "--desktop-auth-stdio",
            ])
            .current_dir(directory)
            .env("OPERANT_DB_PATH", database)
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "无法启动本机 Core。")?;
        // EOF is part of the bounded bootstrap. If any write fails, discard the
        // entire child; never complete or replay a partial key message.
        let written = child
            .stdin
            .take()
            .ok_or(())
            .and_then(|mut pipe| key.write_bootstrap(&mut pipe).map_err(|_| ()));
        if written.is_err() {
            stop_child(&mut child);
            return Err("无法初始化本机连接。".into());
        }
        let bridge = match LocalCoreBridge::new(addr, key) {
            Ok(bridge) => bridge,
            Err(_) => {
                stop_child(&mut child);
                return Err("无法初始化本机连接。".into());
            }
        };
        let mut owned = Self {
            child,
            bridge: Arc::new(bridge),
        };
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            if !owned.running()? {
                return Err("本机 Core 启动失败。".into());
            }
            if owned.bridge.verify_identity().is_ok() {
                if owned.running()? {
                    return Ok(owned);
                }
                return Err("本机 Core 已停止。".into());
            }
            thread::sleep(Duration::from_millis(100));
        }
        Err("无法确认本机 Core 身份。".into())
    }

    pub(crate) fn running(&mut self) -> Result<bool, String> {
        self.child
            .try_wait()
            .map(|status| status.is_none())
            .map_err(|_| "无法读取本机 Core 状态。".into())
    }

    pub(crate) fn verified(&mut self) -> bool {
        self.running().unwrap_or(false) && self.bridge.verify_identity().is_ok()
    }
}

impl Drop for ManagedCore {
    fn drop(&mut self) {
        stop_child(&mut self.child);
    }
}

fn stop_child(child: &mut Child) {
    if matches!(child.try_wait(), Ok(None)) {
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn ensure_port_available(addr: SocketAddr) -> Result<(), String> {
    match TcpStream::connect_timeout(&addr, Duration::from_millis(250)) {
        Ok(_) => Err("本机端口已被占用，请先确认现有 Core 的归属。".into()),
        Err(error) if error.kind() == ErrorKind::ConnectionRefused => Ok(()),
        Err(_) => Err("无法核对本机 Core 端口。".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::{ensure_port_available, ManagedCore};
    use std::io::{Read, Write};
    use std::net::{TcpListener, TcpStream};
    use std::path::PathBuf;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    #[test]
    fn occupied_port_is_rejected_without_using_the_existing_service() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        assert!(ensure_port_available(listener.local_addr().unwrap()).is_err());
    }

    #[test]
    fn an_unused_loopback_port_can_start_a_new_owned_core() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        assert!(ensure_port_available(addr).is_ok());
    }

    /// Real CLI acceptance is opt-in; ordinary unit tests never launch Core.
    /// Use a matching installed CLI and an otherwise credential-free environment.
    #[test]
    #[ignore = "requires OPERANT_VERIFY_CORE_EXECUTABLE and a matching installed Core"]
    fn private_native_parent_starts_queries_and_stops_its_real_core() {
        let executable = PathBuf::from(
            std::env::var_os("OPERANT_VERIFY_CORE_EXECUTABLE")
                .expect("set OPERANT_VERIFY_CORE_EXECUTABLE to a matching installed CLI"),
        );
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory = std::env::temp_dir().join(format!(
            "operant-native-bootstrap-{}-{suffix}",
            std::process::id()
        ));
        std::fs::create_dir(&directory).unwrap();
        let database = directory.join("operant.sqlite3");
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        drop(listener);
        let mut owned = ManagedCore::spawn(&executable, &directory, &database, addr)
            .expect("private native bootstrap must start a verified owned child");
        assert!(owned.running().unwrap());
        assert!(owned.verified());

        let mut unsigned = TcpStream::connect_timeout(&addr, Duration::from_secs(2)).unwrap();
        unsigned
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        write!(
            unsigned,
            "GET /v1/setup/skill-sources HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n\r\n"
        )
        .unwrap();
        let mut denied = String::new();
        unsigned.read_to_string(&mut denied).unwrap();
        assert!(denied.starts_with("HTTP/1.1 403 "));

        let state = owned
            .bridge
            .request("getSetupState", &[], "", &[], "")
            .unwrap();
        assert_eq!(state.status, 200);
        let value: serde_json::Value = serde_json::from_str(&state.text).unwrap();
        assert_eq!(
            value.get("ready").and_then(serde_json::Value::as_bool),
            Some(false)
        );
        let sources = owned
            .bridge
            .request("listSkillSources", &[], "", &[], "")
            .unwrap();
        assert_eq!(sources.status, 200);
        let value: serde_json::Value = serde_json::from_str(&sources.text).unwrap();
        let source_count = value["items"].as_array().unwrap().len();
        assert!(source_count > 0);
        // Reach the formal write route without storing or calling a real key.
        let rejected = owned
            .bridge
            .request(
                "createModelConnection",
                &[],
                "",
                &[
                    ("content-type", "application/json"),
                    ("idempotency-key", "native-invalid-key"),
                ],
                r#"{"provider":"openai-compatible","api_key":null}"#,
            )
            .unwrap();
        assert_eq!(rejected.status, 400);
        assert!(!directory.join(".env").exists());
        drop(owned);
        assert!(TcpStream::connect_timeout(&addr, Duration::from_secs(1)).is_err());
        println!(
            "{}",
            serde_json::json!({
                "native_parent": true,
                "private_pipe": true,
                "unsigned_source_denied": true,
                "formal_source_and_state_queries": true,
                "formal_invalid_write_reached_without_credentials": true,
                "source_count": source_count,
                "database": database,
                "port": addr.port(),
                "owned_child_stopped": true,
                "model_credentials_used": false,
            })
        );
    }
}
