from datetime import date
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    BatchMergeError,
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .repository import IdentityConflict
from .rules import (
    RuleEngine,
    case_close_blockers,
    compute_followup,
    parse_epi_date,
)

ONSET_EDIT_ROLES = ("admin", "clinician", "investigator")
BATCH_ROLES = ("admin", "investigator")


class DomainService:
    def __init__(self, repository, rules=None, clock=None, policies=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        # Injectable clock so follow-up status can be evaluated deterministically.
        self.clock = clock or date.today
        self.policies = dict(policies or {})
        # Test hook: raise on item_id to simulate a crash mid-batch.
        self._fail_on_item = None

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def _today(self):
        value = self.clock()
        return value if isinstance(value, date) else parse_epi_date(value)

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

        # Case closure has a cross-object precondition checked *after* role
        # and state validation: recompute every contact's follow-up window
        # against current case data and block while anyone is still inside
        # (or before, or un-computable).
        if entity["kind"] == "case" and action == "close":
            self._guard_case_close(actor, entity)

        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _guard_case_close(self, actor, case_entity):
        contacts = self.repository.contacts_for_case(case_entity["id"])
        self._recompute_contacts(case_entity, contacts, actor, reason="pre_close")
        blockers = case_close_blockers(
            case_entity["data"], contacts, self._today(), self.policies
        )
        if blockers:
            self.audit.record(
                case_entity["id"], actor, "close_blocked",
                case_entity["status"], case_entity["status"],
                {"blockers": blockers},
            )
            raise InvalidTransition(
                "case cannot be closed: %d contact(s) still within the "
                "follow-up window or awaiting review" % len(blockers)
            )

    def update_onset_date(self, actor, case_id, new_onset_date, expected_version=None):
        """Change a case's onset date and cascade the recalculation.

        Every contact's follow-up window/start/end/status is recomputed;
        rows that cannot be computed are flagged needs_review with reasons.
        """
        if actor.role not in ONSET_EDIT_ROLES:
            raise PermissionDenied("role %s may not change onset date" % actor.role)
        parsed = parse_epi_date(new_onset_date)
        if parsed is None:
            raise ValidationError("onset_date must be YYYY-MM-DD")

        case_entity = self.repository.get_entity(case_id)
        if not case_entity or case_entity["kind"] != "case":
            raise NotFoundError("case not found: " + case_id)
        if case_entity["status"] == "closed":
            raise InvalidTransition("a closed case cannot have its onset date changed")

        old_onset = case_entity["data"].get("onset_date")
        merged = dict(case_entity["data"])
        merged["onset_date"] = parsed.isoformat()
        expected = (
            int(expected_version)
            if expected_version is not None
            else case_entity["version"]
        )
        updated = self.repository.update_entity(
            case_id, expected, case_entity["status"], merged
        )
        self.audit.record(
            case_id, actor, "change_onset_date",
            case_entity["status"], case_entity["status"],
            {"old": old_onset, "new": parsed.isoformat()},
        )
        summary = self.recompute_case_followups(actor, case_id, reason="onset_changed")
        updated["followup_recompute"] = summary
        return updated

    def recompute_case_followups(self, actor, case_id, reason="manual"):
        """Recompute and persist follow-up fields for all contacts of a case."""
        case_entity = self.repository.get_entity(case_id)
        if not case_entity or case_entity["kind"] != "case":
            raise NotFoundError("case not found: " + case_id)
        contacts = self.repository.contacts_for_case(case_id)
        return self._recompute_contacts(case_entity, contacts, actor, reason=reason)

    def _recompute_contacts(self, case_entity, contacts, actor, reason):
        summary = {"updated": 0, "needs_review": 0, "items": []}
        for contact in contacts:
            computed = compute_followup(
                case_entity["data"], contact["data"], self._today(), self.policies
            )
            self.repository.write_followup_fields(contact["id"], computed)
            summary["updated"] += 1
            if computed["followup_status"] == "needs_review":
                summary["needs_review"] += 1
            summary["items"].append({
                "contact_id": contact["id"],
                **computed,
            })
            self.audit.record(
                contact["id"], actor, "recompute_followup",
                contact["status"], contact["status"],
                {"reason": reason, "computed": computed},
            )
        return summary

    def consistency_report(self, case_id):
        """Compare stored follow-up fields with what the rules yield now.

        This is the 'two sides must agree' check: it surfaces any contact
        whose stored window/status has drifted from the source data
        (e.g. onset changed while a merge was in flight).
        """
        case_entity = self.repository.get_entity(case_id)
        if not case_entity or case_entity["kind"] != "case":
            raise NotFoundError("case not found: " + case_id)
        contacts = self.repository.contacts_for_case(case_id)
        drift = []
        for contact in contacts:
            computed = compute_followup(
                case_entity["data"], contact["data"], self._today(), self.policies
            )
            stored = contact["data"]
            mismatches = []
            for field in ("followup_start", "followup_end", "followup_status"):
                if stored.get(field) != computed[field]:
                    mismatches.append({
                        "field": field,
                        "stored": stored.get(field),
                        "expected": computed[field],
                    })
            if mismatches:
                drift.append({"contact_id": contact["id"], "mismatches": mismatches})
        blockers = case_close_blockers(
            case_entity["data"], contacts, self._today(), self.policies
        )
        stored_closed = case_entity["status"] == "closed"
        return {
            "case_id": case_id,
            "case_status": case_entity["status"],
            "contact_count": len(contacts),
            "drift": drift,
            "close_blockers": blockers,
            "consistent": not drift
            and not (stored_closed and blockers),
        }

    # ------------------------------------------------------------------
    # Offline batch merge (断网登记批次回到网内合并)
    # ------------------------------------------------------------------

    def fail_next_batch_item(self, item_id):
        self._fail_on_item = item_id

    def merge_batch(self, actor, payload):
        """Merge one offline batch of newly registered contacts.

        Idempotent by batch_id: re-submitting the identical batch after a
        mid-way failure resumes from the first un-applied item. Already
        applied items and rejected items are skipped, so a person is never
        registered twice.
        """
        if actor.role not in BATCH_ROLES:
            raise PermissionDenied("role %s may not merge batches" % actor.role)

        batch_id = payload.get("batch_id")
        if not batch_id:
            raise ValidationError("batch_id is required")
        items = payload.get("items") or []
        if not isinstance(items, list) or not items:
            raise ValidationError("items must be a non-empty list")

        # The batch belongs to one case of one transmission chain.
        case_id = payload.get("case_id")
        case_entity = self.repository.get_entity(case_id) if case_id else None
        if not case_entity or case_entity["kind"] != "case":
            raise NotFoundError("case not found: " + str(case_id))
        chain_id = payload.get("chain_id") or case_entity["data"].get("chain_id")
        if chain_id and case_entity["data"].get("chain_id") \
                and case_entity["data"]["chain_id"] != chain_id:
            raise ConflictError(
                "batch chain %s does not match case chain %s"
                % (chain_id, case_entity["data"]["chain_id"])
            )
        if case_entity["status"] == "closed":
            raise ConflictError("case %s is closed; new contacts cannot be merged" % case_id)

        header = self.repository.ensure_batch(
            batch_id,
            case_id,
            chain_id,
            payload.get("team_id"),
            payload.get("township"),
            actor.user_id,
        )
        if header["case_id"] != case_id:
            raise ConflictError(
                "batch %s already belongs to case %s" % (batch_id, header["case_id"])
            )

        # Normalize items: item_id defaults to the row index inside the batch,
        # so replaying byte-identical payloads always hits the same ledger rows.
        normalized = []
        for index, raw in enumerate(items):
            item = dict(raw)
            item.setdefault("item_id", str(index))
            item["case_id"] = case_id
            item["chain_id"] = chain_id
            item.setdefault("team_id", payload.get("team_id"))
            item.setdefault("township", payload.get("township"))
            normalized.append(item)

        ledger = {row["item_id"]: row for row in self.repository.list_batch_items(batch_id)}
        report = {"batch_id": batch_id, "applied": [], "rejected": [], "failed": []}
        changed_contact_ids = set()

        for item in normalized:
            item_id = item["item_id"]
            prior = ledger.get(item_id)
            if prior and prior["status"] in ("applied", "rejected"):
                report[prior["status"]].append({
                    "item_id": item_id,
                    "person_id": prior.get("person_id"),
                    "contact_entity_id": prior.get("contact_entity_id"),
                    "reason": prior.get("reason"),
                    "skipped": True,
                })
                continue

            # Permanent validation failures are recorded and never retried.
            problem = self._permanent_problem(item)
            if problem:
                self.repository.reject_batch_item(batch_id, item_id, problem)
                report["rejected"].append({"item_id": item_id, "reason": problem})
                self.audit.record(
                    case_id, actor, "batch_item_rejected",
                    case_entity["status"], case_entity["status"],
                    {"batch_id": batch_id, "item_id": item_id, "reason": problem},
                )
                continue

            try:
                if self._fail_on_item == item_id:
                    self._fail_on_item = None
                    raise RuntimeError("simulated storage failure")
                person_id, contact_entity_id, created, widened = \
                    self.repository.merge_contact_item(batch_id, item)
            except IdentityConflict as exc:
                reason = "identity_conflict: " + str(exc)
                self.repository.reject_batch_item(batch_id, item_id, reason)
                report["rejected"].append({"item_id": item_id, "reason": reason})
                continue
            except Exception as exc:
                # Transient: nothing was applied for this item; it stays pending.
                self.repository.mark_batch_item_failed(batch_id, item_id, str(exc))
                report["failed"].append({"item_id": item_id, "reason": str(exc)})
                self.repository.mark_batch_status(batch_id, "failed")
                self.audit.record(
                    case_id, actor, "batch_merge_failed",
                    case_entity["status"], case_entity["status"],
                    {"batch_id": batch_id, "item_id": item_id, "error": str(exc)},
                )
                raise BatchMergeError(
                    "batch %s stopped at item %s: %s" % (batch_id, item_id, exc),
                    report=report,
                )

            if created or widened:
                changed_contact_ids.add(contact_entity_id)
            report["applied"].append({
                "item_id": item_id,
                "person_id": person_id,
                "contact_entity_id": contact_entity_id,
                "created": created,
                "widened": widened,
            })
            self.audit.record(
                contact_entity_id, actor, "batch_item_applied",
                None, "identified",
                {"batch_id": batch_id, "item_id": item_id, "person_id": person_id},
            )

        # New contacts or widened exposure windows need follow-up windows now.
        if changed_contact_ids:
            contacts = [
                entity
                for entity in self.repository.contacts_for_case(case_id)
                if entity["id"] in changed_contact_ids
            ]
            self._recompute_contacts(case_entity, contacts, actor, reason="batch_merged")

        final_status = "partial" if report["rejected"] else "applied"
        self.repository.mark_batch_status(batch_id, final_status)
        report["status"] = final_status
        return report

    @staticmethod
    def _permanent_problem(item):
        has_identity = any(item.get(field) for field in ("id_card", "phone", "person_id"))
        if not has_identity:
            return "missing identity: one of id_card, phone or person_id is required"
        if not item.get("exposure_start"):
            return "missing exposure_start"
        if parse_epi_date(item.get("exposure_start")) is None:
            return "invalid exposure_start (use YYYY-MM-DD)"
        if item.get("exposure_end") and parse_epi_date(item["exposure_end"]) is None:
            return "invalid exposure_end (use YYYY-MM-DD)"
        return None

    def get_batch_status(self, batch_id):
        header = self.repository.get_batch(batch_id)
        if not header:
            raise NotFoundError("batch not found: " + batch_id)
        items = self.repository.list_batch_items(batch_id)
        return {"batch": header, "items": items}

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def list_contacts(self, case_id):
        return self.repository.contacts_for_case(case_id)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
