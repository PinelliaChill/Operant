"""Core-owned MP-3 publication history and bounded recall index (SQLite v16)."""

from functools import lru_cache

SCHEMA_SQL = """
CREATE TABLE b24_publications (
 dataset_id TEXT NOT NULL, record_id TEXT NOT NULL, cursor INTEGER NOT NULL,
 version INTEGER, state TEXT NOT NULL, body TEXT NOT NULL,
 PRIMARY KEY(dataset_id,record_id,cursor)
);
CREATE INDEX idx_b24_publications_cutoff ON b24_publications(dataset_id,cursor);
INSERT INTO b24_publications
 SELECT dataset_id,record_id,CAST(publication_cursor AS INTEGER),published_version,state,body
 FROM memory_ledger_heads;
CREATE TRIGGER b24_head_insert AFTER INSERT ON memory_ledger_heads BEGIN
 INSERT INTO b24_publications VALUES (new.dataset_id,new.record_id,CAST(new.publication_cursor AS
 INTEGER),new.published_version,new.state,new.body);
END;
CREATE TRIGGER b24_head_update AFTER UPDATE ON memory_ledger_heads BEGIN
 INSERT OR REPLACE INTO b24_publications VALUES
 (new.dataset_id,new.record_id,CAST(new.publication_cursor AS
 INTEGER),new.published_version,new.state,new.body);
END;
CREATE VIRTUAL TABLE b24_fts USING fts5(
 dataset_id UNINDEXED,record_id UNINDEXED,version UNINDEXED,content,tokenize='trigram');
INSERT INTO b24_fts SELECT dataset_id,record_id,version,json_extract(body,'$.content') FROM
memory_ledger_versions;
CREATE TRIGGER b24_version_insert AFTER INSERT ON memory_ledger_versions BEGIN
 INSERT INTO b24_fts
 VALUES(new.dataset_id,new.record_id,new.version,json_extract(new.body,'$.content'));
END;
CREATE TRIGGER b24_version_delete AFTER DELETE ON memory_ledger_versions BEGIN
 DELETE FROM b24_fts WHERE dataset_id=old.dataset_id AND record_id=old.record_id AND
 version=old.version;
END;
CREATE TABLE b24_manifests (
 run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, dataset_id TEXT NOT NULL,
 body TEXT NOT NULL
);
CREATE TABLE b24_context_memory (revision_id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE b24_commands (
 command_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, result TEXT NOT NULL
);
"""


@lru_cache(maxsize=1)
def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, object]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
]:
    import sqlite3

    from operant.memory_plugins.ledger import SCHEMA_SQL as ledger

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    try:
        c.executescript(ledger + SCHEMA_SQL)
        tables = [
            r["name"]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'b24_%'"
            )
        ]
        required = {}
        columns = {}
        keys = {}
        unique: dict[str, tuple[tuple[str, ...], ...]] = {}
        indexes = {}
        for table in tables:
            rows = c.execute(f'PRAGMA table_info("{table}")').fetchall()
            required[table] = {r["name"] for r in rows}
            columns[table] = {
                r["name"]: {"type": r["type"], "not_null": bool(r["notnull"])} for r in rows
            }
            keys[table] = tuple(r["name"] for r in sorted(rows, key=lambda r: r["pk"]) if r["pk"])
            for idx in c.execute(f'PRAGMA index_list("{table}")').fetchall():
                fields = tuple(r["name"] for r in c.execute(f'PRAGMA index_info("{idx["name"]}")'))
                if idx["origin"] != "pk" and idx["unique"]:
                    unique[table] = (*unique.get(table, ()), fields)
                if not idx["name"].startswith("sqlite_autoindex") and not idx["unique"]:
                    indexes[idx["name"]] = fields
        return required, columns, keys, unique, indexes
    finally:
        c.close()
