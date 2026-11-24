"""SQL-backed object store with links, approval requests, an audit log and a webhook outbox.

Deliberately boring: SQLite by default (one file, no server, standard library
only). The same code runs on PostgreSQL (``postgresql://...``, needs ``psycopg``)
and DuckDB (``duckdb://file`` or ``*.duckdb``, needs ``duckdb``) because it only
uses portable SQL. All of nano-onto's own tables are prefixed ``onto_`` so they
can live next to existing tables.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS onto_objects (
    type TEXT NOT NULL,
    pk TEXT NOT NULL,
    props TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (type, pk)
);
CREATE TABLE IF NOT EXISTS onto_links (
    link TEXT NOT NULL,
    from_pk TEXT NOT NULL,
    to_pk TEXT NOT NULL,
    PRIMARY KEY (link, from_pk, to_pk)
);
CREATE INDEX IF NOT EXISTS onto_links_to ON onto_links (link, to_pk);
CREATE TABLE IF NOT EXISTS onto_approvals (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    params TEXT NOT NULL,
    actor TEXT NOT NULL,
    required_roles TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    reasons TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS onto_audit (
    seq BIGINT PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    event TEXT NOT NULL,
    action TEXT,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS onto_counters (
    name TEXT PRIMARY KEY,
    value BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS onto_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS onto_history (
    seq BIGINT PRIMARY KEY,
    ts TEXT NOT NULL,
    entry TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS onto_outbox (
    id TEXT PRIMARY KEY,
    webhook TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_BEGIN = {"sqlite": "BEGIN IMMEDIATE", "postgres": "BEGIN", "duckdb": "BEGIN TRANSACTION"}
_PG_LOCK = 7226351  # advisory lock key: one action at a time per database


class StoreError(Exception):
    """The database refused a statement (constraint violation, lock timeout, missing table...)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _connect(path: str) -> tuple[str, Any, type[Exception]]:
    if path.startswith(("postgresql://", "postgres://")):
        try:
            import psycopg
        except ImportError:
            raise RuntimeError("PostgreSQL stores need psycopg: pip install 'nano-onto[postgres]'") from None
        return "postgres", psycopg.connect(path, autocommit=True), psycopg.Error
    if path.startswith("duckdb://") or path.endswith((".duckdb", ".ddb")):
        try:
            import duckdb
        except ImportError:
            raise RuntimeError("DuckDB stores need duckdb: pip install 'nano-onto[duckdb]'") from None
        return "duckdb", duckdb.connect(path.removeprefix("duckdb://") or ":memory:"), duckdb.Error
    file = path.removeprefix("sqlite://") or ":memory:"
    conn = sqlite3.connect(file, check_same_thread=False, isolation_level=None, timeout=30)
    return "sqlite", conn, sqlite3.Error


