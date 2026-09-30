import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def transaction(self):
        """Run a unit of work in a single IMMEDIATE transaction.

        All writes performed through the yielded connection commit together;
        any exception rolls the whole transaction back so the store can never
        observe a half-applied state.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _get_entity(self, connection, entity_id):
        row = connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._entity_from_row(row) if row else None

    def _list_entities(self, connection, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = connection.execute(
            "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
        ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def _create_entity(self, connection, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection.execute(
            "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (entity_id, kind, status, payload, actor_id, now, now),
        )

    def _update_entity(self, connection, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        row = connection.execute(
            "SELECT version FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        current_version = int(row["version"])
        if expected_version is not None and current_version != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, current_version)
            )
        connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (status, payload, now, entity_id, current_version),
        )
        return current_version + 1

    def _append_audit(self, connection, entity_id, actor_id, actor_role, action,
                     from_status, to_status, detail):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    def _list_audit(self, connection, entity_id=None):
        if entity_id:
            rows = connection.execute(
                "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _get_idempotency(self, connection, actor_id, idem_key):
        row = connection.execute(
            "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
            (actor_id, idem_key),
        ).fetchone()
        return row["entity_id"] if row else None

    def _save_idempotency(self, connection, actor_id, idem_key, entity_id):
        connection.execute(
            "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (actor_id, idem_key, entity_id, utcnow()),
        )

    def get_entity(self, entity_id, conn=None):
        if conn is None:
            with self._connect() as connection:
                return self._get_entity(connection, entity_id)
        return self._get_entity(conn, entity_id)

    def list_entities(self, kind=None, status=None, conn=None):
        if conn is None:
            with self._connect() as connection:
                return self._list_entities(connection, kind, status)
        return self._list_entities(conn, kind, status)

    def find_entities(self, kind, field, value, conn=None):
        entities = self.list_entities(kind=kind, conn=conn)
        if field == "*":
            return entities
        return [
            entity
            for entity in entities
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def create_entity(self, entity_id, kind, status, data, actor_id, conn=None):
        if conn is None:
            with self.transaction() as connection:
                self._create_entity(connection, entity_id, kind, status, data, actor_id)
        else:
            self._create_entity(conn, entity_id, kind, status, data, actor_id)
        return self.get_entity(entity_id, conn=conn)

    def update_entity(self, entity_id, expected_version, status, data, conn=None):
        if conn is None:
            with self.transaction() as connection:
                self._update_entity(connection, entity_id, expected_version, status, data)
        else:
            self._update_entity(conn, entity_id, expected_version, status, data)
        return self.get_entity(entity_id, conn=conn)

    def append_audit(self, entity_id, actor_id, actor_role, action,
                     from_status, to_status, detail, conn=None):
        if conn is None:
            with self.transaction() as connection:
                self._append_audit(
                    connection, entity_id, actor_id, actor_role, action,
                    from_status, to_status, detail,
                )
        else:
            self._append_audit(
                conn, entity_id, actor_id, actor_role, action,
                from_status, to_status, detail,
            )

    def list_audit(self, entity_id=None, conn=None):
        if conn is None:
            with self._connect() as connection:
                return self._list_audit(connection, entity_id)
        return self._list_audit(conn, entity_id)

    def get_idempotency(self, actor_id, idem_key, conn=None):
        if conn is None:
            with self._connect() as connection:
                return self._get_idempotency(connection, actor_id, idem_key)
        return self._get_idempotency(conn, actor_id, idem_key)

    def save_idempotency(self, actor_id, idem_key, entity_id, conn=None):
        if conn is None:
            with self.transaction() as connection:
                self._save_idempotency(connection, actor_id, idem_key, entity_id)
        else:
            self._save_idempotency(conn, actor_id, idem_key, entity_id)

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
