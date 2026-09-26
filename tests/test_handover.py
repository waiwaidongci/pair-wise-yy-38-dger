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
        self.item = self.service.create_item(
            {"title": "泄洪指令", "description": "汛期交接场景", "severity": "urgent",
             "quantity": 12, "threshold": 6, "external_ref": "HO-1"},
            "officer-a", "duty_officer")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _advance(self, item, target):
        return self.service.transition(item["id"], target, item["version"],
                                       "reviewer", TRANSITION_ROLES[target][0])

    def _create_handover(self, item_ids, incoming="officer-b", **extra):
        payload = {"incoming_actor": incoming, "reservoir_level": 183.5,
                   "item_ids": item_ids, "note": "汛期夜班交接"}
        payload.update(extra)
        return self.service.create_handover(payload, "officer-a", "duty_officer")

    def test_pending_item_locked_until_takeover(self):
        handover = self._create_handover([self.item["id"]])
        self.assertEqual(handover["status"], "pending")
        self.assertEqual(len(handover["items"]), 1)
        with self.assertRaises(ConflictError):
            self.service.transition(self.item["id"], STATES[1], self.item["version"],
                                    "reviewer", TRANSITION_ROLES[STATES[1]][0])
        result = self.service.decide_handover(
            handover["id"], {"item_id": self.item["id"], "decision": "takeover"},
            "officer-b", "duty_officer")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["items"][0]["decision"], "takeover")
        updated = self.service.transition(self.item["id"], STATES[1], self.item["version"],
                                          "reviewer", TRANSITION_ROLES[STATES[1]][0])
        self.assertEqual(updated["status"], STATES[1])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_return_reenters_review_with_reason(self):
        current = self.item
        for target in STATES[1:3]:
            current = self._advance(current, target)
        handover = self._create_handover([current["id"]])
        self.service.decide_handover(
            handover["id"],
            {"item_id": current["id"], "decision": "return",
             "reason": "下游警戒水位变化，需重新复核"},
            "officer-b", "duty_officer")
        item = self.service.get_item(current["id"], "viewer")
        self.assertEqual(item["status"], STATES[0])
        self.assertEqual(item["version"], current["version"] + 1)
        records = self.service.list_records(current["id"], "viewer")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "handover_return")
        self.assertEqual(records[0]["status"], "open")
        self.assertIn("重新复核", records[0]["detail"])
        again = self._advance(item, STATES[1])
        self.assertEqual(again["status"], STATES[1])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_return_requires_reason(self):
        handover = self._create_handover([self.item["id"]])
        with self.assertRaises(ValidationError):
            self.service.decide_handover(
                handover["id"], {"item_id": self.item["id"], "decision": "return"},
                "officer-b", "duty_officer")

    def test_only_incoming_officer_can_decide(self):
        handover = self._create_handover([self.item["id"]])
        with self.assertRaises(PermissionDenied):
            self.service.decide_handover(
                handover["id"], {"item_id": self.item["id"], "decision": "takeover"},
                "officer-a", "duty_officer")
        with self.assertRaises(PermissionDenied):
            self.service.decide_handover(
                handover["id"], {"item_id": self.item["id"], "decision": "takeover"},
                "officer-b", "viewer")

    def test_create_handover_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_handover(
                {"incoming_actor": "officer-b", "reservoir_level": 1,
                 "item_ids": [self.item["id"]]}, "officer-a", "viewer")
        with self.assertRaises(ValidationError):
            self._create_handover([self.item["id"]], incoming="officer-a")
        with self.assertRaises(ValidationError):
            self._create_handover([])
        with self.assertRaises(ValidationError):
            self._create_handover([self.item["id"], self.item["id"]])
        with self.assertRaises(NotFoundError):
            self._create_handover([9999])

    def test_executed_item_not_handoverable(self):
        current = self.item
        for target in STATES[1:4]:
            current = self._advance(current, target)
        with self.assertRaises(ConflictError):
            self._create_handover([current["id"]])

    def test_duplicate_pending_handover_rejected(self):
        self._create_handover([self.item["id"]])
        with self.assertRaises(ConflictError):
            self._create_handover([self.item["id"]], incoming="officer-c")

    def test_decide_twice_and_unknown_item(self):
        other = self.service.create_item(
            {"title": "另一条指令", "description": "不在交接中", "severity": "routine",
             "quantity": 1, "threshold": 10, "external_ref": "HO-2"},
            "officer-a", "duty_officer")
        handover = self._create_handover([self.item["id"]])
        with self.assertRaises(NotFoundError):
            self.service.decide_handover(
                handover["id"], {"item_id": other["id"], "decision": "takeover"},
                "officer-b", "duty_officer")
        self.service.decide_handover(
            handover["id"], {"item_id": self.item["id"], "decision": "takeover"},
            "officer-b", "duty_officer")
        with self.assertRaises(ConflictError):
            self.service.decide_handover(
                handover["id"], {"item_id": self.item["id"], "decision": "takeover"},
                "officer-b", "duty_officer")

    def test_handover_completes_after_all_items_decided(self):
        second = self.service.create_item(
            {"title": "第二条指令", "description": "批量交接", "severity": "attention",
             "quantity": 3, "threshold": 10, "external_ref": "HO-3"},
            "officer-a", "duty_officer")
        handover = self._create_handover([self.item["id"], second["id"]])
        result = self.service.decide_handover(
            handover["id"], {"item_id": self.item["id"], "decision": "takeover"},
            "officer-b", "duty_officer")
        self.assertEqual(result["status"], "pending")
        result = self.service.decide_handover(
            handover["id"], {"item_id": second["id"], "decision": "return",
                             "reason": "库位数据存疑"},
            "officer-b", "duty_officer")
        self.assertEqual(result["status"], "confirmed")
        decisions = {entry["item_id"]: entry["decision"] for entry in result["items"]}
        self.assertEqual(decisions[self.item["id"]], "takeover")
        self.assertEqual(decisions[second["id"]], "returned")

    def test_handover_audit_events_in_order(self):
        handover = self._create_handover([self.item["id"]])
        self.service.decide_handover(
            handover["id"], {"item_id": self.item["id"], "decision": "return",
                             "reason": "需重新复核"},
            "officer-b", "duty_officer")
        actions = [event["action"] for event in self.service.audit("viewer")]
        self.assertEqual(actions, ["create", "handover_create", "handover_return",
                                   "handover_decision", "handover_confirm"])
        self.assertTrue(self.repo.verify_audit_chain())
        handovers = self.service.list_handovers("viewer", status="confirmed")
        self.assertEqual(len(handovers), 1)


if __name__ == "__main__":
    unittest.main()
