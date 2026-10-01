"""隔离交换闸口服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exchange_domains (
    domain_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('architect', 'gateway_sender', 'gateway_receiver', 'auditor')),
    domain_id TEXT REFERENCES exchange_domains(domain_id),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_contracts (
    contract_id TEXT PRIMARY KEY,
    message_kind TEXT NOT NULL
        CHECK (message_kind IN ('target_trajectory', 'environment_summary', 'execution_receipt')),
    source_domain TEXT NOT NULL REFERENCES exchange_domains(domain_id),
    target_domain TEXT NOT NULL REFERENCES exchange_domains(domain_id),
    required_fields_json TEXT NOT NULL,
    allowed_versions_json TEXT NOT NULL,
    ticket_ttl_seconds INTEGER NOT NULL CHECK (ticket_ttl_seconds > 0),
    state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft', 'frozen', 'revoked')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    frozen_by TEXT,
    frozen_at TEXT,
    revoked_by TEXT,
    revoked_at TEXT,
    revoke_reason TEXT,
    CHECK (source_domain <> target_domain)
);

CREATE TABLE IF NOT EXISTS transfer_tickets (
    transfer_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES message_contracts(contract_id),
    contract_revision INTEGER NOT NULL,
    message_kind TEXT NOT NULL,
    source_domain TEXT NOT NULL,
    target_domain TEXT NOT NULL,
    payload_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    declared_fields_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'issued'
        CHECK (state IN ('issued', 'consumed', 'expired', 'blocked')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tickets_contract_state
ON transfer_tickets(contract_id, state);

CREATE INDEX IF NOT EXISTS idx_tickets_target_state
ON transfer_tickets(target_domain, state, expires_at);

CREATE TABLE IF NOT EXISTS consumption_facts (
    consumption_id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id TEXT NOT NULL UNIQUE REFERENCES transfer_tickets(transfer_id),
    contract_id TEXT NOT NULL,
    contract_revision INTEGER NOT NULL,
    payload_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    source_domain TEXT NOT NULL,
    target_domain TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS gateway_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gateway_audit_entity
ON gateway_audit_events(entity_type, entity_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "exchange_domains", "gateway_users", "message_contracts",
    "transfer_tickets", "consumption_facts", "gateway_idempotency", "gateway_audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。

    HTTP 服务以多线程处理请求，连接允许跨线程使用；
    并发请求由 API 层的锁串行化，业务事务保持原子性。
    """

    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    initialize(connection)
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化闸口表结构，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
