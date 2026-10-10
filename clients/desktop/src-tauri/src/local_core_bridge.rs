//! Fixed-route native management bridge. No arbitrary URL or signing oracle is
//! exposed to the WebView. The owning Tauri process must first establish the
//! trusted child and keep its `LocalCallerKey` private.

use std::collections::{BTreeMap, HashSet};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::Value;

use crate::local_caller::{LocalCallerKey, ProofRequest, PROTOCOL};

const MAX_TEXT_BYTES: u64 = 1024 * 1024;
const MAX_RESPONSE_HEADER_BYTES: usize = 16 * 1024;
const MAX_QUERY_BYTES: usize = 4096;
const IDENTITY_PATH: &str = "/internal/local-caller/identity";
const ONBOARDING_SCHEMA: &str =
    include_str!("../../../../sdk/protocol/schema/operant-onboarding.openapi.json");
const PHASE56_SCHEMA: &str =
    include_str!("../../../../sdk/protocol/schema/operant-phase56.openapi.json");
const BETA_SCHEMA: &str = include_str!("../../../../sdk/protocol/schema/operant-beta.openapi.json");
const ONBOARDING_OPERATIONS: &[&str] = &[
    "bootstrapSetup",
    "listModelConnections",
    "createModelConnection",
    "getModelConnectionRequest",
    "deleteModelConnection",
    "discoverConnectionModels",
    "selectConnectionModel",
    "initializeConversation",
    "listConversationMetadata",
    "getConversationMetadata",
    "renameConversation",
    "listLocalApplications",
    "listConversationLocalControlSessions",
    "openConversationLocalControl",
    "startModelOAuth",
    "cancelModelOAuth",
    "getModelOAuthStatus",
    "listSkillSources",
    "addSkillSource",
    "removeSkillSource",
    "getSetupState",
    "listTeamTemplates",
    "startTemplateTeam",
];
const PHASE56_OPERATIONS: &[&str] = &[
    "getLocalControlArtifact",
    "listLocalCapabilityPlugins",
    "installLocalCapabilityPlugin",
    "uninstallLocalCapabilityPlugin",
    "setLocalCapabilityPluginEnabled",
    "listLocalControlSessions",
    "openLocalControlSession",
    "actLocalControlSession",
    "closeLocalControlSession",
    "driveLocalControlSession",
    "observeLocalControlSession",
    "resumeLocalControlSession",
    "takeoverLocalControlSession",
    "listUnknownLocalControlJobs",
    "reconcileUnknownLocalControlJob",
    "enableRemoteHost",
    "getRemoteHost",
    "listRemoteHosts",
    "createPairingChallenge",
    "listRemoteDevices",
    "revokeRemoteDevice",
    "createRemoteSession",
    "closeRemoteSession",
    "listRemoteSessions",
    "getRemoteCommand",
    "reconcileRemoteCommand",
    "listRemoteControlEvents",
    "listExtensions",
    "listExtensionDrivers",
    "inspectExtension",
    "installExtension",
    "uninstallExtension",
    "disableExtension",
    "enableExtension",
    "listExtensionCommands",
    "executeExtensionCommand",
    "listSkillCommands",
    "executeSkillCommand",
    "createWriterWorkspace",
    "listWriterWorkspaces",
    "acquireWriterLease",
    "renewWriterLease",
    "releaseWriterLease",
    "publishWriterArtifact",
    "listWriterArtifacts",
    "detectWriterConflicts",
    "listWriterConflicts",
    "createMergeRun",
    "getMergeRun",
    "listMergeRuns",
    "resolveMergeConflict",
    "finalizeMergeRun",
    "reconcileMergeRun",
];
const BETA_OPERATIONS: &[&str] = &[
    "listRemoteGatewayConnections",
    "getContainerWriter",
    "createContainerWriter",
    "reconcileContainerWriter",
    "removeContainerWriter",
    "startContainerWriter",
    "stopContainerWriter",
];
const REQUEST_HEADERS: &[&str] = &[
    "content-type",
    "accept",
    "idempotency-key",
    "x-operant-client-version",
    "last-event-id",
];
const RESPONSE_HEADERS: &[&str] = &[
    "content-type",
    "cache-control",
    "idempotency-key",
    "idempotency-replayed",
];

/// Fixed error classes. No transport URL, body, header, or underlying error is retained.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum BridgeError {
    InvalidEndpoint,
    UnknownOperation,
    InvalidParameter,
    InvalidQuery,
    InvalidHeader,
    InvalidBody,
    SchemaUnavailable,
    IdentityRejected,
    TransportFailed,
    RedirectRejected,
    ResponseTooLarge,
    InvalidResponse,
}

