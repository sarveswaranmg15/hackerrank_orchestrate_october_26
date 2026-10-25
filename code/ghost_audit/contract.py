"""Output contract: allowed phases/operations/states, ordering, serialization and validation."""
import csv
import json
import re

OUTPUT_COLUMNS = ["case_id", "deletion_status", "steps_json"]
STATUSES = ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")
STEP_FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")
PHASES = ("FINDING", "REMEDIATION", "VERIFICATION")

# Operation -> (phase, state), listed in the order mandated by the problem statement.
OPERATIONS = {
    "REPORT_REMAINING_RECORD": ("FINDING", "PRESENT"),
    "REPORT_EXPOSED_PII": ("FINDING", "PRESENT"),
    "REPORT_ACTIVE_DATA_FLOW": ("FINDING", "ACTIVE"),
    "REPORT_RESTORE_RISK": ("FINDING", "ACTIVE"),
    "DISABLE_DATA_FLOW": ("REMEDIATION", "DISABLED"),
    "BLOCK_RESTORE": ("REMEDIATION", "BLOCKED"),
    "DELETE_RECORD": ("REMEDIATION", "ABSENT"),
    "REDACT_RECORD": ("REMEDIATION", "REDACTED"),
    "ANONYMIZE_RECORD": ("REMEDIATION", "ANONYMIZED"),
    "VERIFY_RECORD_ABSENT": ("VERIFICATION", "ABSENT"),
    "VERIFY_RECORD_PRESENT": ("VERIFICATION", "PRESENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFICATION", "REDACTED"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFICATION", "ANONYMIZED"),
    "VERIFY_DATA_FLOW_DISABLED": ("VERIFICATION", "DISABLED"),
    "VERIFY_RESTORE_BLOCKED": ("VERIFICATION", "BLOCKED"),
}
OPERATION_RANK = {op: i for i, op in enumerate(OPERATIONS)}
PHASE_RANK = {p: i for i, p in enumerate(PHASES)}
EVIDENCE_ID_RE = re.compile(r"^(evidence|activity|audit)_\d+$")


def case_sort_key(case_id):
    m = re.search(r"(\d+)$", case_id)
    return (case_id[: m.start()] if m else case_id, int(m.group(1)) if m else -1)


def step_sort_key(step):
    return (PHASE_RANK[step["phase"]], OPERATION_RANK[step["operation"]], step["system_id"], step["resource_id"])


def make_step(operation, system_id, resource_id, evidence_id=""):
    phase, state = OPERATIONS[operation]
    return {"step_no": 0, "phase": phase, "system_id": system_id, "resource_id": resource_id,
            "operation": operation, "state": state, "evidence_id": evidence_id or ""}


def finalize_steps(steps):
    """Deduplicate, order and number steps."""
    seen, unique = set(), []
    for s in steps:
        key = (s["operation"], s["system_id"], s["resource_id"])
        if key not in seen:
            seen.add(key)
            unique.append(dict(s))
    unique.sort(key=step_sort_key)
    for i, s in enumerate(unique, 1):
        s["step_no"] = i
    return [{k: s[k] for k in STEP_FIELDS} for s in unique]


def dumps_steps(steps):
    return json.dumps(steps, ensure_ascii=False, separators=(",", ":"))


def write_output(path, rows):
    rows = sorted(rows, key=lambda r: case_sort_key(r["case_id"]))
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(OUTPUT_COLUMNS)
        for r in rows:
            w.writerow([r["case_id"], r["deletion_status"], dumps_steps(r["steps"])])


def validate_rows(rows, expected_case_ids=None, known_evidence_ids=None):
    """Validate parsed output rows ({case_id, deletion_status, steps_json(str)}). Returns a list of error strings."""
    errors = []
    ids = [r.get("case_id") for r in rows]
    if expected_case_ids is not None:
        exp = set(expected_case_ids)
        missing, extra = exp - set(ids), set(ids) - exp
        if missing:
            errors.append(f"missing case_ids: {sorted(missing, key=case_sort_key)[:10]} ({len(missing)} total)")
        if extra:
            errors.append(f"unexpected case_ids: {sorted(extra, key=case_sort_key)[:10]} ({len(extra)} total)")
    dupes = {c for c in ids if ids.count(c) > 1}
    if dupes:
        errors.append(f"duplicate case_ids: {sorted(dupes)[:10]}")
    if ids != sorted(ids, key=case_sort_key):
        errors.append("rows are not sorted by case_id")
    for r in rows:
        cid, status = r.get("case_id"), r.get("deletion_status")
        if status not in STATUSES:
            errors.append(f"{cid}: invalid deletion_status {status!r}")
        try:
            steps = json.loads(r.get("steps_json", ""))
        except (TypeError, ValueError) as exc:
            errors.append(f"{cid}: steps_json is not valid JSON ({exc})")
            continue
        if not isinstance(steps, list):
            errors.append(f"{cid}: steps_json is not a JSON array")
            continue
        errors.extend(f"{cid}: {e}" for e in _validate_steps(status, steps, known_evidence_ids))
    return errors


def _validate_steps(status, steps, known_evidence_ids):
    errs = []
    if status == "NOT_APPLICABLE":
        if steps:
            errs.append("NOT_APPLICABLE must have an empty steps array")
        return errs
    if not steps:
        errs.append(f"{status} case has no steps")
    prev = None
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or set(s) != set(STEP_FIELDS):
            errs.append(f"step {i}: fields must be exactly {STEP_FIELDS}")
            continue
        if s["step_no"] != i:
            errs.append(f"step {i}: step_no {s['step_no']} is not consecutive from 1")
        op = s["operation"]
        if op not in OPERATIONS:
            errs.append(f"step {i}: unknown operation {op!r}")
            continue
        phase, state = OPERATIONS[op]
        if s["phase"] != phase or s["state"] != state:
            errs.append(f"step {i}: {op} requires phase={phase} state={state}")
        for f in ("system_id", "resource_id"):
            if not isinstance(s[f], str) or not s[f]:
                errs.append(f"step {i}: empty {f}")
        ev = s["evidence_id"]
        if not isinstance(ev, str):
            errs.append(f"step {i}: evidence_id must be a string")
            continue
        if status == "COMPLETE":
            if phase != "VERIFICATION":
                errs.append(f"step {i}: COMPLETE cases may only contain VERIFICATION steps")
            if not ev:
                errs.append(f"step {i}: COMPLETE verification must cite evidence")
        elif status == "INCOMPLETE":
            if phase == "FINDING" and not ev:
                errs.append(f"step {i}: finding must cite evidence")
            if phase != "FINDING" and ev:
                errs.append(f"step {i}: remediation/future verification must use empty evidence_id")
        if ev and not EVIDENCE_ID_RE.match(ev):
            errs.append(f"step {i}: malformed evidence_id {ev!r}")
        if ev and known_evidence_ids is not None and ev not in known_evidence_ids:
            errs.append(f"step {i}: evidence_id {ev!r} does not exist in participant data")
        key = step_sort_key(s)
        if prev is not None and key < prev:
            errs.append(f"step {i}: steps are out of phase/operation/system/resource order")
        prev = key
    if status == "INCOMPLETE":
        phases = {s.get("phase") for s in steps if isinstance(s, dict)}
        if "FINDING" not in phases:
            errs.append("INCOMPLETE case has no findings")
    return errs


def read_output(path):
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames
        return header, list(reader)
