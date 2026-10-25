"""Load and schema-check the participant-facing CSV inputs."""
import csv
import os

SCHEMAS = {
    "deletion_requests.csv": ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"],
    "sample_deletion_requests.csv": ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"],
    "user_identities.csv": ["user_id", "name", "email", "phone", "email_hash", "updated_at"],
    "user_audit_log.csv": ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"],
    "system_activity.csv": ["activity_id", "event_time", "event_type", "source_system", "target_system", "resource_id", "reported_result", "details"],
    "deletion_rules.csv": ["rule_id", "data_category", "condition", "required_action"],
    "evidence_files.csv": ["file_id", "file_path", "origin", "captured_at"],
}


class SchemaError(ValueError):
    pass


def read_csv(dataset_dir, name):
    path = os.path.join(dataset_dir, name)
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        expected = SCHEMAS.get(name)
        if expected and reader.fieldnames != expected:
            raise SchemaError(f"{name}: expected columns {expected}, found {reader.fieldnames}")
        return list(reader)


class Dataset:
    """All participant-visible tabular inputs."""

    def __init__(self, dataset_dir):
        self.dir = dataset_dir
        self.identities = read_csv(dataset_dir, "user_identities.csv")
        self.audit_log = read_csv(dataset_dir, "user_audit_log.csv")
        self.activity = read_csv(dataset_dir, "system_activity.csv")
        self.rules = read_csv(dataset_dir, "deletion_rules.csv")
        self.manifest = read_csv(dataset_dir, "evidence_files.csv")
        self.activity_by_id = {a["activity_id"]: a for a in self.activity}

    def requests(self, name="deletion_requests.csv"):
        rows = read_csv(self.dir, name)
        seen = set()
        for r in rows:
            if r["case_id"] in seen:
                raise SchemaError(f"{name}: duplicate case_id {r['case_id']}")
            seen.add(r["case_id"])
        return rows

    def known_evidence_ids(self):
        return ({m["file_id"] for m in self.manifest} | set(self.activity_by_id)
                | {a["audit_id"] for a in self.audit_log})