impl BridgeError {
    pub(crate) fn message(&self) -> &'static str {
        match self {
            Self::InvalidEndpoint => "本机 Core 地址无效。",
            Self::UnknownOperation => "此本机管理操作不可用。",
            Self::InvalidParameter
            | Self::InvalidQuery
            | Self::InvalidHeader
            | Self::InvalidBody => "本机管理请求格式无效。",
            Self::SchemaUnavailable => "本机管理协议不可用。",
            Self::IdentityRejected => "无法确认本机 Core 身份。",
            Self::TransportFailed => "本机 Core 请求失败。",
            Self::RedirectRejected => "本机 Core 返回了不允许的重定向。",
            Self::ResponseTooLarge => "本机 Core 响应过大。",
            Self::InvalidResponse => "本机 Core 响应格式无效。",
        }
    }
}

/// Only these fields may later cross the native command boundary.
#[derive(Serialize)]
pub(crate) struct BridgeResponse {
    pub(crate) status: u16,
    pub(crate) headers: BTreeMap<String, String>,
    pub(crate) text: String,
}

struct OperationSpec {
    method: &'static str,
    path_template: String,
}

pub(crate) struct LocalCoreBridge {
    addr: SocketAddr,
    key: LocalCallerKey,
}

impl LocalCoreBridge {
    pub(crate) fn new(addr: SocketAddr, key: LocalCallerKey) -> Result<Self, BridgeError> {
        if !addr.ip().is_loopback() || addr.port() == 0 {
            return Err(BridgeError::InvalidEndpoint);
        }
        Ok(Self { addr, key })
    }

    /// Reauthenticate the fixed Core before each bounded management request.
    /// The identity operation and its proof are never returned to this caller.
    pub(crate) fn request(
        &self,
        operation_id: &str,
        path_params: &[(&str, &str)],
        raw_query: &str,
        headers: &[(&str, &str)],
        body: &str,
    ) -> Result<BridgeResponse, BridgeError> {
        let operation = operation_spec(operation_id)?;
        let path = expand_path(&operation.path_template, path_params)?;
        validate_query(raw_query)?;
        let headers = validate_headers(operation.method, headers, body)?;
        if body.len() > MAX_TEXT_BYTES as usize {
            return Err(BridgeError::InvalidBody);
        }
        let mut stream = self.connect()?;
        self.verify_identity_on(&mut stream)?;
        let deadline = Instant::now()
            + if operation.method == "GET" {
                Duration::from_secs(8)
            } else {
                Duration::from_secs(300)
            };
        self.send_signed(
            &mut stream,
            operation.method,
            &path,
            raw_query,
            &headers,
            body,
            deadline,
        )?;
        read_response(&mut stream, deadline)
    }

    pub(crate) fn verify_identity(&self) -> Result<(), BridgeError> {
        let mut stream = self.connect()?;
        self.verify_identity_on(&mut stream)
    }

    fn connect(&self) -> Result<TcpStream, BridgeError> {
        let stream = TcpStream::connect_timeout(&self.addr, Duration::from_secs(2))
            .map_err(|_| BridgeError::TransportFailed)?;
        stream
            .set_read_timeout(Some(Duration::from_secs(8)))
            .map_err(|_| BridgeError::TransportFailed)?;
        stream
            .set_write_timeout(Some(Duration::from_secs(8)))
            .map_err(|_| BridgeError::TransportFailed)?;
        Ok(stream)
    }

    fn verify_identity_on(&self, stream: &mut TcpStream) -> Result<(), BridgeError> {
        let deadline = Instant::now() + Duration::from_secs(8);
        let nonce = self.send_signed(stream, "GET", IDENTITY_PATH, "", &[], "", deadline)?;
        let body = read_response(stream, deadline).map_err(|error| {
            if error == BridgeError::RedirectRejected {
                error
            } else {
                BridgeError::IdentityRejected
            }
        })?;
        if body.status != 200 {
            return Err(BridgeError::IdentityRejected);
        }
        let value: Value =
            serde_json::from_str(&body.text).map_err(|_| BridgeError::IdentityRejected)?;
        if value.get("protocol").and_then(Value::as_str) != Some("local-caller.core.v1")
            || !value
                .get("proof")
                .and_then(Value::as_str)
                .is_some_and(|proof| self.key.verify_core_identity(&nonce, proof))
        {
            return Err(BridgeError::IdentityRejected);
        }
        Ok(())
    }

