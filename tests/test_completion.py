import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ComponentCompletionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repository = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repository, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def act(self, entity, action, data=None, version=None):
        return self.service.transition(self.actor, entity["id"], action, data or {}, version)

    def _setup_equipment_with_passed_inspection(self, asset_no="E-100"):
        equipment = self.create(
            "equipment",
            {"asset_no": asset_no, "equipment_type": "elevator", "location": "A", "inspection_interval_days": 365},
        )
        inspection = self.create(
            "inspection",
            {"equipment_id": equipment["id"], "scheduled_at": "2026-09-27T09:00:00Z", "cycle_days": 365},
        )
        inspection = self.act(inspection, "pass", {"findings": "normal"})
        return equipment, inspection

    def _setup_maintenance(self, equipment_id, serial="SN-1"):
        maintenance = self.create(
            "maintenance",
            {
                "equipment_id": equipment_id,
                "work_type": "component_replacement",
                "part_serial": serial,
                "planned_at": "2026-09-28T09:00:00Z",
            },
        )
        maintenance = self.act(maintenance, "start", {})
        maintenance = self.act(maintenance, "complete", {"completed_at": "2026-09-29T09:00:00Z"})
        return maintenance

    def test_completion_voids_old_inspections_and_revokes_permits(self):
        equipment, old_inspection = self._setup_equipment_with_passed_inspection()

        permit = self.create(
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.act(permit, "request_review", {})
        permit = self.act(permit, "grant", {})
        self.assertEqual(permit["status"], "granted")

        maintenance = self.create(
            "maintenance",
            {
                "equipment_id": equipment["id"],
                "work_type": "component_replacement",
                "part_serial": "SN-9",
                "planned_at": "2026-09-28T09:00:00Z",
            },
        )
        maintenance = self.act(maintenance, "start", {})
        maintenance = self.act(maintenance, "complete", {"completed_at": "2026-09-29T09:00:00Z"})

        self.assertEqual(maintenance["status"], "completed")

        old_inspection = self.service.get(old_inspection["id"])
        self.assertEqual(old_inspection["status"], "void")
        self.assertEqual(old_inspection["data"]["voided_by"], self.actor.user_id)

        permit = self.service.get(permit["id"])
        self.assertEqual(permit["status"], "revoked")
        self.assertEqual(permit["data"]["revoke_reason"], "component_replacement_completed")

        inspections = self.service.list("inspection")
        self.assertTrue(all(i["status"] != "passed" for i in inspections))

    def test_completion_revokes_pending_review_permit(self):
        equipment, _ = self._setup_equipment_with_passed_inspection()
        permit = self.create(
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.act(permit, "request_review", {})
        self.assertEqual(permit["status"], "pending_review")

        self._setup_maintenance(equipment["id"])

        permit = self.service.get(permit["id"])
        self.assertEqual(permit["status"], "revoked")

    def test_open_remediation_blocks_regrant_after_completion(self):
        equipment, _ = self._setup_equipment_with_passed_inspection()

        remediation = self.create(
            "remediation",
            {"equipment_id": equipment["id"], "issue": "door alignment", "owner": "Maint", "due_at": "2026-10-01"},
        )
        remediation = self.act(remediation, "submit_evidence", {"evidence": "IMG-1"})
        remediation = self.act(remediation, "verify", {})
        remediation = self.act(remediation, "close", {})

        permit = self.create(
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.act(permit, "request_review", {})
        permit = self.act(permit, "grant", {})

        self._setup_maintenance(equipment["id"])

        # Old permit is revoked and the old inspection is void.
        permit = self.service.get(permit["id"])
        self.assertEqual(permit["status"], "revoked")

        # A new inspection passes, but an unclosed remediation still blocks re-grant.
        new_inspection = self.create(
            "inspection",
            {"equipment_id": equipment["id"], "scheduled_at": "2026-09-30T09:00:00Z", "cycle_days": 365},
        )
        new_inspection = self.act(new_inspection, "pass", {"findings": "replaced component checked"})

        open_remediation = self.create(
            "remediation",
            {"equipment_id": equipment["id"], "issue": "brake wear", "owner": "Maint", "due_at": "2026-10-05"},
        )
        self.assertEqual(open_remediation["status"], "open")

        new_permit = self.create(
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        new_permit = self.act(new_permit, "request_review", {})
        with self.assertRaises(ConflictError):
            self.act(new_permit, "grant", {})

    def test_passed_inspection_required_before_regraint(self):
        equipment, _ = self._setup_equipment_with_passed_inspection()
        self._setup_maintenance(equipment["id"])

        # After completion the old inspection is void; a new inspection must pass first.
        new_inspection = self.create(
            "inspection",
            {"equipment_id": equipment["id"], "scheduled_at": "2026-09-30T09:00:00Z", "cycle_days": 365},
        )
        new_inspection = self.act(new_inspection, "pass", {"findings": "replaced component checked"})
        self.assertEqual(new_inspection["status"], "passed")

        permit = self.create(
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.act(permit, "request_review", {})
        permit = self.act(permit, "grant", {})
        self.assertEqual(permit["status"], "granted")

    def test_non_component_maintenance_does_not_void(self):
        equipment, inspection = self._setup_equipment_with_passed_inspection()
        maintenance = self.create(
            "maintenance",
            {
                "equipment_id": equipment["id"],
                "work_type": "routine",
                "planned_at": "2026-09-28T09:00:00Z",
            },
        )
        maintenance = self.act(maintenance, "start", {})
        maintenance = self.act(maintenance, "complete", {"completed_at": "2026-09-29T09:00:00Z"})

        inspection = self.service.get(inspection["id"])
        self.assertEqual(inspection["status"], "passed")

    def test_concurrent_inspection_pass_only_one_settles(self):
        equipment, _ = self._setup_equipment_with_passed_inspection(asset_no="E-200")
        inspection = self.create(
            "inspection",
            {"equipment_id": equipment["id"], "scheduled_at": "2026-09-27T09:00:00Z", "cycle_days": 365},
        )
        self.assertEqual(inspection["status"], "scheduled")
        version = inspection["version"]

        barrier = threading.Barrier(2, timeout=10)
        results = []

        def pass_inspection():
            barrier.wait()
            try:
                updated = self.service.transition(
                    self.actor, inspection["id"], "pass", {"findings": "ok"}, version
                )
                results.append(("ok", updated["status"]))
            except ConflictError as exc:
                results.append(("conflict", str(exc)))
            except Exception as exc:
                results.append(("error", "%s: %s" % (type(exc).__name__, exc)))

        threads = [threading.Thread(target=pass_inspection) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        if len(results) != 2 or sum(1 for status, _ in results if status == "ok") != 1:
            self.fail("unexpected concurrent results: %r" % results)
        self.assertEqual(sum(1 for status, _ in results if status == "ok"), 1)
        self.assertEqual(sum(1 for status, _ in results if status == "conflict"), 1, "results were: %r" % results)

        final = self.service.get(inspection["id"])
        self.assertEqual(final["status"], "passed")
        self.assertEqual(final["version"], version + 1)

    def test_audit_failure_rolls_back_completion(self):
        equipment, old_inspection = self._setup_equipment_with_passed_inspection()
        permit = self.create(
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.act(permit, "request_review", {})
        permit = self.act(permit, "grant", {})

        maintenance = self.create(
            "maintenance",
            {
                "equipment_id": equipment["id"],
                "work_type": "component_replacement",
                "part_serial": "SN-9",
                "planned_at": "2026-09-28T09:00:00Z",
            },
        )
        maintenance = self.act(maintenance, "start", {})

        original_append = self.repository._append_audit

        def failing_append(*args, **kwargs):
            raise RuntimeError("audit write failed")

        self.repository._append_audit = failing_append
        try:
            with self.assertRaises(RuntimeError):
                self.act(maintenance, "complete", {"completed_at": "2026-09-29T09:00:00Z"})
        finally:
            self.repository._append_audit = original_append

        maintenance = self.service.get(maintenance["id"])
        self.assertEqual(maintenance["status"], "in_progress")
        self.assertEqual(maintenance["version"], 2)

        old_inspection = self.service.get(old_inspection["id"])
        self.assertEqual(old_inspection["status"], "passed")

        permit = self.service.get(permit["id"])
        self.assertEqual(permit["status"], "granted")

    def test_permit_write_failure_rolls_back_completion(self):
        equipment, old_inspection = self._setup_equipment_with_passed_inspection()
        permit = self.create(
            "permit",
            {"equipment_id": equipment["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )
        permit = self.act(permit, "request_review", {})
        permit = self.act(permit, "grant", {})

        maintenance = self.create(
            "maintenance",
            {
                "equipment_id": equipment["id"],
                "work_type": "component_replacement",
                "part_serial": "SN-9",
                "planned_at": "2026-09-28T09:00:00Z",
            },
        )
        maintenance = self.act(maintenance, "start", {})

        original_update = self.repository._update_entity

        def failing_update(connection, entity_id, expected_version, status, data):
            entity = self.repository.get_entity(entity_id, conn=connection)
            if entity and entity["kind"] == "permit":
                raise RuntimeError("permit write failed")
            return original_update(connection, entity_id, expected_version, status, data)

        self.repository._update_entity = failing_update
        try:
            with self.assertRaises(RuntimeError):
                self.act(maintenance, "complete", {"completed_at": "2026-09-29T09:00:00Z"})
        finally:
            self.repository._update_entity = original_update

        maintenance = self.service.get(maintenance["id"])
        self.assertEqual(maintenance["status"], "in_progress")
        self.assertEqual(maintenance["version"], 2)

        old_inspection = self.service.get(old_inspection["id"])
        self.assertEqual(old_inspection["status"], "passed")

        permit = self.service.get(permit["id"])
        self.assertEqual(permit["status"], "granted")


if __name__ == "__main__":
    unittest.main()
