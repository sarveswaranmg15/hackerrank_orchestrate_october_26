"""Service logs in key=value, RFC5424 syslog and JSON-per-line formats.

Lines sharing a trace_id are grouped: the *_started line carries the lookup key (an email, hash or user id)
and the *_completed line carries the resource id and result.
"""
import json
import re

from .. import norm
from ..observations import interpret
from . import ExtractResult

KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
SYSLOG_RE = re.compile(r"^<\d+>\d+\s+(\S+)\s+(\S+)\s+(\S+)\s+\S+\s+\S+\s*(.*)$")
BRACKET_RE = re.compile(r"^\[(\w+)\]\s+\[([\w-]+)\]\s+\[([\w-]+)\]\s+(\w+):\s*(.*)$")
PIPE_RE = re.compile(r"^(\w+)\s*\|\s*([\w-]+)\s*\|\s*([\w-]+)\s*\|\s*(\w+)\s*\|\s*(.*)$")
LOOKUP_KEYS = ("lookup_key", "match_key", "subject_key", "scan_key", "owner_key", "subject_ref", "query")
SERVICE_SYSTEM = {"support_replica": "support_platform", "attachment_preview": "support_platform"}
# Events that report state for a resource.
STATE_EVENTS = {
    "profile_lookup_completed", "index_query_completed", "analytics_profile_read_completed",
    "analytics_profile_read", "analytics_identity_scan", "contact_lookup_completed",
    "restore_candidate_evaluated", "restore_policy_read_completed", "restore_authorization_completed",
    "connector_config_read_completed", "connector_config_loaded", "connector_heartbeat",
    "export_download_check_completed", "export_metadata_read_completed", "message_pii_scan",
    "attachment_pii_scan", "message_read", "support_ticket_read_completed", "audit_record_read_completed",
    "contact_query_completed", "deletion_marker_check", "record_lookup",
}
# Detector events report that an identifier occurs inside a record; they do not attribute the record to anyone.
IDENTIFIER_SCAN_EVENTS = {"attachment_pii_scan", "message_pii_scan", "analytics_identity_scan"}
EVENT_CATEGORY = {
    "attachment_pii_scan": "SUPPORT_ATTACHMENT",
    # Contact/marketing-style lookups keyed by a hashed or legacy identifier are secondary identifiers.
    "contact_lookup_completed": "SECONDARY_IDENTIFIER",
    "contact_query_completed": "SECONDARY_IDENTIFIER",
    "deletion_marker_check": "SECONDARY_IDENTIFIER",
}


def parse_line(line):
    line = line.strip()
    if not line:
        return None
    ts, _, rest = line.partition(" ")
    rest = rest.strip()
    if rest.startswith("|"):
        rest = rest[1:].strip()
    if rest.startswith("{"):
        try:
            fields = json.loads(rest)
        except ValueError:
            return None
        fields.setdefault("service", fields.get("logger"))
    elif rest.startswith("<"):
        m = SYSLOG_RE.match(rest)
        if not m:
            return None
        fields = _kv(m.group(4))
        fields["service"] = m.group(2)
        if m.group(3).startswith(("tr-", "rb-")):
            fields["trace_id"] = m.group(3)
    elif BRACKET_RE.match(rest):
        level, service, trace, event, kv = BRACKET_RE.match(rest).groups()
        fields = _kv(kv)
        fields.update(level=level, service=service, trace_id=trace, event=event)
    elif PIPE_RE.match(rest):
        level, service, trace, event, kv = PIPE_RE.match(rest).groups()
        fields = _kv(kv)
        fields.update(level=level, service=service, trace_id=trace, event=event)
    else:
        fields = _kv(rest)
        if "message" in fields and "function" in fields:  # cloud runtime wrapper with nested key=value message
            inner = _kv(fields.pop("message").replace('\\"', '"'))
            fields.update(inner)
            fields["service"] = fields.pop("function")
            fields["trace_id"] = fields.pop("requestId", None)
    fields["_ts"] = ts
    return fields


def _kv(text):
    out = {}
    for k, v in KV_RE.findall(text):
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1]
        out[k] = v
    return out


def extract(path, meta):
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    res = ExtractResult(text="\n".join(lines))
    parsed = [p for p in (parse_line(l) for l in lines) if p]
    traces = {}
    for p in parsed:
        tid = p.get("trace_id")
        if tid:
            traces.setdefault(tid, []).append(p)
    for p in parsed:
        event = p.get("event")
        if event not in STATE_EVENTS or not p.get("resource_id"):
            continue
        lookup = [p.get(k) for k in LOOKUP_KEYS if p.get(k)]
        if p.get("trace_id"):
            for q in traces[p["trace_id"]]:
                lookup += [q.get(k) for k in LOOKUP_KEYS if q.get(k)]
        row = {k: v for k, v in p.items() if not k.startswith("_") and k not in (
            "service", "logger", "instance", "pod", "region", "trace_id", "event", "severity", "level", "seq",
            "attempt", "duration_ms", "took_ms", "latency_ms", "shard", "index", "file", "detector") + LOOKUP_KEYS}
        if "correlation_key" in row:
            row["correlation_key"] = row["correlation_key"]
        service = p.get("service") or ""
        system = p.get("system") or norm.system_for(p["resource_id"]) or SERVICE_SYSTEM.get(service) or service
        obs = interpret(row, evidence_id=meta["file_id"], captured_at=meta["captured_at"], observed_at=p["_ts"],
                        system=system, resource_id=p["resource_id"], category=EVENT_CATEGORY.get(event),
                        lookup=lookup, detail=f"{event} {json.dumps(row)[:200]}")
        if obs:
            obs.identifier_scan = event in IDENTIFIER_SCAN_EVENTS
            if event == "contact_lookup_completed" and obs.exposed is None:
                obs.exposed = bool(obs.ids)
            res.observations.append(obs)
    return res
