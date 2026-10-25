"""SQLite-compatible snapshot exports: execute in memory, emit one observation per row and table scopes."""
import json
import re
import sqlite3

from .. import norm
from ..observations import COLLECTION_CATEGORY, ScopeSnapshot, interpret
from . import ExtractResult


def extract(path, meta):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    con = sqlite3.connect(":memory:")
    try:
        return _extract(con, text, meta)
    finally:
        con.close()


def _extract(con, text, meta):
    con.executescript(text)
    m = re.search(r"Captured at:\s*(\S+)", text)
    snap_time = m.group(1) if m else meta["captured_at"]
    scope_times = {}
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
    if "snapshot_scope" in tables:
        scope_times = dict(con.execute("select origin_system, captured_at from snapshot_scope"))
    res = ExtractResult(text=text)
    eid, cap = meta["file_id"], meta["captured_at"]
    for table in tables:
        if table == "snapshot_scope":
            continue
        cur = con.execute(f'select * from "{table}"')
        cols = [d[0] for d in cur.description]
        per_system = {}
        for values in cur.fetchall():
            row = dict(zip(cols, values))
            if "record_json" in row:
                rec = json.loads(row["record_json"])
                system = row.get("origin_system")
                collection = row.get("collection_name") or table
            else:
                rec, system, collection = row, None, table
            rid = rec.get("resource_id") or row.get("resource_id")
            system = system or norm.system_for(rid or "")
            if not rid or not system:
                continue
            observed = scope_times.get(system, snap_time)
            obs = interpret(rec, evidence_id=eid, captured_at=cap, observed_at=observed, system=system,
                            resource_id=rid, category=COLLECTION_CATEGORY.get(collection),
                            detail=f"{table}: {json.dumps(rec, default=str)[:200]}")
            if obs:
                if obs.present is None:
                    obs.present = True  # a row in a snapshot table exists at capture time
                res.observations.append(obs)
                per_system.setdefault((system, collection), set()).add(rid)
        if not per_system:
            # Empty table: infer which system it represents from its name.
            system = _table_system(table)
            if system:
                per_system[(system, table)] = set()
        for (system, collection), rids in per_system.items():
            res.scopes.append(ScopeSnapshot(eid, cap, scope_times.get(system, snap_time), system,
                                            f"{table}", frozenset(rids), exhaustive=True))
    return res


TABLE_HINTS = [("profile", "account_service"), ("search", "search_service"), ("analytics", "analytics_platform"),
               ("support", "support_platform"), ("export", "export_service"), ("connector", "integration_service"),
               ("backup", "backup_service"), ("security", "security_service")]


def _table_system(table):
    for hint, system in TABLE_HINTS:
        if hint in table:
            return system
    return None
