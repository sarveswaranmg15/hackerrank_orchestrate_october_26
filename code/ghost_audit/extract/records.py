"""JSON record exports, snapshot bundles and plain-text handover reports."""
import json
import re

from .. import norm
from ..observations import COLLECTION_CATEGORY, ScopeSnapshot, interpret
from . import ExtractResult

SOURCE_SYSTEM = {"user_export_store": "export_service", "analytics_identity_store": "analytics_platform"}
HANDOVER_RE = re.compile(r"^-\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|\s*(.+)$")


def _emit(res, rec, meta, observed, system=None, category=None, lookup=()):
    obs = interpret(rec, evidence_id=meta["file_id"], captured_at=meta["captured_at"], observed_at=observed,
                    system=system, category=category, lookup=lookup)
    if obs:
        res.observations.append(obs)
    return obs


def extract_json(path, meta):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    res = ExtractResult(text=json.dumps(data))
    top_time = data.get("exported_at") or data.get("captured_at") or meta["captured_at"]
    if "service_exports" in data:
        for system, colls in data["service_exports"].items():
            for coll, recs in colls.items():
                for rec in recs:
                    lookup = [rec.get("query")] if rec.get("query") else []
                    obs = _emit(res, rec, meta, rec.get("observed_at") or top_time, system=system, lookup=lookup)
                    if obs and obs.present is None:
                        obs.present = True
    elif "snapshots" in data:
        for snap in data["snapshots"]:
            system, t = snap["data_store"], snap.get("captured_at") or top_time
            rids, history = set(), False
            for rec in snap["records"]:
                if rec.get("system"):  # history rows name their own system and time; not a collection scope
                    history = True
                    lookup = [rec["lookup_value"]] if rec.get("lookup_value") else []
                    _emit(res, rec, meta, rec.get("recorded_at") or t, system=rec["system"], lookup=lookup)
                    continue
                obs = _emit(res, rec, meta, t, system=system, category=COLLECTION_CATEGORY.get(snap.get("collection")))
                if obs:
                    if obs.present is None:
                        obs.present = True
                    rids.add(obs.resource_id)
            if not history:
                res.scopes.append(ScopeSnapshot(meta["file_id"], meta["captured_at"], t, system,
                                                snap.get("collection", ""), frozenset(rids)))
    elif "records" in data:
        system = SOURCE_SYSTEM.get(data.get("source"))
        for rec in data["records"]:
            t = rec.get("recorded_at") or data.get("captured_at") or top_time
            obs = _emit(res, rec, meta, t, system=rec.get("system") or system)
            if obs and obs.present is None:
                obs.present = True
    else:
        res.unread = "unrecognized JSON layout"
    return res


def extract_txt(path, meta):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    res = ExtractResult(text=text)
    m = re.search(r"Snapshot:\s*(\S+)", text)
    default_t = m.group(1) if m else meta["captured_at"]
    for line in text.splitlines():
        hm = HANDOVER_RE.match(line.strip())
        if not hm:
            continue
        t, system, rid, rtype, result, subject = hm.groups()
        rec = {"resource_id": rid, "system": system, "record_type": rtype, "result": result,
               "subject_reference": subject.strip()}
        _emit(res, rec, meta, t or default_t)
    if not res.observations and norm.resources_in_text(text):
        res.unread = "text mentions resources but no structured rows were parsed"
    return res
