"""Core-owned management state schema, included in the v15 migration."""

SCHEMA_SQL = """
CREATE TABLE b23_management (key TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE b23_commands (
 command_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, state TEXT NOT NULL, result TEXT
);
CREATE TABLE b23_sources (
 source_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, scope TEXT NOT NULL,
 ref TEXT NOT NULL, body TEXT NOT NULL
);
"""


def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, object]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
]:
    """Derive the additional physical schema from its single SQL source."""
    import sqlite3

    from operant.memory_plugins.ledger import SCHEMA_SQL as LEDGER_SQL

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    try:
        c.executescript(LEDGER_SQL + SCHEMA_SQL)
        tables = [r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        required: dict[str, set[str]] = {}
        columns: dict[str, dict[str, dict[str, object]]] = {}
        keys: dict[str, tuple[str, ...]] = {}
        unique: dict[str, tuple[tuple[str, ...], ...]] = {}
        indexes: dict[str, tuple[str, ...]] = {}
        for table in tables:
            rows = c.execute(f'PRAGMA table_info("{table}")').fetchall()
            required[table] = {r["name"] for r in rows}
            columns[table] = {
                r["name"]: {"type": r["type"], "not_null": bool(r["notnull"])} for r in rows
            }
            keys[table] = tuple(r["name"] for r in sorted(rows, key=lambda r: r["pk"]) if r["pk"])
            for index in c.execute(f'PRAGMA index_list("{table}")').fetchall():
                fields = tuple(
                    r["name"] for r in c.execute(f'PRAGMA index_info("{index["name"]}")')
                )
                if index["origin"] != "pk" and index["unique"]:
                    unique[table] = (*unique.get(table, ()), fields)
                if not index["name"].startswith("sqlite_autoindex") and not index["unique"]:
                    indexes[index["name"]] = fields
        return required, columns, keys, unique, indexes
    finally:
        c.close()
