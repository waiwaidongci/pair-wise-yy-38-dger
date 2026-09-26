from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_id, require_number, require_text,
                     require_text_list)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY,
                    HANDOVER_DECISIONS, HANDOVER_ENTITY, HANDOVER_ROLES,
                    HANDOVER_VIEW_ROLES, RECORD_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required,
                    handover_lock_message, handover_registerable,
                    priority_score, response_deadline_hours,
                    return_review_state, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        item_id = require_id(item_id, "item_id")
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        self._require_unlocked(item_id)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item_id = require_id(item_id, "item_id")
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        self._require_unlocked(item_id)
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def _require_unlocked(self, item_id: int) -> None:
        lock = self.repository.open_lock_for_item(item_id)
        if lock is not None:
            raise handover_lock_message(lock["handover_id"])

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item_id = require_id(item_id, "item_id")
        item = self.enrich(self.repository.get_item(item_id))
        lock = self.repository.open_lock_for_item(item_id)
        if lock is not None:
            item["handover_lock"] = self._lock_view(lock)
        return item

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        items = [self.enrich(item) for item in self.repository.list_items(status)]
        locks = self.repository.open_locks_for_items([item["id"] for item in items])
        for item in items:
            lock = locks.get(item["id"])
            if lock is not None:
                item["handover_lock"] = self._lock_view(lock)
        return items

    def register_handover(self, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        ensure_role(role, HANDOVER_ROLES)
        actor = require_text(actor, "actor", 100)
        outgoing_officer = require_text(payload.get("outgoing_officer"), "outgoing_officer", 100)
        incoming_officer = require_text(payload.get("incoming_officer"), "incoming_officer", 100)
        reservoir_level = require_number(payload.get("reservoir_level", 0), "reservoir_level")
        personnel = require_text_list(payload.get("personnel"), "personnel")
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note")
        raw_ids = payload.get("item_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            from .domain import ValidationError
            raise ValidationError("item_ids必须是非空数组")
        item_ids: List[int] = []
        for raw_id in raw_ids:
            item_id = require_id(raw_id, "item_id")
            if item_id not in item_ids:
                item_ids.append(item_id)
        for item_id in item_ids:
            item = self.repository.get_item(item_id)
            if not handover_registerable(item["status"]):
                raise ConflictError(f"指令#{item_id}已关闭，不能登记交接")
        handover = self.repository.create_handover(
            outgoing_officer, incoming_officer, reservoir_level, personnel,
            note, item_ids, actor)
        self.repository.append_audit("handover_register", HANDOVER_ENTITY,
                                     handover["id"], actor, {
            "outgoing_officer": outgoing_officer,
            "incoming_officer": incoming_officer,
            "reservoir_level": reservoir_level,
            "personnel": personnel,
            "item_ids": item_ids,
        })
        return self.handover_view(handover["id"], role)

    def decide_handover_item(self, handover_id: int, handover_item_id: int,
                             payload: Dict[str, Any], actor: str,
                             role: str) -> Dict[str, Any]:
        ensure_role(role, HANDOVER_ROLES)
        actor = require_text(actor, "actor", 100)
        handover_id = require_id(handover_id, "handover_id")
        handover_item_id = require_id(handover_item_id, "handover_item_id")
        decision = payload.get("decision")
        if decision not in HANDOVER_DECISIONS:
            from .domain import ValidationError
            raise ValidationError("decision必须是accepted或returned")
        handover = self.repository.get_handover(handover_id)
        if handover["status"] != "open":
            raise ConflictError("交接单已完成，不能再确认")
        entry = self.repository.get_handover_item(handover_item_id)
        if entry["handover_id"] != handover_id:
            from .domain import NotFoundError
            raise NotFoundError("交接明细不属于该交接单")
        item_id = entry["item_id"]
        if decision == "accepted":
            decided = self.repository.accept_handover_item(handover_item_id, actor)
            self.repository.append_audit("handover_accept", HANDOVER_ENTITY,
                                         handover_id, actor, {
                "handover_item_id": handover_item_id,
                ENTITY: item_id,
                "incoming_officer": handover["incoming_officer"],
            })
        else:
            reason = require_text(payload.get("reason"), "reason")
            decided = self.repository.return_handover_item(handover_item_id, reason, actor)
            self.repository.append_audit("handover_return", HANDOVER_ENTITY,
                                         handover_id, actor, {
                "handover_item_id": handover_item_id,
                ENTITY: item_id,
                "reason": reason,
                "to": return_review_state(),
            })
        return self.handover_view(handover_id, role)

    def get_handover(self, handover_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, HANDOVER_VIEW_ROLES)
        return self.handover_view(handover_id, role)

    def list_handovers(self, role: str, status: Optional[str] = None) -> list:
        ensure_role(role, HANDOVER_VIEW_ROLES)
        handovers = self.repository.list_handovers(status)
        return [self._handover_summary(h) for h in handovers]

    def handover_view(self, handover_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, HANDOVER_VIEW_ROLES)
        handover = self.repository.get_handover(handover_id)
        result = self._handover_summary(handover)
        result["items"] = [self._handover_item_view(row)
                           for row in self.repository.list_handover_items(handover_id)]
        return result

    @staticmethod
    def _lock_view(lock: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "handover_id": lock["handover_id"],
            "handover_item_id": lock["handover_item_id"],
            "incoming_officer": lock["incoming_officer"],
            "state": "pending_confirmation",
        }

    @staticmethod
    def _handover_summary(handover: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(handover)
        result["personnel"] = json.loads(result["personnel"])
        return result

    @staticmethod
    def _handover_item_view(row: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(row)
        result["locked"] = result["decision"] is None
        return result

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None,
              entity_type: Optional[str] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id, entity_type)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
