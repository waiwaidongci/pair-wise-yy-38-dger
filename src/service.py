from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, PermissionDenied, ValidationError,
                     ensure_role, normalize_severity, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, HANDOVER_CREATE_ROLES,
                    HANDOVER_DECIDE_ROLES, HANDOVER_DECISIONS, HANDOVER_ENTITY,
                    HANDOVER_STATUS, RECORD_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, handover_return_target,
                    handoverable, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


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
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if self.repository.pending_handover_for_item(item_id) is not None:
            raise ConflictError("指令待交接确认，已锁定")
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
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

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def create_handover(self, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, HANDOVER_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        incoming = require_text(payload.get("incoming_actor"), "incoming_actor", 100)
        if incoming == actor:
            raise ValidationError("交班人与接班人不能为同一人")
        reservoir_level = require_number(payload.get("reservoir_level"), "reservoir_level")
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 500)
        item_ids = self._require_item_ids(payload.get("item_ids"))
        for item_id in item_ids:
            item = self.repository.get_item(item_id)
            if not handoverable(item["status"]):
                raise ConflictError(f"指令{item_id}状态为{item['status']}，不在可交接范围内")
        handover = self.repository.create_handover(actor, incoming, reservoir_level,
                                                   note, item_ids)
        self.repository.append_audit("handover_create", HANDOVER_ENTITY, handover["id"],
                                     actor, {
                                         "incoming_actor": incoming,
                                         "reservoir_level": reservoir_level,
                                         "item_ids": item_ids,
                                     })
        handover["items"] = self.repository.list_handover_items(handover["id"])
        return handover

    def decide_handover(self, handover_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, HANDOVER_DECIDE_ROLES)
        actor = require_text(actor, "actor", 100)
        handover = self.repository.get_handover(handover_id)
        if handover["status"] != "pending":
            raise ConflictError("交接已完成，不能再确认")
        if actor != handover["incoming_actor"]:
            raise PermissionDenied("只有接班人能确认交接")
        item_id = payload.get("item_id")
        if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id < 1:
            raise ValidationError("item_id必须是正整数")
        decision = payload.get("decision")
        if decision not in HANDOVER_DECISIONS:
            raise ValidationError("decision必须是takeover或return")
        reason = payload.get("reason")
        if decision == "return":
            reason = require_text(reason, "reason")
        elif reason is not None:
            reason = require_text(reason, "reason")
        item = self.repository.get_item(item_id)
        stored = "returned" if decision == "return" else decision
        self.repository.decide_handover_item(handover_id, item_id, stored, reason, actor)
        if decision == "return":
            target = handover_return_target(item["status"])
            updated = self.repository.transition_item(item_id, target,
                                                      item["version"], actor)
            record = self.repository.add_record(item_id, "handover_return", reason,
                                                "open", None, actor)
            self.repository.append_audit("handover_return", ENTITY, item_id, actor, {
                "handover_id": handover_id, "from": item["status"], "to": target,
                "reason": reason, "record_id": record["id"],
                "version": updated["version"],
            })
        self.repository.append_audit("handover_decision", HANDOVER_ENTITY, handover_id,
                                     actor, {
                                         "item_id": item_id, "decision": decision,
                                         "reason": reason,
                                     })
        if self.repository.complete_handover(handover_id):
            self.repository.append_audit("handover_confirm", HANDOVER_ENTITY, handover_id,
                                         actor, {"incoming_actor": actor})
        return self.get_handover(handover_id, role)

    def get_handover(self, handover_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        handover = self.repository.get_handover(handover_id)
        handover["items"] = self.repository.list_handover_items(handover_id)
        return handover

    def list_handovers(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in HANDOVER_STATUS:
            raise ValidationError("未知交接状态")
        return self.repository.list_handovers(status)

    @staticmethod
    def _require_item_ids(value: Any) -> List[int]:
        if not isinstance(value, list) or not value:
            raise ValidationError("item_ids必须是非空数组")
        item_ids: List[int] = []
        for item_id in value:
            if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id < 1:
                raise ValidationError("item_ids必须是正整数")
            if item_id in item_ids:
                raise ValidationError("item_ids存在重复")
            item_ids.append(item_id)
        return item_ids

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
