"""Normalized evidence observations and the shared row interpreter used by every extractor."""
from dataclasses import dataclass, field

from . import norm

DEFAULT_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "analytics_platform": "ANALYTICS_PROFILE",
    "support_platform": "SUPPORT_MESSAGE",
    "export_service": "USER_EXPORT",
    "integration_service": "DATA_FLOW",
    "backup_service": "BACKUP_ARCHIVE",
    "security_service": "SECURITY_AUDIT_LOG",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "legal_service": "LEGAL_HOLD_RECORD",
    "marketing_platform": "MARKETING_PROFILE",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
    "cache_service": "CACHE_ENTRY",
    "object_storage": "SUPPORT_ATTACHMENT",
}

# Fields holding direct identifiers. Their presence (non-null) means the record still exposes the person.
DIRECT_FIELDS = {
    "email", "phone", "email_hash", "full_name", "name", "display_name", "requester_name", "requester_email",
    "requester_phone", "owner_email", "owner_phone", "customer_email", "matched_value",
}
# Stable references: allowed on tombstones/retained records, but they keep analytics attributable.
REFERENCE_FIELDS = {"user_id", "customer_reference", "subject_reference", "subject_selector", "subject_ref"}
UPLOADER_FIELDS = {"uploaded_by", "uploader"}
# Collections whose records belong to a more specific category than their data store's default (bundles and SQL
# record_json rows use the same collection names).
COLLECTION_CATEGORY = {"ticket_attachments": "SUPPORT_ATTACHMENT"}
TRUE = {"true", "1", "yes", "y"}
FALSE = {"false", "0", "no", "n"}


@dataclass
class Observation:
    evidence_id: str
    captured_at: str            # capture time of the evidence file (manifest)
    observed_at: str            # time of this specific row/line
    system: str
    resource_id: str
    category: str
    present: object = None      # True / False / None (unknown)
    exposed: object = None      # direct identifiers still visible
    flow_active: object = None  # data flow enabled
    restore_allowed: object = None
    ids: frozenset = frozenset()      # identifiers inside the record itself
    lookup: frozenset = frozenset()   # keys used to look the record up
    refs: frozenset = frozenset()     # other resource ids referenced by the record
    detail: str = ""
    source_kind: str = "structured"   # "structured" | "pdf_text" | "screenshot_transcript"
    needs_review: str = ""            # non-empty: ambiguous claim, not used until a human confirms the image
    review_scope: str = ""            # "ownership": excluded from linking; "state": the ambiguous state is not set
    identifier_scan: bool = False     # a PII scan matched an identifier inside the record (not record ownership)
    exclusive_claim: bool = False     # evidence states the record belongs only to one subject

    @property
    def cutoff_time(self):
        return max(self.captured_at, self.observed_at or "")


@dataclass
class ScopeSnapshot:
    """A capture that enumerates a whole collection: resources it does not list were absent."""
    evidence_id: str
    captured_at: str
    observed_at: str
    system: str
    collection: str
    resource_ids: frozenset = field(default_factory=frozenset)
    exhaustive: bool = False    # True for full-table SQL dumps; bundles only cover the subjects they mention


def as_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in TRUE:
            return True
        if s in FALSE:
            return False
    return None


def flatten(row, prefix=""):
    """Flatten nested dicts/lists of dicts into one dict (later keys win only if earlier are null)."""
    out = {}
    for k, v in row.items():
        if isinstance(v, dict):
            for k2, v2 in flatten(v).items():
                if k2 not in out or norm.is_null(out[k2]):
                    out[k2] = v2
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            for x in v:
                for k2, v2 in flatten(x).items():
                    if k2 not in out or norm.is_null(out[k2]):
                        out[k2] = v2
        else:
            if k not in out or norm.is_null(out[k]):
                out[k] = v
    return out


def _text_values(row):
    for v in row.values():
        if isinstance(v, str):
            yield v
        elif isinstance(v, (list, tuple)):
            for x in v:
                if isinstance(x, str):
                    yield x


RESULT_STATES = {
    "FOUND": (True, None), "NOT_FOUND": (False, None), "IDENTIFIABLE": (True, True),
    "ANONYMIZED": (True, False), "REDACTED": (True, False), "ACTIVE": (True, None),
    "DISABLED": (True, None), "BLOCKED": (True, None),
}


