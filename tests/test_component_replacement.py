import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class PermitFailingRepository(SQLiteRepository):
    failing_permit_id = None

    def _update_entity(self, connection, entity_id, expected_version, status, data):
        if entity_id == self.failing_permit_id:
            raise RuntimeError("permit write failed")
        return super()._update_entity(connection, entity_id, expected_version, status, data)


class AuditFailingRepository(SQLiteRepository):
    def _append_audit(self, connection, entity_id, actor_id, actor_role, action,
                      from_status, to_status, detail):
        if action == "revoke":
            raise RuntimeError("audit write failed")
        return super()._append_audit(
            connection, entity_id, actor_id, actor_role, action,
            from_status, to_status, detail,
        )


class ComponentReplacementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repository = PermitFailingRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repository, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.inspector = Actor("inspector-1", "inspector")
        self.maintainer = Actor("maintainer-1", "maintenance")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, actor, kind, data):
        return self.service.create(actor, kind, data)

    def act(self, actor, entity, action, data=None, version=None):
        return self.service.transition(
            actor, entity["id"], action, data or {}, version if version is not None else entity["version"]
        )

    def prepare_component_replacement(self, repository=None):
        repository = repository or self.repository
        service = DomainService(repository, RuleEngine()) if repository is not self.repository else self.service
        equipment = service.create(
            self.admin,
            "equipment",
            {
                "asset_no": "E-CR-1",
                "equipment_type": "elevator",
                "location": "Tower A",
                "inspection_interval_days": 365,
            },
        )
        passed_inspection = service.create(
            self.admin,
            "inspection",
            {"equipment_id": equipment["id"], "scheduled_at": "2026-09-20T09:00:00Z", "cycle_days": 365},
        )
        passed_inspection = self.act(self.inspector, passed_inspection, "pass", {"findings": "normal"})

        scheduled_inspection = service.create(
            self.admin,
            "inspection",
            {"equipment_id": equipment["id"], "scheduled_at": "2026-10-10T09:00:00Z", "cycle_days": 365},
        )

        maintenance = service.create(
            self.admin,
            "maintenance",
            {
                "equipment_id": equipment["id"],
                "work_type": "component_replacement",
                "planned_at": "2026-09-30T08:00:00Z",
                "part_serial": "PART-42",
            },
        )
        maintenance = self.act(self.maintainer, maintenance, "start", {})

        pending_permit = service.create(
            self.admin,
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        pending_permit = self.act(self.inspector, pending_permit, "request_review", {})

        granted_permit = service.create(
            self.admin,
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        granted_permit = self.act(self.inspector, granted_permit, "request_review", {})
        granted_permit = self.act(self.inspector, granted_permit, "grant", {})

        remediation = service.create(
            self.admin,
            "remediation",
            {
                "equipment_id": equipment["id"],
                "issue": "controller wiring check",
                "owner": "Maint team",
                "due_at": "2026-10-05",
            },
        )

        blocked_permit = service.create(
            self.admin,
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        return {
            "service": service,
            "equipment": equipment,
            "passed_inspection": passed_inspection,
            "scheduled_inspection": scheduled_inspection,
            "maintenance": maintenance,
            "remediation": remediation,
            "pending_permit": pending_permit,
            "granted_permit": granted_permit,
            "blocked_permit": blocked_permit,
        }

    def test_completion_invalidates_old_inspections_and_withdraws_permits(self):
        fixture = self.prepare_component_replacement()
        completed = self.act(
            self.maintainer,
            fixture["maintenance"],
            "complete",
            {"completed_at": "2026-09-30T10:00:00Z"},
        )

        self.assertEqual(completed["status"], "completed")
        old_passed = self.service.get(fixture["passed_inspection"]["id"])
        old_scheduled = self.service.get(fixture["scheduled_inspection"]["id"])
        self.assertEqual(old_passed["status"], "invalidated")
        self.assertEqual(old_scheduled["status"], "invalidated")
        self.assertEqual(old_passed["data"]["caused_by_maintenance_id"], completed["id"])
        self.assertEqual(old_scheduled["data"]["reason"], "component replacement completed")

        self.assertEqual(self.service.get(fixture["pending_permit"]["id"])["status"], "revoked")
        self.assertEqual(self.service.get(fixture["granted_permit"]["id"])["status"], "revoked")
        self.assertEqual(self.service.get(fixture["blocked_permit"]["id"])["status"], "blocked")
        self.assertEqual(self.service.get(fixture["remediation"]["id"])["status"], "open")

        fresh_inspection = self.create(
            self.admin,
            "inspection",
            {
                "equipment_id": fixture["equipment"]["id"],
                "scheduled_at": "2026-10-01T09:00:00Z",
                "cycle_days": 365,
            },
        )
        fresh_inspection = self.act(self.inspector, fresh_inspection, "pass", {"findings": "new part verified"})
        self.assertEqual(fresh_inspection["status"], "passed")

        blocked_permit = self.service.get(fixture["blocked_permit"]["id"])
        blocked_permit = self.act(self.inspector, blocked_permit, "request_review", {})
        with self.assertRaises(ConflictError):
            self.act(self.inspector, blocked_permit, "grant", {})

        remediation = self.service.get(fixture["remediation"]["id"])
        remediation = self.act(self.maintainer, remediation, "submit_evidence", {"evidence": "PHOTO-1"})
        remediation = self.act(self.inspector, remediation, "verify", {})
        self.act(self.inspector, remediation, "close", {})

        blocked_permit = self.service.get(blocked_permit["id"])
        granted = self.act(self.inspector, blocked_permit, "grant", {})
        self.assertEqual(granted["status"], "granted")

    def test_inspection_pass_and_completion_conflict_when_submitted_concurrently(self):
        fixture = self.prepare_component_replacement()
        original_transaction = self.repository.transaction
        ready = []
        ready_lock = threading.Lock()
        gate = threading.Event()

        def synchronized_transaction():
            with ready_lock:
                ready.append(threading.get_ident())
                should_gate = len(ready) == 2
            if should_gate:
                gate.set()
            else:
                self.assertTrue(gate.wait(timeout=5))
            return original_transaction()

        self.repository.transaction = synchronized_transaction
        outcomes = {}

        def submit_pass():
            try:
                entity = fixture["scheduled_inspection"]
                result = self.service.transition(
                    self.inspector,
                    entity["id"],
                    "pass",
                    {"findings": "concurrent pass"},
                    entity["version"],
                )
                outcomes["pass"] = ("ok", result)
            except Exception as exc:
                outcomes["pass"] = ("error", exc)

        def complete_maintenance():
            try:
                result = self.service.transition(
                    self.maintainer,
                    fixture["maintenance"]["id"],
                    "complete",
                    {"completed_at": "2026-09-30T10:00:00Z"},
                    fixture["maintenance"]["version"],
                )
                outcomes["complete"] = ("ok", result)
            except Exception as exc:
                outcomes["complete"] = ("error", exc)

        with ThreadPoolExecutor(max_workers=2) as executor:
            future1 = executor.submit(submit_pass)
            future2 = executor.submit(complete_maintenance)
            future1.result(10)
            future2.result(10)

        self.assertEqual(set(outcomes), {"pass", "complete"})
        successes = [name for name, result in outcomes.items() if result[0] == "ok"]
        failures = [name for name, result in outcomes.items() if result[0] == "error"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(outcomes[failures[0]][1], ConflictError)

        inspection = self.service.get(fixture["scheduled_inspection"]["id"])
        maintenance = self.service.get(fixture["maintenance"]["id"])
        pass_landed = inspection["status"] == "passed" and maintenance["status"] == "in_progress"
        completion_landed = inspection["status"] == "invalidated" and maintenance["status"] == "completed"
        self.assertNotEqual(pass_landed, completion_landed)
        self.assertTrue(pass_landed or completion_landed)

    def test_ordinary_maintenance_does_not_invalidate_clearances(self):
        equipment = self.create(self.admin, "equipment", {
            "asset_no": "E-ROUTINE-1",
            "equipment_type": "elevator",
            "location": "Tower C",
            "inspection_interval_days": 365,
        })
        inspection = self.create(self.admin, "inspection", {
            "equipment_id": equipment["id"],
            "scheduled_at": "2026-09-20T09:00:00Z",
            "cycle_days": 365,
        })
        inspection = self.act(self.inspector, inspection, "pass", {"findings": "normal"})
        maintenance = self.create(self.admin, "maintenance", {
            "equipment_id": equipment["id"],
            "work_type": "routine",
            "planned_at": "2026-09-30T08:00:00Z",
        })
        maintenance = self.act(self.maintainer, maintenance, "start", {})
        maintenance = self.act(self.maintainer, maintenance, "complete", {"completed_at": "2026-09-30T10:00:00Z"})

        self.assertEqual(maintenance["status"], "completed")
        self.assertEqual(self.service.get(inspection["id"])["status"], "passed")
        self.assertEqual(self.service.get(equipment["id"])["version"], 2)

    def test_permit_write_failure_rolls_back_entire_completion(self):
        self.repository.failing_permit_id = None
        fixture = self.prepare_component_replacement()
        audit_count_before = len(self.service.audit_log())
        self.repository.failing_permit_id = fixture["granted_permit"]["id"]

        with self.assertRaisesRegex(RuntimeError, "permit write failed"):
            self.service.transition(
                self.maintainer,
                fixture["maintenance"]["id"],
                "complete",
                {"completed_at": "2026-09-30T10:00:00Z"},
                fixture["maintenance"]["version"],
            )

        self.assertEqual(self.service.get(fixture["maintenance"]["id"])["status"], "in_progress")
        self.assertEqual(self.service.get(fixture["passed_inspection"]["id"])["status"], "passed")
        self.assertEqual(self.service.get(fixture["granted_permit"]["id"])["status"], "granted")
        self.assertEqual(self.service.get(fixture["equipment"]["id"])["version"], 2)
        self.assertEqual(len(self.service.audit_log()), audit_count_before)

    def test_audit_write_failure_rolls_back_entire_completion(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repository = AuditFailingRepository(Path(tmp.name) / "audit-fail.db")
        service = DomainService(repository, RuleEngine())
        admin = self.admin
        equipment = service.create(admin, "equipment", {
            "asset_no": "E-CR-ROLLBACK",
            "equipment_type": "elevator",
            "location": "Tower B",
            "inspection_interval_days": 365,
        })
        inspection = service.create(admin, "inspection", {
            "equipment_id": equipment["id"],
            "scheduled_at": "2026-09-20T09:00:00Z",
            "cycle_days": 365,
        })
        service.transition(self.inspector, inspection["id"], "pass", {"findings": "normal"}, inspection["version"])
        maintenance = service.create(admin, "maintenance", {
            "equipment_id": equipment["id"],
            "work_type": "component_replacement",
            "planned_at": "2026-09-30T08:00:00Z",
            "part_serial": "PART-99",
        })
        service.transition(self.maintainer, maintenance["id"], "start", {}, maintenance["version"])
        permit = service.create(admin, "permit", {
            "equipment_id": equipment["id"],
            "purpose": "return_to_service",
            "requested_by": "ops",
        })
        permit = service.transition(self.inspector, permit["id"], "request_review", {}, permit["version"])
        permit = service.transition(self.inspector, permit["id"], "grant", {}, permit["version"])

        maintenance = repository.get_entity(maintenance["id"])
        inspection = repository.get_entity(inspection["id"])
        permit = repository.get_entity(permit["id"])
        audit_count_before = len(repository.list_audit())

        with self.assertRaisesRegex(RuntimeError, "audit write failed"):
            service.transition(
                self.maintainer,
                maintenance["id"],
                "complete",
                {"completed_at": "2026-09-30T10:00:00Z"},
                maintenance["version"],
            )

        self.assertEqual(repository.get_entity(maintenance["id"])["status"], "in_progress")
        self.assertEqual(repository.get_entity(inspection["id"])["status"], "passed")
        self.assertEqual(repository.get_entity(permit["id"])["status"], "granted")
        self.assertEqual(repository.get_entity(equipment["id"])["version"], 2)
        self.assertEqual(len(repository.list_audit()), audit_count_before)


if __name__ == "__main__":
    unittest.main()
