"""隔离交换闸口的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS gateway_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('architect','sender','receiver','auditor')),
    domain TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_contracts (
    contract_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    message_kind TEXT NOT NULL
        CHECK(message_kind IN ('target_trajectory','environment_summary','execution_receipt')),
    source_domain TEXT NOT NULL,
    target_domain TEXT NOT NULL,
    field_set_json TEXT NOT NULL,
    field_set_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','frozen','revoked')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    created_by TEXT NOT NULL REFERENCES gateway_users(user_id),
    created_at TEXT NOT NULL,
    frozen_by TEXT REFERENCES gateway_users(user_id),
    frozen_at TEXT,
    revoked_by TEXT REFERENCES gateway_users(user_id),
    revoked_at TEXT,
    revoke_reason TEXT,
    PRIMARY KEY (contract_id, version),
    CHECK(source_domain <> target_domain)
);

CREATE TABLE IF NOT EXISTS transfer_tickets (
    ticket_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    contract_version INTEGER NOT NULL,
    message_kind TEXT NOT NULL,
    source_domain TEXT NOT NULL,
    target_domain TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'issued'
        CHECK(state IN ('issued','consumed','expired','blocked')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    issued_by TEXT NOT NULL REFERENCES gateway_users(user_id),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    blocked_reason TEXT,
    FOREIGN KEY (contract_id, contract_version)
        REFERENCES message_contracts(contract_id, version)
);

CREATE INDEX IF NOT EXISTS idx_tickets_contract
ON transfer_tickets(contract_id, contract_version, state);

CREATE INDEX IF NOT EXISTS idx_tickets_target
ON transfer_tickets(target_domain, state, expires_at);

CREATE TABLE IF NOT EXISTS consumption_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL UNIQUE REFERENCES transfer_tickets(ticket_id),
    contract_id TEXT NOT NULL,
    contract_version INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    ticket_revision INTEGER NOT NULL,
    confirmed_by TEXT NOT NULL REFERENCES gateway_users(user_id),
    confirmed_at TEXT NOT NULL
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


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