def interpret(row, *, evidence_id, captured_at, observed_at, system=None, resource_id=None,
              category=None, lookup=(), detail=""):
    """Turn one evidence row (dict of fields) into an Observation, or None if it names no resource."""
    raw = dict(row)
    row = flatten(row)
    resource_id = resource_id or row.get("resource_id")
    if not resource_id or not isinstance(resource_id, str):
        return None
    system = system or row.get("system") or norm.system_for(resource_id)
    if not system:
        return None
    category = (category or row.get("record_type") or DEFAULT_CATEGORY.get(system) or "UNKNOWN").upper()
    lower = {str(k).lower(): v for k, v in row.items()}

    present = exposed = flow_active = restore_allowed = None
    result = lower.get("result")
    if isinstance(result, str):
        r = result.strip().upper()
        if r in RESULT_STATES:
            present, exposed = RESULT_STATES[r]
            if category == "DATA_FLOW" and r in ("ACTIVE", "DISABLED"):
                flow_active = r == "ACTIVE"
            if category == "BACKUP_ARCHIVE" and r in ("ACTIVE", "BLOCKED"):
                restore_allowed = r == "ACTIVE"
        elif r in ("HIT", "MISS"):
            present = r == "HIT"
    failed_lookup = False
    for key in ("http_status", "status_code"):
        if key in lower and not norm.is_null(lower[key]):
            code = str(lower[key]).strip()
            if code.startswith("2"):
                present = True
            elif code in ("404", "410"):
                present = False  # a completed read that found no record
            else:
                failed_lookup = True  # 5xx, 403, timeouts: a failed lookup proves nothing
    if "rows" in lower and present is None and not failed_lookup:
        try:
            present = int(lower["rows"]) > 0
        except (TypeError, ValueError):
            pass
    if present is None and isinstance(lower.get("total"), (int, str)) and str(lower.get("total")).isdigit():
        present = int(lower["total"]) > 0
    for key in ("available", "download_available", "archive_present"):
        b = as_bool(lower.get(key))
        if b is not None and present is None:
            present = True  # the record exists; availability is a property of it
    if as_bool(lower.get("marker_found")) is False:
        present = True
    if lower.get("profile", "missing") is None and present is None:
        present = False

    if exposed is None:
        if "direct_identifiers" in lower:
            try:
                exposed = int(lower["direct_identifiers"]) > 0
            except (TypeError, ValueError):
                pass
        if exposed is None:
            fields = [k for k in lower if k in DIRECT_FIELDS]
            # Uploader fields expose a person only when they hold a person identifier (an email or phone), not a
            # service name such as "support-service". Whose identifier it is gets decided by linking, not here.
            fields += [k for k in lower if k in UPLOADER_FIELDS and isinstance(lower[k], str)
                       and any("@" in i or i.startswith("tel:") for i in norm.identifiers_in_text(lower[k]))]
            if category == "ANALYTICS_PROFILE":
                fields += [k for k in lower if k in ("user_id", "customer_reference")]
            if "message_text" in lower and isinstance(lower["message_text"], str):
                txt = lower["message_text"]
                msg_exposed = "[REDACTED]" not in txt and bool(norm.identifiers_in_text(txt))
                exposed = msg_exposed or any(not norm.is_null(lower[k]) for k in fields)
            elif fields:
                exposed = any(not norm.is_null(lower[k]) for k in fields)
        if exposed is not None and present is None:
            present = True  # the record's identifier fields were read (exposed or redacted): it exists

    for key in ("enabled", "scheduler_registered"):
        b = as_bool(lower.get(key))
        if b is not None:
            flow_active = b if key == "enabled" else (flow_active if flow_active is not None else b)
    if "restore_allowed" in lower:
        restore_allowed = as_bool(lower["restore_allowed"])
    if "eligible" in lower:
        eligible, blocked = as_bool(lower["eligible"]), as_bool(lower.get("deletion_block"))
        restore_allowed = bool(eligible) and not bool(blocked)
    if isinstance(lower.get("decision"), str):
        restore_allowed = lower["decision"].strip().lower() not in ("deny", "denied", "blocked")
    if (flow_active is not None or restore_allowed is not None) and present is None:
        present = True

    texts = list(_text_values(row))
    ids = set()
    for t in texts:
        ids |= norm.identifiers_in_text(t)
    refs = set()
    for t in texts:
        refs |= norm.resources_in_text(t)
    refs.discard(resource_id)
    lookup_ids = set()
    for k in lookup:
        if not norm.is_null(k):
            lookup_ids |= norm.identifiers_in_text(str(k)) or {str(k).strip().lower()}
    return Observation(
        evidence_id=evidence_id, captured_at=captured_at, observed_at=observed_at or captured_at,
        system=system, resource_id=resource_id, category=category, present=present, exposed=exposed,
        flow_active=flow_active, restore_allowed=restore_allowed, ids=frozenset(ids),
        lookup=frozenset(lookup_ids), refs=frozenset(refs), detail=detail or str(raw)[:240])
