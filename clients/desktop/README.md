# Operant Tauri v2 shell

This directory packages the existing React GUI as a thin desktop shell. Rust
owns only window lifecycle, localhost Core reachability/start/stop, and storage of
an environment-variable **reference name**. It has no Agent action, PTY,
SQLite, approval, policy, remote-device, or recovery implementation.

```bash
cd clients/gui && npm ci && npm run build
cd ../desktop/src-tauri && cargo check
```

`cargo tauri dev` additionally needs the platform WebView prerequisites. The
Beta/RC build emits an unsigned macOS `.app`; DMG, signing, notarization and
automatic update remain release gates rather than being implied by this
candidate. Core launch expects the repository-supported `operant` executable
to be available on `PATH`, or an already-running Core at `127.0.0.1:8000`.
No executable, host, port, workspace, or shell text is accepted from WebView
input.
