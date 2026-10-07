from datetime import datetime, timedelta, date

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# 最长潜伏期（天）：接触者的随访截止时间 = 病例发病日期 + 最长潜伏期
INCUBATION_DAYS = 14
# 接触者随访仍在进行的状态（未收尾）
CONTACT_ACTIVE_STATUSES = ("identified", "following", "overdue")
# 接触者已收尾的状态
CONTACT_TERMINAL_STATUSES = ("completed",)


def _parse_date(value):
    return datetime.fromisoformat(str(value)[:10]).date()


def _date_ordinal(value):
    return _parse_date(value).toordinal()


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


def _validate_update_onset(actor, entity, data, lookup):
    onset = data.get("onset_date")
    if not onset:
        raise ValidationError("onset_date is required")
    try:
        _parse_date(onset)
    except (ValueError, TypeError):
        raise ValidationError("invalid onset_date: %r" % (onset,))
    return {}


def _validate_close(actor, entity, data, lookup):
    """仍有接触者在随访窗口内或待复核时，病例不能收尾。"""
    contacts = lookup("contact", "case_id", entity["id"]) or []
    blockers = []
    for contact in contacts:
        cdata = contact.get("data", {}) or {}
        if cdata.get("needs_review"):
            blockers.append(contact)
            continue
        if contact["status"] in CONTACT_TERMINAL_STATUSES:
            continue
        if contact["status"] not in CONTACT_ACTIVE_STATUSES:
            continue
        due = cdata.get("due_at")
        if due is None:
            blockers.append(contact)
            continue
        try:
            due_date = _parse_date(due)
        except (ValueError, TypeError):
            blockers.append(contact)
            continue
        if due_date >= date.today():
            blockers.append(contact)
    if blockers:
        raise InvalidTransition(
            "cannot close case: %d contact(s) still in follow-up window or pending review"
            % len(blockers)
        )
    return {}


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


def compute_contact_followup(contact, case_onset_date, today=None):
    """根据病例发病日期重算接触者的随访截止时间与状态。

    返回 (patch, needs_review, review_reason)。算不出来的部分标记 needs_review，
    由服务层写入接触者记录等人复核。
    """
    today = today or date.today()
    data = contact.get("data", {}) or {}
    patch = {}
    needs_review = False
    reason = None
    try:
        onset = _parse_date(case_onset_date)
    except (ValueError, TypeError):
        return {}, True, "invalid case onset_date"
    due = onset + timedelta(days=INCUBATION_DAYS)
    patch["due_at"] = due.isoformat()
    exposure = data.get("exposure_start")
    if not exposure:
        needs_review = True
        reason = "missing exposure_start"
    else:
        try:
            exp = _parse_date(exposure)
            if due <= exp:
                needs_review = True
                reason = "follow-up deadline is not after exposure"
        except (ValueError, TypeError):
            needs_review = True
            reason = "invalid exposure_start"
    status = contact.get("status")
    if status == "completed":
        # 随访已完成但发病日期后移、窗口重新打开，需要人工复核
        if due > today:
            needs_review = True
            reason = reason or "onset moved after follow-up was completed"
    elif status == "identified":
        # 尚未开始随访，只更新截止时间，状态保持 identified
        pass
    else:
        patch["status"] = "overdue" if due < today else "following"
    return patch, needs_review, reason


CUSTOM_CREATE = {'case': _validate_case}
CUSTOM_TRANSITIONS = {
    ('case', 'lab_positive'): _validate_lab_positive,
    ('case', 'mark_probable'): _validate_probable,
    ('case', 'update_onset'): _validate_update_onset,
    ('case', 'close'): _validate_close,
}


class RuleEngine:
    ALIASES = {'cases': 'case', 'contacts': 'contact'}
    INITIAL_STATUS = {'case': 'reported', 'contact': 'identified'}
    TRANSITIONS = {
        'case': {
            'triage': (('reported',), 'investigating'),
            'lab_positive': (('investigating',), 'confirmed'),
            'mark_probable': (('investigating',), 'probable'),
            'recover': (('confirmed', 'probable'), 'recovered'),
            'close': (('recovered',), 'closed'),
            'update_onset': (('reported', 'investigating', 'confirmed', 'probable', 'recovered'), None),
        },
        'contact': {
            'begin_followup': (('identified',), 'following'),
            'complete_followup': (('following', 'overdue'), 'completed'),
        },
    }
    CREATE_REQUIRED = {'case': ('person_id', 'onset_date', 'location', 'symptoms'), 'contact': ('case_id', 'person_id', 'exposure_start')}
    ACTION_REQUIRED = {
        ('case', 'triage'): ('clinician',),
        ('case', 'lab_positive'): ('lab_id', 'result'),
        ('case', 'mark_probable'): ('epi_link',),
        ('case', 'recover'): ('recovered_at',),
        ('case', 'close'): ('outcome',),
        ('case', 'update_onset'): ('onset_date',),
        ('contact', 'begin_followup'): ('followup_start', 'due_at'),
        ('contact', 'complete_followup'): ('outcome',),
    }
    CREATE_ROLES = {'case': ('admin', 'clinician'), 'contact': ('admin', 'investigator')}
    ROLE_ACTIONS = {
        'triage': ('admin', 'clinician'),
        'lab_positive': ('admin', 'lab'),
        'mark_probable': ('admin', 'investigator'),
        'recover': ('admin', 'clinician'),
        'close': ('admin', 'investigator'),
        'update_onset': ('admin', 'clinician'),
        'begin_followup': ('admin', 'investigator'),
        'complete_followup': ('admin', 'investigator'),
    }

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
        # next_status 为 None 表示保持原状态（如 update_onset 只改数据不改状态）
        return (next_status if next_status is not None else entity["status"]), patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None
