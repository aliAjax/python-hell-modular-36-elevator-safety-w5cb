import hashlib
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value, connection=None):
        return self.repository.find_entities(
            self.rules.normalize_kind(kind), field, value, connection=connection
        )

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        payload = dict(data or {})
        next_status, patch, effects = self.rules.plan_transition(
            actor, entity, action, payload, self._lookup
        )
        aggregate_id = self.rules.aggregate_lock_id(entity, action)
        aggregate = self.repository.get_entity(aggregate_id) if aggregate_id else None
        if aggregate_id and not aggregate:
            raise NotFoundError("aggregate equipment not found: " + aggregate_id)
        aggregate_version = aggregate["version"] if aggregate else None

        with self.repository.transaction() as connection:
            locked_entity = self.repository.get_entity(entity_id, connection=connection)
            if not locked_entity:
                raise NotFoundError("entity not found: " + entity_id)
            locked_aggregate = (
                self.repository.get_entity(aggregate_id, connection=connection)
                if aggregate_id
                else None
            )
            if aggregate_id and not locked_aggregate:
                raise NotFoundError("aggregate equipment not found: " + aggregate_id)
            if locked_aggregate and locked_aggregate["version"] != aggregate_version:
                raise ConflictError("version conflict on equipment; refetch the latest data")

            next_status, patch, effects = self.rules.plan_transition(
                actor,
                locked_entity,
                action,
                payload,
                lambda kind, field, value: self._lookup(kind, field, value, connection=connection),
            )
            merged = dict(locked_entity["data"])
            merged.update(patch)
            updated = self.repository.update_entity(
                entity_id, expected, next_status, merged, connection=connection
            )
            self.repository.append_audit(
                connection=connection,
                entity_id=entity_id,
                actor_id=actor.user_id,
                actor_role=actor.role,
                action=action,
                from_status=locked_entity["status"],
                to_status=updated["status"],
                detail={"patch": patch, "effects": len(effects)},
            )

            if locked_aggregate:
                self.repository.update_entity(
                    aggregate_id,
                    aggregate_version,
                    locked_aggregate["status"],
                    dict(locked_aggregate["data"]),
                    connection=connection,
                )
                self.repository.append_audit(
                    connection=connection,
                    entity_id=aggregate_id,
                    actor_id=actor.user_id,
                    actor_role=actor.role,
                    action="safety_epoch",
                    from_status=locked_aggregate["status"],
                    to_status=locked_aggregate["status"],
                    detail={"caused_by": entity_id, "action": action},
                )

            for effect in effects:
                target = self.repository.get_entity(effect["entity_id"], connection=connection)
                if not target:
                    raise NotFoundError("effect target not found: " + effect["entity_id"])
                if target["status"] not in effect["expected_statuses"]:
                    raise InvalidTransition(
                        "cannot %s %s from status %s"
                        % (effect["action"], effect["entity_id"], target["status"])
                    )
                effect_data = dict(target["data"])
                effect_data.update(effect["patch"])
                effect_updated = self.repository.update_entity(
                    effect["entity_id"],
                    target["version"],
                    effect["status"],
                    effect_data,
                    connection=connection,
                )
                self.repository.append_audit(
                    connection=connection,
                    entity_id=effect["entity_id"],
                    actor_id=actor.user_id,
                    actor_role=actor.role,
                    action=effect["action"],
                    from_status=target["status"],
                    to_status=effect_updated["status"],
                    detail={"patch": effect["patch"], "caused_by": entity_id},
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
            existing = self.repository.get_entity(entity_id)
            if existing:
                created.append(existing)
                continue
            payload = dict(raw)
            self.rules.validate_create(actor, "offline_record", payload, self._lookup)
            entity = self.repository.create_entity(
                entity_id,
                "offline_record",
                self.rules.initial_status("offline_record", payload),
                payload,
                actor.user_id,
            )
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"], {"source_id": source_id, "record_id": record_id})
            created.append(entity)
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