class Store:
    def __init__(self, path: str = ":memory:"):
        self.path = str(path)
        self.dialect, self.conn, self._driver_error = _connect(self.path)
        self.lock = threading.RLock()
        self._depth = 0
        with self.transaction():
            for statement in _SCHEMA.split(";"):
                self._run(statement)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------- SQL
    def _run(self, sql: str, params: tuple | list = ()) -> Any:
        if self.dialect == "postgres":
            sql = sql.replace("?", "%s")
        with self.lock:
            try:
                return self.conn.execute(sql, tuple(params))
            except self._driver_error as exc:
                raise StoreError(f"{type(exc).__name__}: {exc}".strip()) from None

    def _rows(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self.lock:
            cur = self._run(sql, params)
            names = [column[0] for column in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Re-entrant, serialized transaction: only the outermost block commits or rolls back.

        Writers are serialized (``BEGIN IMMEDIATE`` on SQLite, an advisory lock on
        PostgreSQL), so a rule check and the effects that follow it see one state.
        """
        with self.lock:
            outer = self._depth == 0
            if outer:
                self._run(_BEGIN[self.dialect])
                if self.dialect == "postgres":
                    self._run("SELECT pg_advisory_xact_lock(?)", (_PG_LOCK,))
            self._depth += 1
            try:
                yield
            except BaseException:
                if outer:
                    self._abort()
                raise
            else:
                if outer:
                    try:
                        self._run("COMMIT")
                    except StoreError:  # e.g. SQLITE_BUSY: do not leave the transaction (and its lock) open
                        self._abort()
                        raise
            finally:
                self._depth -= 1

    def _abort(self) -> None:
        try:
            self._run("ROLLBACK")
        except StoreError:
            pass

    @property
    def in_transaction(self) -> bool:
        return self._depth > 0

    def _bump(self, name: str) -> int:
        """Next value of a named counter. Call inside a transaction."""
        rows = self._rows("SELECT value FROM onto_counters WHERE name = ?", (name,))
        value = (rows[0]["value"] if rows else 0) + 1
        self._set_counter(name, value)
        return value

    def _set_counter(self, name: str, value: int) -> None:
        self._run(
            "INSERT INTO onto_counters (name, value) VALUES (?, ?) "
            "ON CONFLICT (name) DO UPDATE SET value = excluded.value",
            (name, value),
        )

    # ---------------------------------------------------------------- objects
    def get(self, type_name: str, pk: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT props FROM onto_objects WHERE type = ? AND pk = ?", (type_name, str(pk)))
        return json.loads(rows[0]["props"]) if rows else None

    def exists(self, type_name: str, pk: str) -> bool:
        return self.get(type_name, pk) is not None

    def put(self, type_name: str, pk: str, props: dict[str, Any]) -> None:
        now = _now()
        self._run(
            "INSERT INTO onto_objects (type, pk, props, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (type, pk) DO UPDATE SET props = excluded.props, updated_at = excluded.updated_at",
            (type_name, str(pk), _dump(props), now, now),
        )

    def update(self, type_name: str, pk: str, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.get(type_name, pk)
        if current is None:
            raise KeyError(f"{type_name} {pk!r} not found")
        current.update(changes)
        self.put(type_name, pk, current)
        return current

    def delete(self, type_name: str, pk: str) -> None:
        self._run("DELETE FROM onto_objects WHERE type = ? AND pk = ?", (type_name, str(pk)))

    def query(
        self, type_name: str, where: dict[str, Any] | None = None, limit: int | None = 100
    ) -> list[dict[str, Any]]:
        """Objects of a type with exact-match filters, ordered by primary key. ``limit=None`` returns all."""
        out = []
        for row in self._rows("SELECT props FROM onto_objects WHERE type = ? ORDER BY pk", (type_name,)):
            props = json.loads(row["props"])
            if where and any(props.get(k) != v for k, v in where.items()):
                continue
            out.append(props)
            if limit is not None and len(out) >= limit:
                break
        return out

    def count(self, type_name: str) -> int:
        return self._rows("SELECT COUNT(*) AS n FROM onto_objects WHERE type = ?", (type_name,))[0]["n"]

    def next_id(self, type_name: str, prefix: str) -> str:
        with self.transaction():
            value = self._bump(type_name)
            while self.exists(type_name, f"{prefix}{value:04d}"):
                value += 1
            self._set_counter(type_name, value)
        return f"{prefix}{value:04d}"

    # ------------------------------------------------------------------ links
    def add_link(self, link: str, from_pk: str, to_pk: str) -> None:
        self._run(
            "INSERT INTO onto_links (link, from_pk, to_pk) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            (link, str(from_pk), str(to_pk)),
        )

    def remove_link(self, link: str, from_pk: str | None = None, to_pk: str | None = None) -> None:
        """Remove links of one type, narrowed by either end (both ``None`` removes them all)."""
        sql, params = "DELETE FROM onto_links WHERE link = ?", [link]
        if from_pk is not None:
            sql, params = sql + " AND from_pk = ?", params + [str(from_pk)]
        if to_pk is not None:
            sql, params = sql + " AND to_pk = ?", params + [str(to_pk)]
        self._run(sql, params)

    def links_from(self, link: str, from_pk: str) -> list[str]:
        rows = self._rows(
            "SELECT to_pk FROM onto_links WHERE link = ? AND from_pk = ? ORDER BY to_pk", (link, str(from_pk))
        )
        return [r["to_pk"] for r in rows]

    def links_to(self, link: str, to_pk: str) -> list[str]:
        rows = self._rows(
            "SELECT from_pk FROM onto_links WHERE link = ? AND to_pk = ? ORDER BY from_pk", (link, str(to_pk))
        )
        return [r["from_pk"] for r in rows]

    # -------------------------------------------------------------- approvals
    def create_approval(
        self,
        action: str,
        params: dict[str, Any],
        actor: dict[str, str],
        required_roles: list[str],
        reasons: list[str],
    ) -> str:
        rid = "REQ-" + uuid.uuid4().hex[:8].upper()
        now = _now()
        self._run(
            "INSERT INTO onto_approvals VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, action, _dump(params), _dump(actor), _dump(required_roles), _dump({}),
             _dump(reasons), "pending", now, now),
        )
        return rid

    def get_approval(self, rid: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM onto_approvals WHERE id = ?", (rid,))
        return self._approval(rows[0]) if rows else None

    def update_approval(self, rid: str, **fields: Any) -> None:
        sets, values = [], []
        for key, value in fields.items():
            if key not in ("status", "required_roles", "approved_by", "reasons"):
                raise KeyError(key)
            sets.append(f"{key} = ?")
            values.append(value if key == "status" else _dump(value))
        sets.append("updated_at = ?")
        values.extend([_now(), rid])
        self._run(f"UPDATE onto_approvals SET {', '.join(sets)} WHERE id = ?", values)

    def list_approvals(self, status: str | None = "pending") -> list[dict[str, Any]]:
        if status:
            rows = self._rows("SELECT * FROM onto_approvals WHERE status = ? ORDER BY created_at, id", (status,))
        else:
            rows = self._rows("SELECT * FROM onto_approvals ORDER BY created_at, id")
        return [self._approval(r) for r in rows]

    @staticmethod
    def _approval(row: dict[str, Any]) -> dict[str, Any]:
        for key in ("params", "actor", "required_roles", "approved_by", "reasons"):
            row[key] = json.loads(row[key])
        return row

    # ------------------------------------------------------------------ audit
    def log(self, actor: dict[str, str], event: str, action: str | None, outcome: str, detail: Any) -> None:
        with self.transaction():
            self._run(
                "INSERT INTO onto_audit (seq, ts, actor, role, event, action, outcome, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self._bump("onto:audit"), _now(), actor.get("id", "?"), actor.get("role", "?"),
                 event, action, outcome, _dump(detail)),
            )

    def audit_log(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM onto_audit ORDER BY seq DESC LIMIT ?", (limit,))
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return rows[::-1]

    # ------------------------------------------------- ontology version stamp
    def get_meta(self, key: str) -> Any:
        rows = self._rows("SELECT value FROM onto_meta WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else None

    def set_meta(self, key: str, value: Any) -> None:
        self._run(
            "INSERT INTO onto_meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, _dump(value)),
        )

    def add_history(self, entry: dict[str, Any]) -> None:
        with self.transaction():
            self._run("INSERT INTO onto_history (seq, ts, entry) VALUES (?, ?, ?)",
                      (self._bump("onto:history"), _now(), _dump(entry)))

    def history(self) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM onto_history ORDER BY seq")
        return [{"seq": r["seq"], "ts": r["ts"], **json.loads(r["entry"])} for r in rows]

    # ---------------------------------------------------------------- outbox
    def enqueue(self, webhook: str, body: dict[str, Any]) -> str:
        """Queue a webhook delivery. Called inside the action's transaction, so it commits with the effects."""
        oid = "EVT-" + uuid.uuid4().hex[:12].upper()
        now = _now()
        self._run(
            "INSERT INTO onto_outbox VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (oid, webhook, _dump({**body, "id": oid}), "pending", 0, now, None, now, now),
        )
        return oid

    def outbox(self, status: str | None = None, due_before: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM onto_outbox WHERE 1 = 1", []
        if status:
            sql, params = sql + " AND status = ?", params + [status]
        if due_before:
            sql, params = sql + " AND next_attempt_at <= ?", params + [due_before]
        rows = self._rows(sql + " ORDER BY created_at, id LIMIT ?", params + [limit])
        for row in rows:
            row["body"] = json.loads(row["body"])
        return rows

    def update_outbox(self, oid: str, status: str, attempts: int, next_attempt_at: str, last_error: str | None) -> None:
        self._run(
            "UPDATE onto_outbox SET status = ?, attempts = ?, next_attempt_at = ?, last_error = ?, updated_at = ? "
            "WHERE id = ?",
            (status, attempts, next_attempt_at, last_error, _now(), oid),
        )


def open_store(path: str = ":memory:", onto: Any = None) -> Store:
    """Open a store; if the ontology maps types or links onto existing tables, read through to them."""
    if onto is not None and onto.mapped:
        from .tables import MappedStore

        return MappedStore(path, onto)
    return Store(path)
