import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.draft = self.service.create_item({
            "title": "待复核指令", "description": "汛期泄洪", "severity": "urgent",
            "quantity": 12, "threshold": 6, "external_ref": "HD-DRAFT-1",
        }, "zhang", "duty_officer")
        self.authorized = self.service.create_item({
            "title": "已授权指令", "description": "等待执行", "severity": "emergency",
            "quantity": 20, "threshold": 6, "external_ref": "HD-AUTH-1",
        }, "zhang", "duty_officer")
        current = self.authorized
        for target in STATES[1:3]:
            current = self.service.transition(
                current["id"], target, current["version"], "li",
                TRANSITION_ROLES[target][0])
        self.authorized = current

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _register(self, item_ids=None, actor="zhang"):
        return self.service.register_handover({
            "outgoing_officer": "zhang",
            "incoming_officer": "wang",
            "reservoir_level": 145.2,
            "personnel": ["zhang", "wang", "zhao"],
            "note": "汛期夜班交接",
            "item_ids": item_ids if item_ids is not None else [self.draft["id"], self.authorized["id"]],
        }, actor, "duty_officer")

    def test_register_locks_items(self):
        handover = self._register()
        self.assertEqual(handover["status"], "open")
        self.assertEqual(handover["personnel"], ["zhang", "wang", "zhao"])
        self.assertEqual(len(handover["items"]), 2)
        self.assertTrue(all(item["locked"] for item in handover["items"]))
        item = self.service.get_item(self.draft["id"], "viewer")
        self.assertEqual(item["handover_lock"]["state"], "pending_confirmation")
        self.assertEqual(item["handover_lock"]["handover_id"], handover["id"])
        with self.assertRaises(ConflictError):
            self.service.add_record(self.draft["id"], {
                "kind": "review", "detail": "锁定期尝试复核",
            }, "wang", "duty_officer")
        with self.assertRaises(ConflictError):
            self.service.transition(self.draft["id"], STATES[1],
                                    self.draft["version"], "wang", "duty_officer")

    def test_double_open_registration_conflicts(self):
        self._register()
        with self.assertRaises(ConflictError):
            self._register(item_ids=[self.draft["id"]])

    def test_register_requires_personnel_and_items(self):
        with self.assertRaises(ValidationError):
            self.service.register_handover({
                "outgoing_officer": "zhang", "incoming_officer": "wang",
                "reservoir_level": 1, "personnel": [], "item_ids": [self.draft["id"]],
            }, "zhang", "duty_officer")
        with self.assertRaises(ValidationError):
            self.service.register_handover({
                "outgoing_officer": "zhang", "incoming_officer": "wang",
                "reservoir_level": 1, "personnel": ["zhang"], "item_ids": [],
            }, "zhang", "duty_officer")

    def test_register_permission(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_handover({
                "outgoing_officer": "a", "incoming_officer": "b",
                "reservoir_level": 1, "personnel": ["a"],
                "item_ids": [self.draft["id"]],
            }, "viewer", "viewer")

    def test_accept_unlocks_and_flow_continues(self):
        handover = self._register()
        entry = next(i for i in handover["items"] if i["item_id"] == self.draft["id"])
        result = self.service.decide_handover_item(
            handover["id"], entry["id"], {"decision": "accepted"}, "wang", "duty_officer")
        accepted = next(i for i in result["items"] if i["id"] == entry["id"])
        self.assertEqual(accepted["decision"], "accepted")
        self.assertFalse(accepted["locked"])
        # 接手后仍有另一条未确认，交接单未完成，但该条已可流转
        item = self.service.get_item(self.draft["id"], "viewer")
        self.assertNotIn("handover_lock", item)
        moved = self.service.transition(self.draft["id"], STATES[1],
                                        self.draft["version"], "wang", "duty_officer")
        self.assertEqual(moved["status"], "checked")
        # 另一条仍锁定
        locked = self.service.get_item(self.authorized["id"], "viewer")
        self.assertIn("handover_lock", locked)
        self.assertEqual(self.service.get_handover(handover["id"], "viewer")["status"], "open")

    def test_return_requires_reason_and_reopens_review(self):
        handover = self._register()
        entry = next(i for i in handover["items"] if i["item_id"] == self.authorized["id"])
        old_version = self.authorized["version"]
        with self.assertRaises(ValidationError):
            self.service.decide_handover_item(
                handover["id"], entry["id"], {"decision": "returned"},
                "wang", "duty_officer")
        result = self.service.decide_handover_item(
            handover["id"], entry["id"],
            {"decision": "returned", "reason": "授权依据与当前库位不符，需重新复核"},
            "wang", "duty_officer")
        returned = next(i for i in result["items"] if i["id"] == entry["id"])
        self.assertEqual(returned["decision"], "returned")
        self.assertEqual(returned["reason"], "授权依据与当前库位不符，需重新复核")
        item = self.service.get_item(self.authorized["id"], "viewer")
        self.assertEqual(item["status"], STATES[0])
        self.assertEqual(item["version"], old_version + 1)
        self.assertNotIn("handover_lock", item)
        # 重新进入复核：可以再次复核流转
        checked = self.service.transition(item["id"], STATES[1], item["version"],
                                          "wang", "duty_officer")
        self.assertEqual(checked["status"], "checked")

    def test_all_decided_completes_handover_and_audit_chain(self):
        handover = self._register()
        entries = handover["items"]
        self.service.decide_handover_item(
            handover["id"], entries[0]["id"], {"decision": "accepted"},
            "wang", "duty_officer")
        result = self.service.decide_handover_item(
            handover["id"], entries[1]["id"],
            {"decision": "returned", "reason": "数据待核实"},
            "wang", "duty_officer")
        self.assertEqual(result["status"], "completed")
        self.assertIsNotNone(result["completed_at"])
        with self.assertRaises(ConflictError):
            self.service.decide_handover_item(
                handover["id"], entries[0]["id"], {"decision": "accepted"},
                "wang", "duty_officer")
        events = self.service.audit("viewer", handover["id"], "交接单")
        actions = [e["action"] for e in events]
        self.assertEqual(actions[0], "handover_register")
        self.assertIn("handover_accept", actions)
        self.assertIn("handover_return", actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_closed_item_cannot_register(self):
        item = self.service.create_item({
            "title": "闭环指令", "description": "done", "severity": "routine",
            "quantity": 1, "threshold": 10, "external_ref": "HD-CLOSED-1",
        }, "zhang", "duty_officer")
        self.service.add_record(item["id"], {"kind": "evidence",
                                             "detail": "闭环材料", "status": "closed"},
                                "zhang", "duty_officer")
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "li",
                TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError):
            self._register(item_ids=[current["id"]])

    def test_decision_on_foreign_entry_not_found(self):
        first = self._register()
        other = self.service.create_item({
            "title": "其他指令", "description": "x", "severity": "routine",
            "quantity": 1, "threshold": 10, "external_ref": "HD-OTHER-1",
        }, "zhang", "duty_officer")
        second = self.service.register_handover({
            "outgoing_officer": "chen", "incoming_officer": "sun",
            "reservoir_level": 100, "personnel": ["chen", "sun"],
            "item_ids": [other["id"]],
        }, "chen", "duty_officer")
        foreign = second["items"][0]["id"]
        with self.assertRaises(NotFoundError):
            self.service.decide_handover_item(
                first["id"], foreign, {"decision": "accepted"},
                "wang", "duty_officer")


if __name__ == "__main__":
    unittest.main()
