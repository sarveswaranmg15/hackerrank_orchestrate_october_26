"""Identifier normalization and extraction shared by identity resolution and evidence parsing."""
import hashlib
import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
PHONE_RE = re.compile(r"\+\d[\d ()\-.]{6,}\d")
RESOURCE_RE = re.compile(
    r"\b(?:account|search|analytics|support|export|data_flow|backup|tombstone|audit_record|legal_hold|"
    r"marketing|email_delivery|cache|object)_\d+\b")

# Resource-id prefix -> owning system, used only when the evidence row does not name a system.
PREFIX_SYSTEM = [
    ("account_", "account_service"), ("search_", "search_service"), ("analytics_", "analytics_platform"),
    ("support_", "support_platform"), ("export_", "export_service"), ("data_flow_", "integration_service"),
    ("backup_", "backup_service"), ("tombstone_", "identity_service"), ("audit_record_", "security_service"),
    ("legal_hold_", "legal_service"), ("marketing_", "marketing_platform"),
    ("email_delivery_", "email_delivery_service"), ("cache_", "cache_service"), ("object_", "object_storage"),
]


def system_for(resource_id):
    for prefix, system in PREFIX_SYSTEM:
        if resource_id.startswith(prefix):
            return system
    return None


def email(v):
    return v.strip().lower()


def phone(v):
    digits = re.sub(r"\D", "", v)
    return "tel:" + digits if len(digits) >= 7 else None


def email_hash(addr):
    return hashlib.sha256(email(addr).encode("utf-8")).hexdigest()


def is_null(v):
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a"))


def identifiers_in_text(text):
    """All linkable identifiers mentioned in free text (emails, hashes, user ids, phones)."""
    out = set()
    if not text:
        return out
    out.update(email(m) for m in EMAIL_RE.findall(text))
    out.update(m.lower() for m in HASH_RE.findall(text.lower()))
    out.update(USER_RE.findall(text))
    for m in PHONE_RE.findall(text):
        p = phone(m)
        if p:
            out.add(p)
    return out


def resources_in_text(text):
    return set(RESOURCE_RE.findall(text or ""))
