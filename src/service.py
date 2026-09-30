import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _tx_lookup(self, conn):
        def lookup(kind, field, value):
            return self.repository.find_entities(self.rules.normalize_kind(kind), field, value, conn)
        return lookup

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        with self.repository.transaction() as conn:
            self.rules.validate_create(actor, kind, payload, self._tx_lookup(conn))
            entity_id = str(payload.pop("id", "") or uuid4())
            status = self.rules.initial_status(kind, payload)
            if idempotency_key:
                existing_id = self.repository.get_idempotency(actor.user_id, idempotency_key, conn)
                if existing_id:
                    existing = self.repository.get_entity(existing_id, conn)
                    if existing:
                        return existing
            if self.repository.get_entity(entity_id, conn):
                raise ConflictError("entity already exists: " + entity_id)
            self.repository.create_entity(entity_id, kind, status, payload, actor.user_id, conn)
            self.repository.append_audit(
                entity_id, actor.user_id, actor.role, "create", None, status, {"kind": kind}, conn
            )
            if idempotency_key:
                self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id, conn)
        return self.repository.get_entity(entity_id)

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        with self.repository.transaction() as conn:
            entity = self.repository.get_entity(entity_id, conn)
            if not entity:
                raise NotFoundError("entity not found: " + entity_id)
            expected = int(expected_version) if expected_version is not None else entity["version"]
            if expected is not None and entity["version"] != expected:
                raise ConflictError(
                    "version conflict: expected %s, found %s" % (expected, entity["version"])
                )
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, dict(data or {}), self._tx_lookup(conn)
            )
            merged = dict(entity["data"])
            merged.update(patch)

            if self._is_component_replacement_completion(entity, action, merged):
                return self._complete_component_replacement(actor, entity, next_status, merged, conn)

            updated = self.repository.update_entity(
                entity_id, entity["version"], next_status, merged, conn
            )
            self.repository.append_audit(
                entity_id,
                actor.user_id,
                actor.role,
                action,
                entity["status"],
                updated["status"],
                {"patch": patch},
                conn,
            )
        return updated

    @staticmethod
    def _is_component_replacement_completion(entity, action, merged):
        return (
            entity["kind"] == "maintenance"
            and action == "complete"
            and merged.get("work_type") == "component_replacement"
        )

    def _complete_component_replacement(self, actor, entity, next_status, merged, conn):
        """Complete a key component replacement as one atomic unit.

        Runs inside the caller's transaction (already under the write lock):
        the maintenance update, the voiding of the equipment's old passed
        inspections, the withdrawal of its pending/granted permits, and every
        audit entry commit together. If any write fails the transaction rolls
        back, so the equipment and its certificates never observe a
        half-new half-old state and the original records are preserved.
        """
        equipment_id = entity["data"].get("equipment_id")
        updated = self.repository.update_entity(
            entity["id"], entity["version"], next_status, merged, conn
        )
        self.repository.append_audit(
            entity["id"],
            actor.user_id,
            actor.role,
            "complete",
            entity["status"],
            updated["status"],
            {"patch": merged},
            conn,
        )

        inspections = self.repository.list_entities(kind="inspection", conn=conn)
        permits = self.repository.list_entities(kind="permit", conn=conn)
        void_inspections, revoke_permits = self.rules.component_replacement_effects(
            equipment_id, inspections, permits
        )

        for inspection in void_inspections:
            inspection_data = dict(inspection["data"])
            inspection_data["voided_by"] = actor.user_id
            inspection_data["voided_at"] = utcnow()
            self.repository.update_entity(
                inspection["id"], inspection["version"], "void", inspection_data, conn
            )
            self.repository.append_audit(
                inspection["id"],
                actor.user_id,
                actor.role,
                "void",
                inspection["status"],
                "void",
                {
                    "reason": "component_replacement_completed",
                    "maintenance_id": entity["id"],
                },
                conn,
            )

        for permit in revoke_permits:
            permit_data = dict(permit["data"])
            permit_data["revoked_by"] = actor.user_id
            permit_data["revoked_at"] = utcnow()
            permit_data["revoke_reason"] = "component_replacement_completed"
            self.repository.update_entity(
                permit["id"], permit["version"], "revoked", permit_data, conn
            )
            self.repository.append_audit(
                permit["id"],
                actor.user_id,
                actor.role,
                "revoke",
                permit["status"],
                "revoked",
                {
                    "reason": "component_replacement_completed",
                    "maintenance_id": entity["id"],
                },
                conn,
            )
        return updated

    def merge_offline(self, actor, records):
        """Merge field records by a stable (source_id, record_id) identity."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            digest = hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
            entity_id = "offline-" + digest
            with self.repository.transaction() as conn:
                existing = self.repository.get_entity(entity_id, conn)
                if existing:
                    created.append(existing)
                    continue
                payload = dict(raw)
                self.rules.validate_create(actor, "offline_record", payload, self._tx_lookup(conn))
                status = self.rules.initial_status("offline_record", payload)
                self.repository.create_entity(
                    entity_id,
                    "offline_record",
                    status,
                    payload,
                    actor.user_id,
                    conn,
                )
                self.repository.append_audit(
                    entity_id,
                    actor.user_id,
                    actor.role,
                    "merge_offline",
                    None,
                    status,
                    {"source_id": source_id, "record_id": record_id},
                    conn,
                )
        return created

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