    fn send_signed(
        &self,
        stream: &mut TcpStream,
        method: &str,
        path: &str,
        query: &str,
        headers: &[(String, String)],
        body: &str,
        deadline: Instant,
    ) -> Result<String, BridgeError> {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| BridgeError::TransportFailed)?
            .as_secs();
        let idempotency = headers
            .iter()
            .find(|(name, _)| name == "idempotency-key")
            .map(|(_, value)| value.as_str());
        let signed = self
            .key
            .sign(&ProofRequest {
                method,
                raw_path: path,
                raw_query: query,
                timestamp,
                idempotency_key: idempotency,
                body: body.as_bytes(),
            })
            .map_err(|_| BridgeError::InvalidParameter)?;
        let target = if query.is_empty() {
            path.to_owned()
        } else {
            format!("{path}?{query}")
        };
        let mut request = format!(
            "{method} {target} HTTP/1.1\r\nHost: {}\r\nConnection: keep-alive\r\nAccept-Encoding: identity\r\nContent-Length: {}\r\nX-Operant-Caller-Protocol: {PROTOCOL}\r\nX-Operant-Caller-Timestamp: {}\r\nX-Operant-Caller-Nonce: {}\r\nX-Operant-Caller-Signature: {}\r\n",
            self.addr,
            body.len(),
            signed.timestamp,
            signed.nonce,
            signed.signature,
        );
        for (name, value) in headers {
            request.push_str(&format!("{name}: {value}\r\n"));
        }
        request.push_str("\r\n");
        write_before(stream, request.as_bytes(), deadline)?;
        write_before(stream, body.as_bytes(), deadline)?;
        Ok(signed.nonce)
    }
}

fn operation_spec(id: &str) -> Result<OperationSpec, BridgeError> {
    let schema = if ONBOARDING_OPERATIONS.contains(&id) {
        ONBOARDING_SCHEMA
    } else if PHASE56_OPERATIONS.contains(&id) {
        PHASE56_SCHEMA
    } else if BETA_OPERATIONS.contains(&id) {
        BETA_SCHEMA
    } else {
        return Err(BridgeError::UnknownOperation);
    };
    let root: Value = serde_json::from_str(schema).map_err(|_| BridgeError::SchemaUnavailable)?;
    for (path, methods) in root
        .get("paths")
        .and_then(Value::as_object)
        .ok_or(BridgeError::SchemaUnavailable)?
    {
        for (method, definition) in methods.as_object().ok_or(BridgeError::SchemaUnavailable)? {
            if definition.get("operationId").and_then(Value::as_str) == Some(id) {
                let method = match method.as_str() {
                    "get" => "GET",
                    "post" => "POST",
                    "put" => "PUT",
                    "patch" => "PATCH",
                    "delete" => "DELETE",
                    _ => return Err(BridgeError::SchemaUnavailable),
                };
                if !path.starts_with("/v1/setup/")
                    && !path.starts_with("/v1/local-control/")
                    && !path.starts_with("/v1/remote-control/")
                    && path != "/v1/extensions"
                    && !path.starts_with("/v1/extensions/")
                    && path != "/v1/workbench/extensions/commands"
                    && !path.starts_with("/v1/workbench/threads/")
                    && !path.starts_with("/v1/graph/runs/")
                    && !path.starts_with("/v1/writer-workspaces/")
                    && !path.starts_with("/v1/writer-conflicts/")
                    && path != "/v1/merge-runs"
                    && !path.starts_with("/v1/merge-runs/")
                {
                    return Err(BridgeError::SchemaUnavailable);
                }
                return Ok(OperationSpec {
                    method,
                    path_template: path.clone(),
                });
            }
        }
    }
    Err(BridgeError::SchemaUnavailable)
}

fn expand_path(template: &str, params: &[(&str, &str)]) -> Result<String, BridgeError> {
    let mut path = template.to_owned();
    let mut seen = HashSet::new();
    for (name, value) in params {
        if !seen.insert(*name)
            || value.is_empty()
            || value.len() > 200
            || *value == "."
            || *value == ".."
            || !value.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'~')
            })
        {
            return Err(BridgeError::InvalidParameter);
        }
        let placeholder = format!("{{{name}}}");
        if !path.contains(&placeholder) {
            return Err(BridgeError::InvalidParameter);
        }
        path = path.replace(&placeholder, value);
    }
    if path.contains('{')
        || path.contains('}')
        || path.contains("//")
        || path.len() > 2048
        || path
            .split('/')
            .any(|segment| segment == "." || segment == "..")
    {
        return Err(BridgeError::InvalidParameter);
    }
    Ok(path)
}

fn validate_query(query: &str) -> Result<(), BridgeError> {
    if query.len() > MAX_QUERY_BYTES
        || query
            .bytes()
            .any(|byte| !(0x21..=0x7e).contains(&byte) || byte == b'#')
    {
        return Err(BridgeError::InvalidQuery);
    }
    Ok(())
}

