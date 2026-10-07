import json
import sqlite3
import time
from datetime import datetime, timezone
from uuid import uuid4

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IdentityConflict(Exception):
    """An identifier (id_card/phone) resolves to a different person."""


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
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
                -- Person master index: one physical person regardless of how
                -- many field teams or offline batches registered them.
                CREATE TABLE IF NOT EXISTS persons (
                    person_id TEXT PRIMARY KEY,
                    id_card TEXT,
                    phone TEXT,
                    name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_id_card
                    ON persons(id_card) WHERE id_card IS NOT NULL AND id_card <> '';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_phone
                    ON persons(phone) WHERE phone IS NOT NULL AND phone <> '';
                -- Exposure ledger: one row per (case, person, exposure window).
                -- The unique constraint makes re-applying a batch item a no-op.
                CREATE TABLE IF NOT EXISTS contact_exposures (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    contact_entity_id TEXT NOT NULL,
                    person_id TEXT NOT NULL,
                    exposure_start TEXT,
                    exposure_end TEXT,
                    team_id TEXT,
                    township TEXT,
                    batch_id TEXT,
                    item_id TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, item_id)
                );
                CREATE INDEX IF NOT EXISTS idx_exposures_case
                    ON contact_exposures(case_id);
                CREATE INDEX IF NOT EXISTS idx_exposures_contact
                    ON contact_exposures(contact_entity_id, exposure_start);
                -- Offline inbound batches and their per-item progress ledger.
                CREATE TABLE IF NOT EXISTS inbound_batches (
                    batch_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    chain_id TEXT,
                    team_id TEXT,
                    township TEXT,
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbound_batch_items (
                    batch_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    person_id TEXT,
                    contact_entity_id TEXT,
                    reason TEXT,
                    applied_at TEXT,
                    PRIMARY KEY(batch_id, item_id)
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
        return [
            entity
            for entity in self.list_entities(kind=kind)
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

    # ------------------------------------------------------------------
    # Offline batch merge
    # ------------------------------------------------------------------

    def get_batch(self, batch_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM inbound_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_batch_items(self, batch_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM inbound_batch_items WHERE batch_id = ? ORDER BY item_id",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def ensure_batch(self, batch_id, case_id, chain_id, team_id, township, actor_id):
        """Create the batch header on first sight; later retries are no-ops.

        Reusing the same batch id is the retry mechanism, so an existing
        header must never be overwritten (that is what makes a mid-way crash
        safely resumable)."""
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO inbound_batches"
                "(batch_id, case_id, chain_id, team_id, township, status, "
                "created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (batch_id, case_id, chain_id, team_id, township, actor_id, now, now),
            )
        return self.get_batch(batch_id)

    def mark_batch_status(self, batch_id, status):
        with self._connect() as connection:
            connection.execute(
                "UPDATE inbound_batches SET status = ?, updated_at = ? WHERE batch_id = ?",
                (status, utcnow(), batch_id),
            )

    def _resolve_person(self, connection, item):
        """Find or create the person master record.

        Identity precedence: id_card, then phone, then person_id. An
        identifier already bound to a *different* person is an
        IdentityConflict (permanent rejection). An identifier we have not
        seen before is attached to the resolved person (first-seen wins, so
        a new phone number reported later never clobbers an old one).
        """
        person_id = item.get("person_id")
        id_card = item.get("id_card")
        phone = item.get("phone")
        name = item.get("name")

        def by(field, value):
            return connection.execute(
                "SELECT * FROM persons WHERE " + field + " = ?", (value,)
            ).fetchone()

        row = None
        if id_card:
            row = by("id_card", id_card)
        if row is None and phone:
            row = by("phone", phone)
        if row is None and person_id:
            row = by("person_id", person_id)

        if row is not None:
            resolved_id = row["person_id"]
            existing = dict(row)
            for field, value in (("id_card", id_card), ("phone", phone)):
                if not value:
                    continue
                holder = by(field, value)
                if holder is not None and holder["person_id"] != resolved_id:
                    raise IdentityConflict(
                        "%s %s already registered to person %s"
                        % (field, value, holder["person_id"])
                    )
            updates = []
            params = []
            for field, value in (("id_card", id_card), ("phone", phone), ("name", name)):
                if value and not existing.get(field) and by(field, value) is None:
                    updates.append(field + " = ?")
                    params.append(value)
            if updates:
                updates.append("updated_at = ?")
                params.append(utcnow())
                params.append(resolved_id)
                connection.execute(
                    "UPDATE persons SET " + ", ".join(updates) + " WHERE person_id = ?",
                    params,
                )
            return resolved_id, False

        resolved_id = person_id or ("P-" + uuid4().hex[:12])
        now = utcnow()
        connection.execute(
            "INSERT INTO persons(person_id, id_card, phone, name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (resolved_id, id_card or None, phone or None, name or None, now, now),
        )
        return resolved_id, True

    def merge_contact_item(self, batch_id, item):
        """Atomically register one offline contact item.

        Returns (person_id, contact_entity_id, created_contact, widened).
        ``widened`` means an existing contact's exposure window changed and
        its follow-up window must be recomputed.
        Raises on a transient failure; the whole item rolls back and the
        batch can be retried without any partial effect.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            person_id, _ = self._resolve_person(connection, item)

            contact_data = {
                "case_id": item["case_id"],
                "person_id": person_id,
                "chain_id": item.get("chain_id"),
                "team_id": item.get("team_id"),
                "township": item.get("township"),
                "exposure_start": item.get("exposure_start"),
                "exposure_end": item.get("exposure_end"),
                "source_batch_id": batch_id,
            }
            now = utcnow()

            # Same person already registered as a contact of this case?
            row = connection.execute(
                "SELECT ce.contact_entity_id, ce.exposure_start, ce.exposure_end, e.data "
                "FROM contact_exposures ce "
                "JOIN entities e ON e.id = ce.contact_entity_id "
                "WHERE ce.case_id = ? AND ce.person_id = ? "
                "ORDER BY ce.created_at LIMIT 1",
                (item["case_id"], person_id),
            ).fetchone()

            created_contact = False
            widened = False
            if row is None:
                contact_entity_id = "C-" + uuid4().hex[:12]
                payload = json.dumps(contact_data, ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "INSERT INTO entities(id, kind, status, version, data, "
                    "created_by, created_at, updated_at) "
                    "VALUES (?, 'contact', 'identified', 1, ?, ?, ?, ?)",
                    (
                        contact_entity_id,
                        payload,
                        item.get("team_id") or "offline-batch",
                        now,
                        now,
                    ),
                )
                created_contact = True
            else:
                contact_entity_id = row["contact_entity_id"]
                data = json.loads(row["data"])
                # Widen the exposure window if this batch adds new information.
                old_start = data.get("exposure_start")
                old_end = data.get("exposure_end")
                new_start = item.get("exposure_start")
                new_end = item.get("exposure_end")
                if new_start and (not old_start or new_start < old_start):
                    old_start = new_start
                    widened = True
                if new_end and (not old_end or new_end > old_end):
                    old_end = new_end
                    widened = True
                data["exposure_start"] = old_start
                data["exposure_end"] = old_end
                if widened:
                    connection.execute(
                        "UPDATE entities SET data = ?, version = version + 1, updated_at = ? "
                        "WHERE id = ?",
                        (json.dumps(data, ensure_ascii=False, sort_keys=True), now, contact_entity_id),
                    )

            # One ledger row per (batch, item): UNIQUE makes replay a no-op.
            connection.execute(
                "INSERT OR IGNORE INTO contact_exposures"
                "(id, case_id, contact_entity_id, person_id, exposure_start, "
                "exposure_end, team_id, township, batch_id, item_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "E-" + uuid4().hex[:12],
                    item["case_id"],
                    contact_entity_id,
                    person_id,
                    item.get("exposure_start"),
                    item.get("exposure_end"),
                    item.get("team_id"),
                    item.get("township"),
                    batch_id,
                    item["item_id"],
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO inbound_batch_items"
                "(batch_id, item_id, status, person_id, contact_entity_id, applied_at) "
                "VALUES (?, ?, 'applied', ?, ?, ?) "
                "ON CONFLICT(batch_id, item_id) DO UPDATE SET "
                "status='applied', person_id=excluded.person_id, "
                "contact_entity_id=excluded.contact_entity_id, "
                "reason=NULL, applied_at=excluded.applied_at",
                (batch_id, item["item_id"], person_id, contact_entity_id, now),
            )
            connection.commit()
            return person_id, contact_entity_id, created_contact, widened
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reject_batch_item(self, batch_id, item_id, reason, person_id=None):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO inbound_batch_items"
                "(batch_id, item_id, status, person_id, reason, applied_at) "
                "VALUES (?, ?, 'rejected', ?, ?, NULL) "
                "ON CONFLICT(batch_id, item_id) DO UPDATE SET "
                "status='rejected', reason=excluded.reason, person_id=excluded.person_id",
                (batch_id, item_id, reason, person_id),
            )

    def mark_batch_item_failed(self, batch_id, item_id, reason):
        """Record a transient failure but keep the item pending for retry."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO inbound_batch_items"
                "(batch_id, item_id, status, reason, applied_at) "
                "VALUES (?, ?, 'pending', ?, NULL) "
                "ON CONFLICT(batch_id, item_id) DO UPDATE SET "
                "status='pending', reason=excluded.reason",
                (batch_id, item_id, reason),
            )

    def contacts_for_case(self, case_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT e.* FROM entities e "
                "WHERE e.kind = 'contact' AND json_extract(e.data, '$.case_id') = ? "
                "ORDER BY e.created_at, e.id",
                (case_id,),
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def write_followup_fields(self, contact_entity_id, computed):
        """Store recomputed follow-up fields and bump the version atomically."""
        now = utcnow()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM entities WHERE id = ?", (contact_entity_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                raise NotFoundError("entity not found: " + contact_entity_id)
            data = json.loads(row["data"])
            data.update(computed)
            connection.execute(
                "UPDATE entities SET data = ?, version = version + 1, updated_at = ? "
                "WHERE id = ?",
                (json.dumps(data, ensure_ascii=False, sort_keys=True), now, contact_entity_id),
            )
            connection.commit()

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
