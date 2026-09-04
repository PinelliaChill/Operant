# Operant Tauri v2 shell

This directory packages the existing React GUI as a thin desktop shell. Rust
owns only window lifecycle, localhost Core reachability/start/stop, and storage of
an environment-variable **reference name**. It has no Agent action, PTY,
SQLite, approval, policy, remote-device, or recovery implementation.

```bash
cd clients/gui && npm ci && npm run build
cd ../desktop/src-tauri && cargo check
```

`cargo tauri dev` additionally needs the platform WebView prerequisites. A
successful `cargo check` is configuration/compile evidence, not real desktop
shell acceptance. Core launch expects the repository-supported `uvicorn`
executable to be available on `PATH`; no executable, host, port, workspace, or
shell text is accepted from WebView input.