fn validate_headers(
    method: &str,
    headers: &[(&str, &str)],
    body: &str,
) -> Result<Vec<(String, String)>, BridgeError> {
    if method == "GET" && !body.is_empty() {
        return Err(BridgeError::InvalidBody);
    }
    let mut seen = HashSet::new();
    let mut accepted = Vec::with_capacity(headers.len());
    for (name, value) in headers {
        let name = name.to_ascii_lowercase();
        if !REQUEST_HEADERS.contains(&name.as_str())
            || !seen.insert(name.clone())
            || value.is_empty()
            || value.len() > 300
            || value.bytes().any(|byte| !(0x21..=0x7e).contains(&byte))
        {
            return Err(BridgeError::InvalidHeader);
        }
        if name == "content-type" && value != &"application/json" && value != &"text/plain" {
            return Err(BridgeError::InvalidHeader);
        }
        accepted.push((name, (*value).to_owned()));
    }
    if method != "GET" && !seen.contains("idempotency-key") {
        return Err(BridgeError::InvalidHeader);
    }
    if !body.is_empty() && !seen.contains("content-type") {
        return Err(BridgeError::InvalidHeader);
    }
    Ok(accepted)
}

fn read_before(
    stream: &mut TcpStream,
    bytes: &mut [u8],
    deadline: Instant,
) -> Result<(), BridgeError> {
    let mut remaining = bytes;
    while !remaining.is_empty() {
        let time = deadline
            .checked_duration_since(Instant::now())
            .filter(|time| !time.is_zero())
            .ok_or(BridgeError::TransportFailed)?;
        stream
            .set_read_timeout(Some(time))
            .map_err(|_| BridgeError::TransportFailed)?;
        let size = stream
            .read(remaining)
            .map_err(|_| BridgeError::TransportFailed)?;
        if size == 0 {
            return Err(BridgeError::TransportFailed);
        }
        remaining = &mut remaining[size..];
    }
    Ok(())
}

fn write_before(
    stream: &mut TcpStream,
    bytes: &[u8],
    deadline: Instant,
) -> Result<(), BridgeError> {
    let mut remaining = bytes;
    while !remaining.is_empty() {
        let time = deadline
            .checked_duration_since(Instant::now())
            .filter(|time| !time.is_zero())
            .ok_or(BridgeError::TransportFailed)?;
        stream
            .set_write_timeout(Some(time))
            .map_err(|_| BridgeError::TransportFailed)?;
        let size = stream
            .write(remaining)
            .map_err(|_| BridgeError::TransportFailed)?;
        if size == 0 {
            return Err(BridgeError::TransportFailed);
        }
        remaining = &remaining[size..];
    }
    Ok(())
}

