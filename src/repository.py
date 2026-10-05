import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError, ValidationError


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
                CREATE TABLE IF NOT EXISTS resource_capacity (
                    resource_type TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    capacity INTEGER NOT NULL,
                    PRIMARY KEY(resource_type, resource_key)
                );
                CREATE TABLE IF NOT EXISTS resource_claim (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chain_id TEXT NOT NULL,
                    step_no INTEGER NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_key TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    released_at TEXT,
                    UNIQUE(chain_id, step_no, resource_type, resource_key)
                );
                CREATE INDEX IF NOT EXISTS idx_resource_claim_lookup
                    ON resource_claim(resource_type, resource_key, status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_chain_active
                    ON entities(kind, json_extract(data, '$.incident_id'))
                    WHERE kind = 'recovery_chain' AND status NOT IN ('succeeded', 'compensated');
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

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        entities = self.list_entities(kind=kind)
        if field == "*":
            return entities
        return [
            entity
            for entity in entities
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
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
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
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

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
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

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def register_resource(self, resource_type, resource_key, capacity):
        if int(capacity) < 0:
            raise ValidationError("resource capacity must be non-negative")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO resource_capacity(resource_type, resource_key, capacity) "
                "VALUES (?, ?, ?)",
                (resource_type, resource_key, int(capacity)),
            )
        return self.get_resource(resource_type, resource_key)

    def get_resource(self, resource_type, resource_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT resource_type, resource_key, capacity FROM resource_capacity "
                "WHERE resource_type = ? AND resource_key = ?",
                (resource_type, resource_key),
            ).fetchone()
        if not row:
            return None
        return {
            "resource_type": row["resource_type"],
            "resource_key": row["resource_key"],
            "capacity": int(row["capacity"]),
        }

    def list_resources(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT c.resource_type, c.resource_key, c.capacity, "
                "COALESCE((SELECT SUM(units) FROM resource_claim "
                "WHERE resource_type = c.resource_type AND resource_key = c.resource_key AND status = 'held'), 0) AS held "
                "FROM resource_capacity c ORDER BY c.resource_type, c.resource_key"
            ).fetchall()
        return [
            {
                "resource_type": row["resource_type"],
                "resource_key": row["resource_key"],
                "capacity": int(row["capacity"]),
                "held": int(row["held"]),
                "available": int(row["capacity"]) - int(row["held"]),
            }
            for row in rows
        ]

    def claim_resources(self, chain_id, step_no, resources):
        """Atomically claim resources for a step. Idempotent per (chain, step, resource)."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for resource in resources:
                    resource_type = resource["type"]
                    resource_key = resource["key"]
                    units = int(resource["units"])
                    capacity_row = connection.execute(
                        "SELECT capacity FROM resource_capacity WHERE resource_type = ? AND resource_key = ?",
                        (resource_type, resource_key),
                    ).fetchone()
                    if not capacity_row:
                        raise ValidationError("unknown resource: %s/%s" % (resource_type, resource_key))
                    capacity = int(capacity_row["capacity"])
                    held = connection.execute(
                        "SELECT COALESCE(SUM(units), 0) FROM resource_claim "
                        "WHERE resource_type = ? AND resource_key = ? AND status = 'held' "
                        "AND NOT (chain_id = ? AND step_no = ?)",
                        (resource_type, resource_key, chain_id, step_no),
                    ).fetchone()[0]
                    already = connection.execute(
                        "SELECT COALESCE(SUM(units), 0) FROM resource_claim "
                        "WHERE chain_id = ? AND step_no = ? AND resource_type = ? AND resource_key = ?",
                        (chain_id, step_no, resource_type, resource_key),
                    ).fetchone()[0]
                    need = max(0, units - int(already))
                    if int(held) + need > capacity:
                        raise ConflictError(
                            "resource %s/%s exhausted: %d held, need %d, capacity %d"
                            % (resource_type, resource_key, int(held), need, capacity)
                        )
                    connection.execute(
                        "INSERT OR IGNORE INTO resource_claim"
                        "(chain_id, step_no, resource_type, resource_key, units, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, 'held', ?)",
                        (chain_id, step_no, resource_type, resource_key, units, utcnow()),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def release_resources(self, chain_id, step_no):
        """Release a step's claims. Idempotent: already-released claims are not touched again."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE resource_claim SET status = 'released', released_at = ? "
                "WHERE chain_id = ? AND step_no = ? AND status = 'held'",
                (utcnow(), chain_id, step_no),
            )

    def list_claims(self, chain_id=None):
        clauses = []
        params = []
        if chain_id:
            clauses.append("chain_id = ?")
            params.append(chain_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM resource_claim" + where + " ORDER BY id", params
            ).fetchall()
        return [
            {
                "id": row["id"],
                "chain_id": row["chain_id"],
                "step_no": int(row["step_no"]),
                "resource_type": row["resource_type"],
                "resource_key": row["resource_key"],
                "units": int(row["units"]),
                "status": row["status"],
                "created_at": row["created_at"],
                "released_at": row["released_at"],
            }
            for row in rows
        ]

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
