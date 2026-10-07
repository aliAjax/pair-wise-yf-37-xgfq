from datetime import date, datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)

# ---------------------------------------------------------------------------
# Follow-up window rules (接触者随访窗口)
#
# A close contact's follow-up window starts at the later of the case onset
# date and the contact's last exposure date, and runs for an observation
# period (default 14 days, configurable per disease). Day one is the window
# start, so the window end is start + (observation_days - 1).
#
# Statuses:
#   PENDING     - before the window starts
#   ACTIVE      - inside the window (window is inclusive on both ends)
#   COMPLETED   - after the window ends
#   NEEDS_REVIEW- window cannot be computed from the data on record; the
#                 contact is flagged for a human epidemiologist to review
# ---------------------------------------------------------------------------

FOLLOWUP_PENDING = "pending"
FOLLOWUP_ACTIVE = "active"
FOLLOWUP_COMPLETED = "completed"
FOLLOWUP_NEEDS_REVIEW = "needs_review"

FOLLOWUP_STATUSES = (
    FOLLOWUP_PENDING,
    FOLLOWUP_ACTIVE,
    FOLLOWUP_COMPLETED,
    FOLLOWUP_NEEDS_REVIEW,
)

DEFAULT_OBSERVATION_DAYS = 14
# Contacts still in the window, not yet inside it, or un-computable all block
# case closure. Only a clean "completed" set lets the case close.
CLOSE_BLOCKING_STATUSES = (
    FOLLOWUP_ACTIVE,
    FOLLOWUP_PENDING,
    FOLLOWUP_NEEDS_REVIEW,
)


def parse_epi_date(value):
    """Parse an ISO date (YYYY-MM-DD); return None for blank/garbage input."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def _validate_case(actor, data, lookup):
    rows = lookup("case", "person_id", data.get("person_id")) or [] if lookup else []
    for row in rows:
        if row["data"].get("onset_date") == data.get("onset_date"):
            raise ConflictError("duplicate case for person and onset date")
    if not data.get("symptoms"):
        raise ValidationError("symptoms are required")


def _validate_lab_positive(actor, entity, data, lookup):
    if data.get("result", "").lower() not in ("positive", "detected"):
        raise ValidationError("lab result must be positive or detected")
    return {"confirmed_by": actor.user_id}


def _validate_probable(actor, entity, data, lookup):
    if not data.get("epi_link"):
        raise ValidationError("probable case requires an epidemiological link")


def cluster_cases(cases, max_days=14):
    groups = []
    for case in sorted(cases, key=lambda item: str(item.get("onset_date", ""))):
        placed = False
        for group in groups:
            same_location = group["location"] == case.get("location")
            delta = abs(_date_ordinal(group["onset_date"]) - _date_ordinal(case.get("onset_date")))
            if same_location and delta <= max_days:
                group["members"].append(case.get("id"))
                placed = True
                break
        if not placed:
            groups.append({"location": case.get("location"), "onset_date": case.get("onset_date"), "members": [case.get("id")]})
    return [group for group in groups if len(group["members"]) > 1]


CUSTOM_CREATE = {'case': _validate_case}
CUSTOM_TRANSITIONS = {('case', 'lab_positive'): _validate_lab_positive, ('case', 'mark_probable'): _validate_probable}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified'}
    TRANSITIONS = {'case': {'triage': (('reported',), 'investigating'), 'lab_positive': (('investigating',), 'confirmed'), 'mark_probable': (('investigating',), 'probable'), 'recover': (('confirmed', 'probable'), 'recovered'), 'close': (('recovered',), 'closed')}, 'contact': {'begin_followup': (('identified',), 'following'), 'complete_followup': (('following',), 'completed')}}
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start')}
    ACTION_REQUIRED = {('case', 'triage'): ('clinician',), ('case', 'lab_positive'): ('lab_id', 'result'), ('case', 'mark_probable'): ('epi_link',), ('case', 'recover'): ('recovered_at',), ('case', 'close'): ('outcome',), ('contact', 'begin_followup'): ('followup_start', 'due_at'), ('contact', 'complete_followup'): ('outcome',)}
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {'triage': ('admin', 'clinician'), 'lab_positive': ('admin', 'lab'), 'mark_probable': ('admin', 'investigator'), 'recover': ('admin', 'clinician'), 'close': ('admin', 'investigator'), 'begin_followup': ('admin', 'investigator'), 'complete_followup': ('admin', 'investigator')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def observation_days_for(case_data, policies=None):
    """Observation period length: disease policy first, then case override, then default."""
    if policies:
        disease = (case_data or {}).get("disease")
        if disease and disease in policies:
            return int(policies[disease])
    override = (case_data or {}).get("observation_days")
    if override:
        return int(override)
    return DEFAULT_OBSERVATION_DAYS


def compute_followup(case_data, contact_data, as_of, policies=None):
    """Compute a contact's follow-up window and status from source data.

    Returns a dict with followup_start/followup_end (ISO strings or None),
    followup_status and review_reasons (list, non-empty only for needs_review).
    Pure function: raises nothing, marks un-computable inputs for review.
    """
    reasons = []
    onset = parse_epi_date((case_data or {}).get("onset_date"))
    if onset is None:
        reasons.append("missing_or_invalid_case_onset_date")

    exposure_start = parse_epi_date((contact_data or {}).get("exposure_start"))
    exposure_end = parse_epi_date((contact_data or {}).get("exposure_end"))
    if exposure_start is None:
        reasons.append("missing_or_invalid_exposure_date")
    last_exposure = exposure_end or exposure_start

    days = observation_days_for(case_data, policies)
    if days <= 0:
        reasons.append("invalid_observation_days")

    if reasons:
        return {
            "followup_start": None,
            "followup_end": None,
            "followup_status": FOLLOWUP_NEEDS_REVIEW,
            "review_reasons": reasons,
        }

    window_start = max(onset, last_exposure)
    window_end = window_start + timedelta(days=days - 1)
    today = as_of if isinstance(as_of, date) else parse_epi_date(as_of)

    if today is None:
        status = FOLLOWUP_NEEDS_REVIEW
        reasons.append("missing_reference_date")
    elif today < window_start:
        status = FOLLOWUP_PENDING
    elif today <= window_end:
        status = FOLLOWUP_ACTIVE
    else:
        status = FOLLOWUP_COMPLETED

    return {
        "followup_start": window_start.isoformat(),
        "followup_end": window_end.isoformat(),
        "followup_status": status,
        "review_reasons": reasons,
    }


def case_close_blockers(case_data, contacts, as_of, policies=None):
    """Return contacts that prevent closing the case.

    Every contact is recomputed against the current case data (never trusting
    stored follow-up fields). The case and the contacts therefore cannot tell
    different stories: closure and contact windows always agree.
    """
    blockers = []
    for contact in contacts:
        result = compute_followup(case_data, contact["data"], as_of, policies)
        if result["followup_status"] in CLOSE_BLOCKING_STATUSES:
            blockers.append({
                "contact_id": contact["id"],
                "person_id": contact["data"].get("person_id"),
                "followup_status": result["followup_status"],
                "followup_end": result["followup_end"],
                "review_reasons": result["review_reasons"],
            })
    return blockers


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    parsed = parse_epi_date(value)
    if parsed is None:
        raise ValueError("invalid date: %r" % value)
    return parsed.toordinal()
