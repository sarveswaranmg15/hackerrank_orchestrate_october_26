"""Map deletion_rules.csv onto resource categories and decide compliance of a resource's state."""

# Categories that are processes rather than records; they are governed by the "no recreation path" rule.
PROCESS_CATEGORIES = {"DATA_FLOW"}
# Treatments understood by the planner. Data flows are not in deletion_rules.csv: an active flow that can
# recreate the subject's data must always be disabled.
KNOWN_ACTIONS = {"DELETE", "REDACT", "ANONYMIZE", "RETAIN"}


class Policy:
    def __init__(self, rules):
        self.by_category = {}
        for r in rules:
            action = r["required_action"].strip().upper()
            if action not in KNOWN_ACTIONS:
                raise ValueError(f"unsupported action {action} in {r['rule_id']}")
            self.by_category[r["data_category"].strip().upper()] = (action, r["rule_id"], r["condition"])

    def action(self, category):
        if category in PROCESS_CATEGORIES:
            return "DISABLE"
        entry = self.by_category.get(category)
        return entry[0] if entry else None


def evaluate(action, category, state):
    """Return (finding_op, remediation_op, verification_op, compliant) for one resource state.

    state: dict with present/exposed/flow_active/restore_allowed (True/False/None).
    Returns None when the state is unknown and nothing can be concluded.
    """
    present, exposed = state.get("present"), state.get("exposed")
    if action == "DISABLE":
        active = state.get("flow_active")
        if active is True:
            return ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED", False)
        if active is False or present is False:
            return (None, None, "VERIFY_DATA_FLOW_DISABLED", True)
        return None
    if category == "BACKUP_ARCHIVE":
        allowed = state.get("restore_allowed")
        if allowed is True:
            return ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED", False)
        if allowed is False:
            return (None, None, "VERIFY_RESTORE_BLOCKED", True)
        return None
    if action == "DELETE":
        if present is True:
            return ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT", False)
        if present is False:
            return (None, None, "VERIFY_RECORD_ABSENT", True)
        return None
    if action in ("REDACT", "ANONYMIZE"):
        verify = "VERIFY_RECORD_REDACTED" if action == "REDACT" else "VERIFY_RECORD_ANONYMIZED"
        remed = "REDACT_RECORD" if action == "REDACT" else "ANONYMIZE_RECORD"
        if present is False:
            return (None, None, "VERIFY_RECORD_ABSENT", True)
        if exposed is True:
            return ("REPORT_EXPOSED_PII", remed, verify, False)
        if exposed is False:
            return (None, None, verify, True)
        return None
    if action == "RETAIN":
        if present is True:
            return (None, None, "VERIFY_RECORD_PRESENT", True)
        return None
    return None


def final_state_check(action, category):
    """The verification that proves the required final state, independent of the current state."""
    if category == "BACKUP_ARCHIVE":
        return "VERIFY_RESTORE_BLOCKED"
    return {"DISABLE": "VERIFY_DATA_FLOW_DISABLED", "DELETE": "VERIFY_RECORD_ABSENT",
            "REDACT": "VERIFY_RECORD_REDACTED", "ANONYMIZE": "VERIFY_RECORD_ANONYMIZED",
            "RETAIN": "VERIFY_RECORD_PRESENT"}.get(action)
