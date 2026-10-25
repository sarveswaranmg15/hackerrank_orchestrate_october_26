"""Participant-created evaluation cases on a synthetic mini-dataset.

Every person, identifier and record below is fictional and was written for this test. Expected outputs are derived
by hand from the written contract (problem_statement.md / AGENTS.md section 6), not from the public samples. The
dataset is built in a temporary directory, run through the production pipeline, and scored with the same scorer
as the public samples.

Usage (from the repository root):  python3 code/evaluation/custom_cases.py
"""
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(CODE)
sys.path.insert(0, CODE)

from ghost_audit import contract  # noqa: E402
from ghost_audit.loaders import SCHEMAS, Dataset  # noqa: E402
from ghost_audit.pipeline import Auditor  # noqa: E402

REQUESTED, REPORTED, AUDIT = "2026-05-01T08:00:00Z", "2026-05-02T08:00:00Z", "2026-05-10T12:00:00Z"
DELETE_TEXT = "Please permanently delete the account associated with this email address."


def _scorer():
    """Load code/evaluation/main.py under a unique name (code/main.py is also a module called 'main')."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ghost_eval_scorer", os.path.join(HERE, "main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(email):
    return hashlib.sha256(email.encode()).hexdigest()


USERS = [  # user_id, name, current email, phone
    ("user_9001", "Ines Example", "ines.new@example.test", "+1 555 0100 001"),
    ("user_9002", "Omar Sample", "omar.sample@example.test", "+1 555 0100 002"),
    ("user_9003", "Dana Fixture", "dana@example.test", "+1 555 0100 003"),
    ("user_9004", "Dana Junior", "dana.jr@example.test", "+1 555 0100 004"),  # look-alike decoy owner
    ("user_9005", "Theo Case", "theo@example.test", "+1 555 0100 005"),
]
AUDIT_LOG = [("audit_9001", "user_9001", "2026-02-01T00:00:00Z", "email", "ines.old@example.test",
              "ines.new@example.test")]

CASES = [
    {"case_id": "custom_01", "email": "omar.sample@example.test",
     "text": "Please stop the promotional emails. I still use this account, so please don't delete anything.",
     "why": "Negated deletion plus another purpose: the request does not authorize deletion.",
     "expected": ("NOT_APPLICABLE", [])},
    {"case_id": "custom_02", "email": "ines.old@example.test", "text": DELETE_TEXT,
     "why": "Request uses a historical email (audit log). The account readback is a completed 404 in the request "
            "window; the search index still holds the old email. Historical identity must link both.",
     "expected": ("INCOMPLETE", [
         ("FINDING", "search_service", "search_9101", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_01"),
         ("REMEDIATION", "search_service", "search_9101", "DELETE_RECORD", "ABSENT", ""),
         ("VERIFICATION", "account_service", "account_9101", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
         ("VERIFICATION", "search_service", "search_9101", "VERIFY_RECORD_ABSENT", "ABSENT", "")])},
    {"case_id": "custom_03", "email": "theo@example.test", "text": DELETE_TEXT,
     "why": "Pre-audit evidence proves absence and a retained audit record. A failed (503) lookup must not link "
            "account_9502. A capture taken after audit_at shows the account present again, and it must be ignored.",
     "expected": ("COMPLETE", [
         ("VERIFICATION", "account_service", "account_9501", "VERIFY_RECORD_ABSENT", "ABSENT", "evidence_01"),
         ("VERIFICATION", "security_service", "audit_record_9501", "VERIFY_RECORD_PRESENT", "PRESENT",
          "evidence_01")])},
    {"case_id": "custom_04", "email": "dana@example.test", "text": DELETE_TEXT,
     "why": "An active connector and a restorable backup must be disabled and blocked before verification. A "
            "search hit found with Dana's lookup key but naming dana.jr@ (another person) must not be linked.",
     "expected": ("INCOMPLETE", [
         ("FINDING", "integration_service", "data_flow_9301", "REPORT_ACTIVE_DATA_FLOW", "ACTIVE", "evidence_01"),
         ("FINDING", "backup_service", "backup_9301", "REPORT_RESTORE_RISK", "ACTIVE", "evidence_01"),
         ("REMEDIATION", "integration_service", "data_flow_9301", "DISABLE_DATA_FLOW", "DISABLED", ""),
         ("REMEDIATION", "backup_service", "backup_9301", "BLOCK_RESTORE", "BLOCKED", ""),
         ("VERIFICATION", "account_service", "account_9301", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
         ("VERIFICATION", "integration_service", "data_flow_9301", "VERIFY_DATA_FLOW_DISABLED", "DISABLED", ""),
         ("VERIFICATION", "backup_service", "backup_9301", "VERIFY_RESTORE_BLOCKED", "BLOCKED", "")])},
]

LOG_LINES = [
    # custom_02: historical email readback (completed 404) and a remaining search entry
    '2026-05-09T09:00:00Z level=DEBUG service=account_service trace_id=tr-c2a event=profile_lookup_started '
    'lookup_key="ines.old@example.test"',
    '2026-05-09T09:00:01Z level=INFO service=account_service trace_id=tr-c2a resource_id=account_9101 '
    'event=profile_lookup_completed rows=0 http_status=404',
    '2026-05-09T09:05:00Z level=DEBUG service=search_service trace_id=tr-c2b event=index_query_started '
    'match_key="ines.old@example.test"',
    '2026-05-09T09:05:01Z level=INFO service=search_service trace_id=tr-c2b resource_id=search_9101 '
    'event=index_query_completed result=hit email="ines.old@example.test"',
    # custom_03: completed absence, a failed lookup, and a retained audit record
    '2026-05-09T10:00:00Z level=DEBUG service=account_service trace_id=tr-c3a event=profile_lookup_started '
    'lookup_key="theo@example.test"',
    '2026-05-09T10:00:01Z level=INFO service=account_service trace_id=tr-c3a resource_id=account_9501 '
    'event=profile_lookup_completed rows=0 http_status=404',
    '2026-05-09T10:10:00Z level=DEBUG service=account_service trace_id=tr-c3b event=profile_lookup_started '
    'lookup_key="theo@example.test"',
    '2026-05-09T10:10:05Z level=ERROR service=account_service trace_id=tr-c3b resource_id=account_9502 '
    'event=profile_lookup_completed http_status=503',
    '2026-05-09T10:20:00Z level=INFO service=security_service trace_id=tr-c3c resource_id=audit_record_9501 '
    'event=audit_record_read_completed rows=1 subject_reference=user_9005',
    # custom_04: active flow, restorable backup, completed account absence, look-alike decoy
    '2026-05-09T11:00:00Z level=INFO service=integration_service trace_id=tr-c4a resource_id=data_flow_9301 '
    'event=connector_config_read_completed enabled=true customer_email=dana@example.test',
    '2026-05-09T11:05:00Z level=INFO service=backup_service trace_id=tr-c4b resource_id=backup_9301 '
    'event=restore_policy_read_completed archive_present=true restore_allowed=true customer_email=dana@example.test',
    '2026-05-09T11:10:00Z level=DEBUG service=account_service trace_id=tr-c4c event=profile_lookup_started '
    'lookup_key="dana@example.test"',
    '2026-05-09T11:10:01Z level=INFO service=account_service trace_id=tr-c4c resource_id=account_9301 '
    'event=profile_lookup_completed rows=0 http_status=404',
    '2026-05-09T11:15:00Z level=DEBUG service=search_service trace_id=tr-c4d event=index_query_started '
    'match_key="dana@example.test"',
    '2026-05-09T11:15:01Z level=INFO service=search_service trace_id=tr-c4d resource_id=search_9399 '
    'event=index_query_completed result=hit email="dana.jr@example.test"',
]
LATE_EXPORT = {  # captured after audit_at: shows account_9501 present again; must not change custom_03
    "exported_at": "2026-05-11T09:00:00Z", "region": "global", "source": "records_assurance_gateway",
    "service_exports": {"account_service": {"profile_lookups": [
        {"observed_at": "2026-05-11T08:00:00Z", "resource_id": "account_9501", "status_code": 200,
         "profile": {"email": "theo@example.test", "full_name": "Theo Case"}}]}},
}


def _write_csv(path, name, rows):
    with open(os.path.join(path, name), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(SCHEMAS[name])
        w.writerows(rows)


def build_dataset(root):
    os.makedirs(os.path.join(root, "evidence", "logs"))
    os.makedirs(os.path.join(root, "evidence", "records"))
    shutil.copy(os.path.join(REPO_ROOT, "dataset", "deletion_rules.csv"), os.path.join(root, "deletion_rules.csv"))
    _write_csv(root, "user_identities.csv",
               [(u, n, e, p, _sha(e), "2026-02-01T00:00:00Z") for u, n, e, p in USERS])
    _write_csv(root, "user_audit_log.csv", AUDIT_LOG)
    _write_csv(root, "system_activity.csv", [])
    _write_csv(root, "deletion_requests.csv",
               [(c["case_id"], c["email"], REQUESTED, "COMPLETE", REPORTED, AUDIT, c["text"]) for c in CASES])
    with open(os.path.join(root, "evidence", "logs", "custom_trace.log"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(LOG_LINES) + "\n")
    with open(os.path.join(root, "evidence", "records", "late_export.json"), "w", encoding="utf-8") as fh:
        json.dump(LATE_EXPORT, fh)
    _write_csv(root, "evidence_files.csv", [
        ("evidence_01", "evidence/logs/custom_trace.log", "custom_fixture", "2026-05-10T09:00:00Z"),
        ("evidence_02", "evidence/records/late_export.json", "custom_fixture", "2026-05-11T09:00:00Z")])


def expected_rows():
    rows = {}
    for c in CASES:
        status, steps = c["expected"]
        objs = [dict(zip(("phase", "system_id", "resource_id", "operation", "state", "evidence_id"), s),
                     step_no=i) for i, s in enumerate(steps, 1)]
        rows[c["case_id"]] = {"case_id": c["case_id"], "deletion_status": status,
                              "steps_json": contract.dumps_steps(objs)}
    return rows


def run():
    score = _scorer().score
    root = tempfile.mkdtemp(prefix="ghost_custom_")
    try:
        build_dataset(root)
        ds = Dataset(root)
        auditor = Auditor(ds)
        pred = {}
        for req in ds.requests():
            row, _ = auditor.audit(req)
            pred[req["case_id"]] = {"case_id": req["case_id"], "deletion_status": row["deletion_status"],
                                    "steps_json": contract.dumps_steps(row["steps"]),
                                    "unresolved": row.get("unresolved", "")}
        gold = expected_rows()
        errors = contract.validate_rows([{k: v for k, v in p.items() if k != "unresolved"} for p in pred.values()],
                                        list(gold), ds.known_evidence_ids())
        return gold, pred, errors, score(gold, pred)
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    report = _scorer().report
    gold, pred, errors, (m, fh, ft, conf, mismatches) = run()
    print(f"custom cases: {len(gold)} | contract validation: {'PASS' if not errors else errors}")
    print(report(m, fh, ft, conf))
    for c in CASES:
        ok = pred[c["case_id"]]["deletion_status"] == c["expected"][0] and c["case_id"] not in {x[0] for x in mismatches}
        print(f"  {c['case_id']}: {'PASS' if ok else 'FAIL'} - {c['why']}")
