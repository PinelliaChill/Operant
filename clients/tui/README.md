# Operant Textual TUI

The TUI is an optional client. Its wheel depends on the exact matching
`operant-agent` release, which ships the generated Python clients from
`sdk/python_client`; it does not read SQLite, logs, or the Core event bus.

```bash
python -m pip install -e clients/tui
operant-tui --core-url http://127.0.0.1:8000
```

`Textual` and `Rich` are optional UI dependencies of the main Operant Core and
are installed only with this standalone client package. The URL is not allowed
to contain credentials. Runtime secrets remain Core-owned `secret_ref` values.