fn read_response(stream: &mut TcpStream, deadline: Instant) -> Result<BridgeResponse, BridgeError> {
    // Bytewise header reads avoid consuming bytes past the exact framed response.
    // That matters because the authenticated request follows on this same socket.
    let mut header_bytes = Vec::new();
    let mut next = [0u8; 1];
    loop {
        if header_bytes.len() >= MAX_RESPONSE_HEADER_BYTES {
            return Err(BridgeError::ResponseTooLarge);
        }
        read_before(stream, &mut next, deadline)?;
        header_bytes.push(next[0]);
        if header_bytes.ends_with(b"\r\n\r\n") {
            break;
        }
    }
    let header_text =
        std::str::from_utf8(&header_bytes).map_err(|_| BridgeError::InvalidResponse)?;
    let mut lines = header_text.split("\r\n");
    let status_line = lines.next().ok_or(BridgeError::InvalidResponse)?;
    if !status_line
        .bytes()
        .all(|byte| (0x20..=0x7e).contains(&byte))
    {
        return Err(BridgeError::InvalidResponse);
    }
    let mut status_parts = status_line.split(' ');
    if status_parts.next() != Some("HTTP/1.1") {
        return Err(BridgeError::InvalidResponse);
    }
    let code = status_parts.next().ok_or(BridgeError::InvalidResponse)?;
    if code.len() != 3 || !code.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(BridgeError::InvalidResponse);
    }
    let status: u16 = code.parse().map_err(|_| BridgeError::InvalidResponse)?;
    if !(200..=599).contains(&status) || status == 205 || status == 304 {
        return Err(BridgeError::InvalidResponse);
    }
    if (300..400).contains(&status) {
        return Err(BridgeError::RedirectRejected);
    }
    let mut seen = HashSet::new();
    let mut content_length = None;
    let mut headers = BTreeMap::new();
    for line in lines {
        if line.is_empty() {
            continue;
        }
        let (name, value) = line.split_once(':').ok_or(BridgeError::InvalidResponse)?;
        if name.is_empty()
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
            || !value
                .bytes()
                .all(|byte| byte == b'\t' || (0x20..=0x7e).contains(&byte))
        {
            return Err(BridgeError::InvalidResponse);
        }
        let name = name.to_ascii_lowercase();
        if !seen.insert(name.clone()) || name == "transfer-encoding" {
            return Err(BridgeError::InvalidResponse);
        }
        let value = value.trim();
        if name == "content-encoding" && value != "identity" {
            return Err(BridgeError::InvalidResponse);
        }
        if name == "content-length" {
            if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
                return Err(BridgeError::InvalidResponse);
            }
            content_length = Some(
                value
                    .parse::<u64>()
                    .map_err(|_| BridgeError::InvalidResponse)?,
            );
        }
        if RESPONSE_HEADERS.contains(&name.as_str()) && value.len() <= 300 {
            headers.insert(name, value.to_owned());
        }
    }
    let length = if status == 204 {
        if content_length.is_some_and(|length| length != 0) {
            return Err(BridgeError::InvalidResponse);
        }
        0
    } else {
        content_length.ok_or(BridgeError::InvalidResponse)?
    };
    if length > MAX_TEXT_BYTES {
        return Err(BridgeError::ResponseTooLarge);
    }
    let mut bytes = vec![0; length as usize];
    read_before(stream, &mut bytes, deadline)?;
    let text = String::from_utf8(bytes).map_err(|_| BridgeError::InvalidResponse)?;
    Ok(BridgeResponse {
        status,
        headers,
        text,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use hmac::{Hmac, Mac};
    use sha2::{Digest, Sha256};
    use std::io::Write;
    use std::net::{IpAddr, Ipv4Addr, TcpListener, TcpStream};
    use std::thread;

    fn read_request(stream: &mut TcpStream) -> String {
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        let mut bytes = Vec::new();
        loop {
            let mut next = [0u8; 1];
            stream.read_exact(&mut next).unwrap();
            bytes.push(next[0]);
            if bytes.ends_with(b"\r\n\r\n") {
                break;
            }
        }
        let header_text = String::from_utf8(bytes.clone()).unwrap();
        let length: usize = header(&header_text, "content-length").parse().unwrap();
        let mut body = vec![0u8; length];
        stream.read_exact(&mut body).unwrap();
        bytes.extend_from_slice(&body);
        String::from_utf8(bytes).unwrap()
    }

    fn header<'a>(request: &'a str, name: &str) -> &'a str {
        request
            .lines()
            .find_map(|line| {
                let (key, value) = line.split_once(':')?;
                key.eq_ignore_ascii_case(name).then_some(value.trim())
            })
            .unwrap()
    }

    fn reply(stream: &mut TcpStream, status: &str, body: &str, extra: &str) {
        write!(stream, "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: keep-alive\r\n{extra}\r\n{body}", body.len()).unwrap();
        stream.flush().unwrap();
    }

    fn test_bridge(listener: &TcpListener) -> (LocalCoreBridge, Vec<u8>) {
        let key = LocalCallerKey::generate().unwrap();
        let mut bootstrap = Vec::new();
        key.write_bootstrap(&mut bootstrap).unwrap();
        (
            LocalCoreBridge::new(listener.local_addr().unwrap(), key).unwrap(),
            bootstrap,
        )
    }

    fn core_proof(key: &[u8], nonce: &str) -> String {
        let mut mac = Hmac::<Sha256>::new_from_slice(key).unwrap();
        mac.update(b"local-caller.core.v1\n");
        mac.update(nonce.as_bytes());
        mac.finalize()
            .into_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect()
    }

    #[test]
    fn schema_allowlist_is_complete_for_supported_management_operations() {
        let onboarding: Value = serde_json::from_str(ONBOARDING_SCHEMA).unwrap();
        let phase56: Value = serde_json::from_str(PHASE56_SCHEMA).unwrap();
        let beta: Value = serde_json::from_str(BETA_SCHEMA).unwrap();
        let collect = |schema: &Value, prefix: &str| -> HashSet<String> {
            schema["paths"]
                .as_object()
                .unwrap()
                .iter()
                .filter(|(path, _)| path.starts_with(prefix))
                .flat_map(|(_, methods)| methods.as_object().unwrap().values())
                .filter_map(|method| method.get("operationId").and_then(Value::as_str))
                .map(str::to_owned)
                .collect()
        };
        assert_eq!(
            collect(&onboarding, "/v1/setup/"),
            ONBOARDING_OPERATIONS
                .iter()
                .map(|id| (*id).to_owned())
                .collect()
        );
        assert_eq!(
            collect(&phase56, "/v1/local-control/"),
            PHASE56_OPERATIONS
                .iter()
                .filter(|id| id.contains("LocalControl")
                    || id.contains("LocalCapability")
                    || id.contains("UnknownLocalControl"))
                .map(|id| (*id).to_owned())
                .collect()
        );
        let expected_phase56: HashSet<String> = [
            "/v1/local-control/",
            "/v1/remote-control/",
            "/v1/extensions",
            "/v1/workbench/extensions/commands",
            "/v1/workbench/threads/",
            "/v1/graph/runs/",
            "/v1/writer-workspaces/",
            "/v1/writer-conflicts/",
            "/v1/merge-runs",
        ]
        .iter()
        .flat_map(|prefix| collect(&phase56, prefix))
        .filter(|id| {
            !matches!(
                id.as_str(),
                "pairRemoteDevice" | "submitRemoteCommand" | "queryRemoteSessionResult"
            )
        })
        .collect();
        assert_eq!(
            expected_phase56,
            PHASE56_OPERATIONS
                .iter()
                .map(|id| (*id).to_owned())
                .collect()
        );
        let expected_beta: HashSet<String> = [
            "/v1/remote-control/gateway/connections",
            "/v1/writer-workspaces/",
        ]
        .iter()
        .flat_map(|prefix| collect(&beta, prefix))
        .collect();
        assert_eq!(
            expected_beta,
            BETA_OPERATIONS.iter().map(|id| (*id).to_owned()).collect()
        );
        for id in ONBOARDING_OPERATIONS
            .iter()
            .chain(PHASE56_OPERATIONS)
            .chain(BETA_OPERATIONS)
        {
            assert!(operation_spec(id).is_ok(), "{id}");
        }
        for id in [
            "pairRemoteDevice",
            "submitRemoteCommand",
            "queryRemoteSessionResult",
            "/internal/local-caller/identity",
        ] {
            assert!(matches!(
                operation_spec(id),
                Err(BridgeError::UnknownOperation)
            ));
        }
    }

    #[test]
    fn rejects_untrusted_endpoint_path_query_headers_and_body_before_network() {
        assert!(matches!(
            LocalCoreBridge::new(
                SocketAddr::new(IpAddr::V4(Ipv4Addr::UNSPECIFIED), 8000),
                LocalCallerKey::generate().unwrap()
            ),
            Err(BridgeError::InvalidEndpoint)
        ));
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let (bridge, _) = test_bridge(&listener);
        let bad = [
            bridge
                .request("notAnOperation", &[], "", &[], "")
                .err()
                .unwrap(),
            bridge
                .request(
                    "getModelOAuthStatus",
                    &[("attempt_id", "../escape")],
                    "",
                    &[],
                    "",
                )
                .err()
                .unwrap(),
            bridge
                .request("getModelOAuthStatus", &[("attempt_id", "a/b")], "", &[], "")
                .err()
                .unwrap(),
            bridge
                .request("getSetupState", &[], "a=1#fragment", &[], "")
                .err()
                .unwrap(),
            bridge
                .request("getSetupState", &[], "", &[("Host", "evil.example")], "")
                .err()
                .unwrap(),
            bridge
                .request(
                    "getSetupState",
                    &[],
                    "",
                    &[("Authorization", "Bearer x")],
                    "",
                )
                .err()
                .unwrap(),
            bridge
                .request(
                    "getSetupState",
                    &[],
                    "",
                    &[("X-Operant-Caller-Signature", "oracle")],
                    "",
                )
                .err()
                .unwrap(),
            bridge
                .request(
                    "getSetupState",
                    &[],
                    "",
                    &[("Accept", "application/json"), ("accept", "text/plain")],
                    "",
                )
                .err()
                .unwrap(),
            bridge
                .request("addSkillSource", &[], "", &[], "{}")
                .err()
                .unwrap(),
            bridge
                .request(
                    "addSkillSource",
                    &[],
                    "",
                    &[
                        ("Idempotency-Key", "key-1"),
                        ("Content-Type", "application/json"),
                    ],
                    &"x".repeat(MAX_TEXT_BYTES as usize + 1),
                )
                .err()
                .unwrap(),
        ];
        assert_eq!(bad[0], BridgeError::UnknownOperation);
        assert_eq!(bad[1], BridgeError::InvalidParameter);
        assert_eq!(bad[2], BridgeError::InvalidParameter);
        assert_eq!(bad[3], BridgeError::InvalidQuery);
        assert!(bad[4..8]
            .iter()
            .all(|error| *error == BridgeError::InvalidHeader));
        assert_eq!(bad[8], BridgeError::InvalidHeader);
        assert_eq!(bad[9], BridgeError::InvalidBody);
        assert_eq!(
            bridge
                .request(
                    "getSetupState",
                    &[],
                    "",
                    &[("Content-Type", "application/json")],
                    "{}",
                )
                .err(),
            Some(BridgeError::InvalidBody)
        );
    }

    #[test]
    fn dotted_plugin_id_is_safe_but_dot_segments_are_rejected() {
        let template = &operation_spec("uninstallLocalCapabilityPlugin")
            .unwrap()
            .path_template;
        assert_eq!(
            expand_path(template, &[("plugin_id", "com.example.plugin-v1")]).unwrap(),
            "/v1/local-control/plugins/com.example.plugin-v1"
        );
        for value in [".", "..", "a/b", "a%2fb"] {
            assert_eq!(
                expand_path(template, &[("plugin_id", value)]).err(),
                Some(BridgeError::InvalidParameter)
            );
        }
    }

    #[test]
    fn valid_identity_allows_only_fixed_management_response() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let (bridge, key) = test_bridge(&listener);
        let server = thread::spawn(move || {
            let (mut first, _) = listener.accept().unwrap();
            let identity = read_request(&mut first);
            assert!(identity.starts_with("GET /internal/local-caller/identity HTTP/1.1"));
            let nonce = header(&identity, "x-operant-caller-nonce");
            let proof = core_proof(&key, nonce);
            reply(
                &mut first,
                "200 OK",
                &format!("{{\"protocol\":\"local-caller.core.v1\",\"proof\":\"{proof}\"}}"),
                "",
            );
            let management = read_request(&mut first);
            assert!(management.starts_with("GET /v1/setup/state?b=2&a=1 HTTP/1.1"));
            assert_eq!(header(&management, "accept"), "application/json");
            assert_eq!(header(&management, "x-operant-caller-protocol"), PROTOCOL);
            reply(
                &mut first,
                "200 OK",
                "{\"ok\":true}",
                "Cache-Control: no-store\r\nIdempotency-Replayed: true\r\nX-Secret: sentinel\r\n",
            );
        });
        let result = bridge
            .request(
                "getSetupState",
                &[],
                "b=2&a=1",
                &[("Accept", "application/json")],
                "",
            )
            .unwrap();
        assert_eq!(result.status, 200);
        assert_eq!(result.text, "{\"ok\":true}");
        assert_eq!(
            result.headers.get("cache-control").map(String::as_str),
            Some("no-store")
        );
        assert_eq!(
            result
                .headers
                .get("idempotency-replayed")
                .map(String::as_str),
            Some("true")
        );
        assert!(!result.headers.contains_key("x-secret"));
        server.join().unwrap();
    }

    #[test]
    fn schema_method_dynamic_path_and_body_are_signed_exactly() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let (bridge, key) = test_bridge(&listener);
        let server = thread::spawn(move || {
            let (mut first, _) = listener.accept().unwrap();
            let identity = read_request(&mut first);
            let proof = core_proof(&key, header(&identity, "x-operant-caller-nonce"));
            reply(
                &mut first,
                "200 OK",
                &format!("{{\"protocol\":\"local-caller.core.v1\",\"proof\":\"{proof}\"}}"),
                "",
            );
            let management = read_request(&mut first);
            assert!(management
                .starts_with("PATCH /v1/setup/conversations/thread_1/metadata?b=2&a=1 HTTP/1.1"));
            assert_eq!(header(&management, "content-type"), "application/json");
            assert_eq!(header(&management, "idempotency-key"), "rename-1");
            let canonical = format!(
                "{PROTOCOL}\nPATCH\n/v1/setup/conversations/thread_1/metadata?b=2&a=1\n{}\n{}\nrename-1\n{:x}",
                header(&management, "x-operant-caller-timestamp"),
                header(&management, "x-operant-caller-nonce"),
                Sha256::digest(b"{\"title\":\"safe\"}")
            );
            let mut mac = Hmac::<Sha256>::new_from_slice(&key).unwrap();
            mac.update(canonical.as_bytes());
            let expected: String = mac
                .finalize()
                .into_bytes()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect();
            assert_eq!(header(&management, "x-operant-caller-signature"), expected);
            reply(&mut first, "200 OK", "{}", "");
        });
        let response = bridge
            .request(
                "renameConversation",
                &[("thread_id", "thread_1")],
                "b=2&a=1",
                &[
                    ("Content-Type", "application/json"),
                    ("Idempotency-Key", "rename-1"),
                ],
                "{\"title\":\"safe\"}",
            )
            .unwrap();
        assert_eq!(response.status, 200);
        server.join().unwrap();
    }

    #[test]
    fn spoofed_identity_and_redirect_never_reach_management_request() {
        for (status, body, expected) in [
            ("200 OK", "{\"protocol\":\"local-caller.core.v1\",\"proof\":\"0000000000000000000000000000000000000000000000000000000000000000\"}", BridgeError::IdentityRejected),
            ("302 Found", "{}", BridgeError::RedirectRejected),
        ] {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let (bridge, _) = test_bridge(&listener);
            let server = thread::spawn(move || {
                let (mut first, _) = listener.accept().unwrap();
                let _ = read_request(&mut first);
                reply(&mut first, status, body, "Location: http://example.invalid/\r\n");
                listener.set_nonblocking(true).unwrap();
                thread::sleep(Duration::from_millis(100));
                assert!(listener.accept().is_err());
            });
            assert_eq!(bridge.request("getSetupState", &[], "", &[], "").err(), Some(expected));
            server.join().unwrap();
        }
    }

    #[test]
    fn oversized_management_response_is_rejected() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let (bridge, key) = test_bridge(&listener);
        let server = thread::spawn(move || {
            let (mut first, _) = listener.accept().unwrap();
            let identity = read_request(&mut first);
            let proof = core_proof(&key, header(&identity, "x-operant-caller-nonce"));
            reply(
                &mut first,
                "200 OK",
                &format!("{{\"protocol\":\"local-caller.core.v1\",\"proof\":\"{proof}\"}}"),
                "",
            );
            let _ = read_request(&mut first);
            write!(
                first,
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                MAX_TEXT_BYTES + 1
            )
            .unwrap();
        });
        assert_eq!(
            bridge.request("getSetupState", &[], "", &[], "").err(),
            Some(BridgeError::ResponseTooLarge)
        );
        server.join().unwrap();
    }

    #[test]
    fn closed_authenticated_socket_never_reconnects_for_management_body() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let (bridge, key) = test_bridge(&listener);
        let server = thread::spawn(move || {
            let (mut trusted, _) = listener.accept().unwrap();
            let identity = read_request(&mut trusted);
            let proof = core_proof(&key, header(&identity, "x-operant-caller-nonce"));
            reply(
                &mut trusted,
                "200 OK",
                &format!("{{\"protocol\":\"local-caller.core.v1\",\"proof\":\"{proof}\"}}"),
                "",
            );
            trusted.shutdown(std::net::Shutdown::Both).unwrap();
            drop(trusted);
            // A replacement listener on the same address would only see a new
            // TCP connection. Keep this listener to prove no reconnect occurs.
            listener.set_nonblocking(true).unwrap();
            thread::sleep(Duration::from_millis(100));
            assert!(listener.accept().is_err());
        });
        assert_eq!(
            bridge
                .request(
                    "addSkillSource",
                    &[],
                    "",
                    &[
                        ("Idempotency-Key", "new-skill-1"),
                        ("Content-Type", "application/json"),
                    ],
                    "{\"sentinel\":\"PRIVATE_BODY\"}",
                )
                .err(),
            Some(BridgeError::TransportFailed)
        );
        server.join().unwrap();
    }

    #[test]
    fn ambiguous_or_truncated_http_framing_fails_closed() {
        let cases = [
            ("HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nab", BridgeError::TransportFailed),
            ("HTTP/1.1 200 OK\r\n\r\n{}", BridgeError::InvalidResponse),
            ("HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}", BridgeError::InvalidResponse),
            ("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n", BridgeError::InvalidResponse),
            ("HTTP/1.1 200 OK\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}", BridgeError::InvalidResponse),
            ("HTTP/1.1 302 Found\r\nContent-Length: 0\r\nLocation: http://example.invalid/\r\n\r\n", BridgeError::RedirectRejected),
            ("HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\n\r\n", BridgeError::ResponseTooLarge),
            ("HTTP/1.1 200\nInjected: yes\r\nContent-Length: 2\r\n\r\n{}", BridgeError::InvalidResponse),
        ];
        for (response, expected) in cases {
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let addr = listener.local_addr().unwrap();
            let server = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                stream.write_all(response.as_bytes()).unwrap();
            });
            let mut client = TcpStream::connect(addr).unwrap();
            assert_eq!(
                read_response(&mut client, Instant::now() + Duration::from_secs(2)).err(),
                Some(expected)
            );
            server.join().unwrap();
        }
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let _ = stream.write_all(
                format!(
                    "HTTP/1.1 200 OK\r\nX-Large: {}\r\n\r\n",
                    "a".repeat(MAX_RESPONSE_HEADER_BYTES)
                )
                .as_bytes(),
            );
        });
        let mut client = TcpStream::connect(addr).unwrap();
        assert_eq!(
            read_response(&mut client, Instant::now() + Duration::from_secs(2)).err(),
            Some(BridgeError::ResponseTooLarge)
        );
        server.join().unwrap();
    }
}
