from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine, compute_contact_followup


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

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
        status = self.rules.initial_status(kind)
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
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(
            entity_id, expected, next_status if next_status is not None else entity["status"], merged
        )
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        # 病例发病日期改动后，联动重算所有接触者的随访截止时间与状态
        if entity["kind"] == "case" and action == "update_onset":
            self._recalculate_contacts(actor, updated)
        return updated

    def _recalculate_contacts(self, actor, case):
        onset = case["data"].get("onset_date")
        contacts = self._lookup("contact", "case_id", case["id"]) or []
        for contact in contacts:
            patch, needs_review, reason = compute_contact_followup(contact, onset)
            if not patch:
                continue
            merged = dict(contact.get("data") or {})
            merged.update(patch)
            if needs_review:
                merged["needs_review"] = True
                merged["review_reason"] = reason
            else:
                merged["needs_review"] = False
                merged.pop("review_reason", None)
            new_status = patch.get("status", contact["status"])
            self.repository.update_entity(contact["id"], contact["version"], new_status, merged)
            self.audit.record(
                contact["id"],
                actor,
                "recalculate_followup",
                contact["status"],
                new_status,
                {"case_id": case["id"], "patch": patch, "needs_review": needs_review},
            )

    def merge_contacts(self, actor, batch_id, contacts):
        """合并断网期间离线登记的接触者。

        以 batch_id + client_id 做幂等：同一批次重试不会重复创建；
        同一 case_id 下 person_id 相同的记录按人去重，不会重复登记同一个人。
        单条失败不影响整批，按原批次重试时只处理尚未成功的记录。
        """
        if not batch_id:
            raise ValidationError("batch_id is required")
        if not isinstance(contacts, list):
            raise ValidationError("contacts must be a list")
        self.rules._ensure_role(actor, self.rules.CREATE_ROLES.get("contact", ("admin",)))
        self.repository.upsert_merge_batch(batch_id, actor.user_id, len(contacts))
        results = []
        merged = duplicates = errors = 0
        for item in contacts:
            if not isinstance(item, dict):
                errors += 1
                results.append({"client_id": None, "status": "error", "error": "item must be an object"})
                continue
            client_id = item.get("client_id") or item.get("temp_id")
            if not client_id:
                errors += 1
                results.append({"client_id": None, "status": "error", "error": "client_id is required"})
                continue
            idem_key = "merge:%s:%s" % (batch_id, client_id)
            existing_id = self.repository.get_idempotency(actor.user_id, idem_key)
            if existing_id:
                merged += 1
                results.append({"client_id": client_id, "status": "already_merged", "entity_id": existing_id})
                continue
            person_id = item.get("person_id")
            case_id = item.get("case_id")
            if not person_id or not case_id:
                errors += 1
                results.append({"client_id": client_id, "status": "error", "error": "case_id and person_id are required"})
                self.repository.record_merge_item(
                    batch_id, client_id, None, "error", {"error": "case_id and person_id are required"}
                )
                continue
            dup = None
            for candidate in self._lookup("contact", "case_id", case_id) or []:
                if candidate["data"].get("person_id") == person_id:
                    dup = candidate
                    break
            if dup:
                self.repository.save_idempotency(actor.user_id, idem_key, dup["id"])
                self.repository.record_merge_item(
                    batch_id, client_id, dup["id"], "duplicate", {"duplicate_of": dup["id"]}
                )
                duplicates += 1
                results.append({"client_id": client_id, "status": "duplicate", "entity_id": dup["id"]})
                continue
            case = self.repository.get_entity(case_id)
            if not case or case["kind"] != "case":
                errors += 1
                results.append({"client_id": client_id, "status": "error", "error": "case not found: %s" % case_id})
                self.repository.record_merge_item(
                    batch_id, client_id, None, "error", {"error": "case not found: %s" % case_id}
                )
                continue
            payload = {key: value for key, value in item.items() if key not in ("client_id", "temp_id")}
            payload["case_id"] = case_id
            payload["person_id"] = person_id
            needs_review = not payload.get("exposure_start")
            payload["needs_review"] = needs_review
            if needs_review:
                payload["review_reason"] = "missing exposure_start"
            entity_id = str(uuid4())
            entity = self.repository.create_entity(entity_id, "contact", "identified", payload, actor.user_id)
            self.audit.record(entity_id, actor, "merge_create", None, "identified", {"batch_id": batch_id})
            self.repository.save_idempotency(actor.user_id, idem_key, entity_id)
            self.repository.record_merge_item(batch_id, client_id, entity_id, "merged", {})
            merged += 1
            results.append({"client_id": client_id, "status": "merged", "entity_id": entity_id})
        self.repository.finish_merge_batch(batch_id, merged, duplicates, errors)
        return {
            "batch_id": batch_id,
            "items": results,
            "merged": merged,
            "duplicates": duplicates,
            "errors": errors,
        }

    def get_merge_batch(self, batch_id):
        batch = self.repository.get_merge_batch(batch_id)
        if not batch:
            raise NotFoundError("merge batch not found: " + batch_id)
        return batch

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
