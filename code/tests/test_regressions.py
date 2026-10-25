"""Regression tests for failures found while evaluating on the public samples.

Run from the repository root:  python3 -m unittest discover -s code/tests -v
Tests use participant-facing inputs only; sample_output.csv is read solely by the contract test.
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest

CODE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(CODE)
DATASET = os.path.join(REPO, "dataset")
sys.path.insert(0, CODE)

from ghost_audit import contract, intent  # noqa: E402
from ghost_audit.extract import images, logs  # noqa: E402
from ghost_audit.loaders import Dataset  # noqa: E402
from ghost_audit.observations import interpret  # noqa: E402
from ghost_audit.pipeline import Auditor  # noqa: E402


class PipelineFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = Dataset(DATASET)
        cls.auditor = Auditor(cls.ds)
        cls.samples = {r["case_id"]: r for r in cls.ds.requests("sample_deletion_requests.csv")}
        cls.evals = {r["case_id"]: r for r in cls.ds.requests("deletion_requests.csv")}
        cls.captured = {m["file_id"]: m["captured_at"] for m in cls.ds.manifest}

    def audit(self, case_id):
        req = self.samples.get(case_id) or self.evals[case_id]
        return self.auditor.audit(req)

    def ops(self, row):
        return {(s["operation"], s["resource_id"], s["evidence_id"]) for s in row["steps"]}


class TestContract(PipelineFixture):
    def test_official_sample_output_passes_validator(self):
        header, rows = contract.read_output(os.path.join(DATASET, "sample_output.csv"))
        self.assertEqual(header, contract.OUTPUT_COLUMNS)
        errors = contract.validate_rows(rows, list(self.samples), self.ds.known_evidence_ids())
        self.assertEqual(errors, [])

    def test_validator_rejects_contract_violations(self):
        good = contract.make_step("VERIFY_RECORD_ABSENT", "account_service", "account_1", "evidence_01")
        cases = {
            "wrong state": dict(good, state="PRESENT"),
            "invented operation": dict(good, operation="PURGE_RECORD"),
            "remediation in COMPLETE": contract.make_step("DELETE_RECORD", "a", "b"),
            "unknown evidence": dict(good, evidence_id="evidence_999"),
        }
        for label, step in cases.items():
            step = dict(step, step_no=1)
            row = {"case_id": "case_1", "deletion_status": "COMPLETE", "steps_json": json.dumps([step])}
            errs = contract.validate_rows([row], ["case_1"], {"evidence_01"})
            self.assertTrue(errs, label)

    def test_not_applicable_requires_empty_steps(self):
        step = dict(contract.make_step("VERIFY_RECORD_ABSENT", "s", "r", "evidence_01"), step_no=1)
        row = {"case_id": "case_1", "deletion_status": "NOT_APPLICABLE", "steps_json": json.dumps([step])}
        self.assertTrue(contract.validate_rows([row], ["case_1"]))

    def test_case_ids_sort_numerically(self):
        self.assertEqual(sorted(["case_100", "case_33", "case_9"], key=contract.case_sort_key),
                         ["case_9", "case_33", "case_100"])


class TestObservationSemantics(unittest.TestCase):
    def obs(self, **row):
        return interpret(dict(resource_id="account_1", **row), evidence_id="evidence_01",
                         captured_at="2026-01-01T00:00:00Z", observed_at="2026-01-01T00:00:00Z",
                         system="account_service")

    def test_failed_lookup_is_not_absence(self):
        self.assertIsNone(self.obs(http_status="503", rows="0").present)
        self.assertIsNone(self.obs(http_status="403").present)

    def test_completed_404_is_absence(self):
        self.assertIs(self.obs(http_status="404", rows="0").present, False)

    def test_analytics_reference_keeps_record_attributable(self):
        o = interpret({"resource_id": "analytics_1", "user_id": "user_1", "email": None},
                      evidence_id="e", captured_at="t", observed_at="t", system="analytics_platform")
        self.assertIs(o.exposed, True)


class TestLogFormats(unittest.TestCase):
    def test_all_line_formats_parse(self):
        lines = {
            "bracket": '2026-07-28T05:51:21Z [INFO] [account_service] [tr-1] profile_lookup_completed: '
                       'resource_id="account_7947" rows="0" http_status="404"',
            "pipe": '2026-07-03T07:51:32Z | DEBUG | security_service | tr-2 | audit_record_read_completed | '
                    'resource_id="audit_record_4413" rows="1"',
            "cloud": '2026-06-08T06:52:09Z requestId=tr-3 level=INFO function=search_service message="resource_id='
                     '\\"search_021\\" event=\\"index_query_completed\\" result=\\"hit\\""',
            "syslog": '2026-08-22T10:51:17Z <15>1 node-1 account_service tr-4 - - resource_id="account_1785" '
                      'event="profile_lookup_completed" rows="1"',
            "json": '2026-09-16T08:54:49Z {"service":"backup_service","trace_id":"tr-5","resource_id":"backup_3933",'
                    '"event":"restore_authorization_completed","decision":"deny"}',
        }
        for label, line in lines.items():
            p = logs.parse_line(line)
            self.assertTrue(p and p.get("resource_id") and p.get("trace_id") and p.get("event"), label)


class TestIntent(unittest.TestCase):
    def test_negated_deletion_is_not_authorized(self):
        for text in ["Could you send me a copy of the information held on my account? I still use it, so please "
                     "don't delete or change anything.",
                     "Can you tell me what personal information you hold about me? This is an information "
                     "request, not a request to erase it.",
                     "Please remove me from marketing lists only. I still want receipts and service messages.",
                     "I can't access my profile and need help resetting the login. Do not close the account."]:
            self.assertFalse(intent.classify(text)[0], text)

    def test_deletion_confirmation_is_authorized(self):
        self.assertTrue(intent.classify("Please check the latest state and confirm that the profile is gone "
                                        "and that remaining records cannot identify me.")[0])
        self.assertTrue(intent.classify("이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다.")[0])


class TestLinkingAndTime(PipelineFixture):
    def test_negative_lookup_before_request_does_not_link(self):
        # case_17: a 404 lookup on account_6523 from March belongs to an earlier lifecycle.
        row, trace = self.audit("case_17")
        self.assertNotIn("account_6523", trace["linked"])
        self.assertIn("account_4092", trace["linked"])

    def test_record_time_decides_facts_capture_time_decides_citations(self):
        # case_24: audit_record_7095 is known only from a bundle captured after audit_at whose snapshot time is
        # at audit_at: it is used as a fact but never cited.
        row, trace = self.audit("case_24")
        self.assertIn(("VERIFY_RECORD_PRESENT", "audit_record_7095", ""), self.ops(row))

    def test_no_step_cites_evidence_captured_after_audit(self):
        for cid, req in list(self.samples.items()) + list(self.evals.items()):
            row, _ = self.auditor.audit(req)
            for s in row["steps"]:
                if s["evidence_id"].startswith("evidence_"):
                    self.assertLessEqual(self.captured[s["evidence_id"]], req["audit_at"], (cid, s))

    def test_partial_inventory_is_not_proof_of_deletion(self):
        # case_05: an empty 09-05 inventory must not override the failover replica that still holds the row.
        row, _ = self.audit("case_05")
        self.assertEqual(row["deletion_status"], "INCOMPLETE")
        self.assertIn(("REPORT_REMAINING_RECORD", "account_005", "evidence_54"), self.ops(row))

    def test_catalog_claim_rejected_when_row_names_someone_else(self):
        # marketing_001's screenshot shows sj.park@naver.com; the catalog claims kwame-mensah@proton.me.
        _, trace = self.audit("case_63")
        self.assertNotIn("marketing_001", trace["linked"])

    def test_catalog_claim_accepted_for_anonymous_row(self):
        # object_19 is shown only by file name; the catalog ties it to priya.raman@gmail.com.
        row, trace = self.audit("case_61")
        self.assertIn("object_19", trace["linked"])

    def test_previous_lifecycle_records_are_out_of_scope(self):
        _, trace7 = self.audit("case_07")
        self.assertNotIn("object_19", trace7["linked"])
        _, trace17 = self.audit("case_17")
        self.assertNotIn("tombstone_17", trace17["linked"])

    def test_prior_lifecycle_rule_requires_evidence_inside_earlier_audit_window(self):
        _, trace = self.audit("case_07")
        self.assertIn("object_19", trace["rejected_links"])
        self.assertIn("case_61", trace["rejected_links"]["object_19"])

    def test_prior_audit_scoping_is_configurable(self):
        off = Auditor(self.ds, index=self.auditor.index, scope_prior_audit_evidence=False)
        _, trace = off.audit(self.samples["case_07"])
        self.assertIn("object_19", trace["linked"])
        _, trace = self.audit("case_07")
        self.assertNotIn("object_19", trace["linked"])

    def test_request_history_reads_no_sample_files(self):
        self.assertEqual(self.auditor.history.get("user_07"), [("2026-02-11T05:00:00Z", "2026-02-14T10:00:00Z",
                                                               "case_61")])
        # Only the loader's schema table names the sample request file (used when a user explicitly audits it).
        for root, _, files in os.walk(os.path.join(CODE, "ghost_audit")):
            for f in files:
                if f.endswith(".py") and f != "loaders.py":
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        self.assertNotIn("sample_deletion_requests", fh.read(), f)

    def test_timing_candidates_never_become_steps(self):
        # case_02: account_002 is tied to the case only by deletion-worker timing.
        row, trace = self.audit("case_02")
        cands = {c["resource_id"] for c in trace["candidate_associations"]}
        self.assertIn("account_002", cands)
        self.assertNotIn("account_002", {s["resource_id"] for s in row["steps"]})
        for c in trace["candidate_associations"]:
            self.assertIn("timing only", c["basis"])

    def test_authorized_request_without_evidence_is_flagged_unresolved(self):
        for cid in ("case_01", "case_02", "case_04", "case_63"):
            row, _ = self.audit(cid)
            self.assertEqual(row["steps"], [])
            self.assertIn("no resource", row.get("unresolved", ""), cid)
        row, _ = self.audit("case_55")  # linked backup exists, but its restore state at audit_at is unknown
        self.assertIn("no evidence-backed step", row["unresolved"])
        row, _ = self.audit("case_05")
        self.assertNotIn("unresolved", row)

    def test_complete_requires_known_compliance_of_every_linked_record(self):
        # case_46: account_6523 is provably absent, but tombstone_17 (screenshot shows no identifier fields)
        # has unknown exposure, so COMPLETE is not provable and the row is gated rather than claimed.
        row, trace = self.audit("case_46")
        self.assertIn("tombstone_17", trace["unknown_compliance"])
        self.assertIn("tombstone_17: compliance unknown at audit_at; no verification can be stated", trace["uncited"])
        self.assertFalse(any("compliant per" in u for u in trace["uncited"] if u.startswith("tombstone_17")))
        self.assertIn("COMPLETE not provable", row["unresolved"])
        # A COMPLETE row whose linked records are all decided stays resolved.
        row, trace = self.audit("case_21")
        self.assertEqual(row["deletion_status"], "COMPLETE")
        self.assertNotIn("unresolved", row)
        self.assertEqual(trace["unknown_compliance"], [])

    def test_cli_refuses_to_write_output_with_unresolved_rows(self):
        from main import run
        tmp = tempfile.mkdtemp()
        try:
            out = os.path.join(tmp, "out.csv")
            run(DATASET, "sample_deletion_requests.csv", out, only={"case_01", "case_05"})
            self.assertFalse(os.path.exists(out))
            run(DATASET, "sample_deletion_requests.csv", out, only={"case_05"})
            self.assertTrue(os.path.exists(out))
        finally:
            shutil.rmtree(tmp)

    def test_replica_drift_reports_remaining_copy(self):
        # case_16: the primary no longer has account_994 but the read replica does.
        row, _ = self.audit("case_16")
        self.assertIn(("REPORT_REMAINING_RECORD", "account_994", "evidence_05"), self.ops(row))

    def test_historical_email_hash_links_record(self):
        # case_12: analytics_9914 is keyed by the SHA-256 of the user's previous email.
        row, trace = self.audit("case_12")
        self.assertIn("analytics_9914", trace["linked"])

    def test_decoy_lookalike_email_not_linked(self):
        _, trace = self.audit("case_28")
        self.assertNotIn("search_8168", trace["linked"])  # smartinez6461.jr@gmail.com belongs to someone else


class TestAttachmentEvidence(PipelineFixture):
    """Defects found in the readiness review: ticket_attachments category, uploaded_by, silent newer rows."""

    def obs(self, rid, t, **fields):
        return interpret(dict(resource_id=rid, **fields), evidence_id="evidence_01", captured_at=t,
                         observed_at=t, system="support_platform")

    def test_ticket_attachments_collection_is_support_attachment(self):
        bundle = [o for o in self.auditor.index.by_resource["support_2283"] if o.evidence_id == "evidence_17"][0]
        self.assertEqual(bundle.category, "SUPPORT_ATTACHMENT")          # JSON bundle path
        sql = [o for o in self.auditor.index.by_resource["support_8903"] if o.evidence_id == "evidence_20"]
        self.assertEqual({o.category for o in sql}, {"SUPPORT_ATTACHMENT"})  # SQL record_json path

    def test_uploaded_by_exposes_only_person_identifiers(self):
        self.assertIs(self.obs("support_1", "t", uploaded_by="tunde-balogun@proton.me").exposed, True)
        self.assertIsNot(self.obs("support_1", "t", uploaded_by="support-service").exposed, True)

    def test_unrelated_uploader_is_not_linked_or_exclusive(self):
        subject = self.auditor.identity.subject("user_177")  # Tunde Balogun
        other = self.obs("support_1", "t", uploaded_by="yuna-jang@proton.me")
        other.lookup = frozenset({"tunde-balogun@proton.me"})  # even when looked up by the subject's email
        self.assertIsNone(self.auditor._owned_by_subject(other, subject))
        mixed = interpret({"resource_id": "support_1", "uploaded_by": "yuna-jang@proton.me",
                           "file_name": "x.pdf", "note": "tunde-balogun@proton.me"}, evidence_id="e",
                          captured_at="t", observed_at="t", system="support_platform", category="SUPPORT_ATTACHMENT")
        self.assertEqual(self.auditor._owned_by_subject(mixed, subject), "identifier")
        self.assertEqual(self.auditor._attachment_exclusive([mixed], subject), "")  # another person is named

    def test_ambiguous_uploader_does_not_link(self):
        subject = self.auditor.identity.subject("user_177")
        self.assertIsNone(self.auditor._owned_by_subject(self.obs("support_1", "t", uploaded_by="support-service"),
                                                         subject))

    def test_silent_newer_row_keeps_earlier_exposure(self):
        t1, t2, t3 = "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"
        exposed = self.obs("support_1", t1, requester_email="a@b.co")
        silent = self.obs("support_1", t2, ticket_id="CS-1")
        silent.present = True
        state, _ = Auditor._reconstruct([exposed, silent])
        self.assertIs(state["exposed"], True)

    def test_explicit_change_overrides_and_absence_resets(self):
        t1, t2, t3 = "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", "2026-01-03T00:00:00Z"
        exposed = self.obs("support_1", t1, requester_email="a@b.co")
        redacted = self.obs("support_1", t2, message_text="[REDACTED]")
        redacted.present = True
        self.assertIs(Auditor._reconstruct([exposed, redacted])[0]["exposed"], False)
        gone = self.obs("support_1", t2, http_status="404")
        back = self.obs("support_1", t3, ticket_id="CS-1")
        back.present = True
        state, reset = Auditor._reconstruct([exposed, gone, back])
        self.assertIs(state["present"], True)
        self.assertIsNone(state["exposed"])  # exposure seen before the deletion is not carried over
        self.assertEqual(reset, t2)

    def test_repeated_absence_keeps_earliest_citable_proof(self):
        # case_113: two 404s for account_6697; the later one sits in a file captured after audit_at.
        row, _ = self.audit("case_113")
        self.assertIn(("VERIFY_RECORD_ABSENT", "account_6697", "evidence_88"), self.ops(row))

    def test_case_179_resolved_by_pre_audit_evidence(self):
        req = self.evals["case_179"]
        row, trace = self.audit("case_179")
        self.assertNotIn("unresolved", row)
        self.assertEqual(row["deletion_status"], "INCOMPLETE")
        self.assertIn(("REPORT_REMAINING_RECORD", "support_2283", "evidence_84"), self.ops(row))
        self.assertLessEqual(self.captured["evidence_84"], req["audit_at"])
        info = [r for r in trace["resources"] if r["resource_id"] == "support_2283"][0]
        self.assertEqual(info["category"], "SUPPORT_ATTACHMENT")
        self.assertIn("rule_06 applied", info["policy_note"])


def _load_custom_cases():
    import importlib.util
    spec = importlib.util.spec_from_file_location("ghost_custom_cases",
                                                  os.path.join(CODE, "evaluation", "custom_cases.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCustomEvaluationCases(unittest.TestCase):
    """Participant-created synthetic cases (code/evaluation/custom_cases.py) must match their hand-derived answers."""

    def test_custom_cases_match_expected_exactly(self):
        custom_cases = _load_custom_cases()
        gold, pred, errors, (m, _, _, _, mismatches) = custom_cases.run()
        self.assertEqual(errors, [])
        self.assertEqual(mismatches, [])
        self.assertEqual(m["exact"], len(gold))
        self.assertTrue(all(not p["unresolved"] for p in pred.values()))

    def test_failed_lookup_never_links(self):
        custom_cases = _load_custom_cases()
        root = tempfile.mkdtemp()
        try:
            custom_cases.build_dataset(root)
            ds = Dataset(root)
            req = [r for r in ds.requests() if r["case_id"] == "custom_03"][0]
            _, trace = Auditor(ds).audit(req)
            self.assertNotIn("account_9502", trace["linked"])  # 503 lookup by the subject's own email
            self.assertIn("no completed result", trace["rejected_links"]["account_9502"])
        finally:
            shutil.rmtree(root)

    def test_read_record_fields_imply_presence(self):
        # support_9996 (case_264): a message read whose text is "[REDACTED]" shows the record exists.
        o = interpret({"resource_id": "support_1", "message_text": "[REDACTED]"}, evidence_id="e",
                      captured_at="t", observed_at="t", system="support_platform")
        self.assertIs(o.present, True)
        self.assertIs(o.exposed, False)


class TestPackageCheck(unittest.TestCase):
    def _load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("ghost_package_check", os.path.join(CODE, "package_check.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_repository_package_has_no_prohibited_content(self):
        problems, _, files = self._load().check(REPO)
        self.assertIn("README.md", files)
        self.assertFalse(any(f.startswith("dataset/") or "__pycache__" in f for f in files))
        self.assertFalse([p for p in problems if "prohibited" in p or "secret" in p or "nested archive" in p])

    def test_detects_prohibited_content_and_missing_output(self):
        pc = self._load()
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "code", "dataset"))
            with open(os.path.join(tmp, "README.md"), "w") as fh:
                fh.write("readme")
            with open(os.path.join(tmp, "code", "dataset", "x.csv"), "w") as fh:
                fh.write("a")
            with open(os.path.join(tmp, "code", "inner.zip"), "wb") as fh:
                fh.write(b"PK")
            with open(os.path.join(tmp, "code", "cfg.py"), "w") as fh:
                fh.write("API" + "_KEY" + ' = "' + "abcd1234" + "efgh5678" + '"')  # assembled: no literal in source
            problems, _, _ = pc.check(tmp)
            joined = "\n".join(problems)
            for expected in ("prohibited (dataset)", "nested archive", "possible secret", "output.csv not generated",
                             "missing from code.zip: code/evaluation/evaluation_report.md"):
                self.assertIn(expected, joined)
        finally:
            shutil.rmtree(tmp)


class TestScreenshots(PipelineFixture):
    def test_all_screenshots_read_with_provenance(self):
        self.assertEqual(self.auditor.index.unread, {})
        for m in self.ds.manifest:
            if m["file_path"].endswith(".png"):
                self.assertIn("transcription", self.auditor.index.provenance.get(m["file_id"], ""))

    def test_rows_with_list_columns_parse(self):
        flows = {o.resource_id: o for o in self.auditor.index.observations if o.evidence_id == "evidence_14"}
        self.assertIs(flows["data_flow_1121"].flow_active, True)
        backups = {o.resource_id: o for o in self.auditor.index.observations if o.evidence_id == "evidence_32"}
        self.assertIsNone(backups["backup_9287"].restore_allowed)  # inferred restore state awaits review

    def test_ambiguous_screenshot_claims_need_review(self):
        backups = {o.resource_id: o for o in self.auditor.index.observations if o.evidence_id == "evidence_32"}
        self.assertTrue(backups["backup_9287"].needs_review)
        self.assertEqual(backups["backup_9287"].review_scope, "state")
        self.assertIsNone(backups["backup_9287"].restore_allowed)
        # case_55: the only restore-risk evidence is the inferred drill, so no finding is confirmed from it.
        row, trace = self.audit("case_55")
        self.assertNotIn(("REPORT_RESTORE_RISK", "backup_9287", "evidence_32"), self.ops(row))
        info = [r for r in trace["resources"] if r["resource_id"] == "backup_9287"][0]
        self.assertTrue(info["pending_review"])
        # support_9538 appears on user_11's page only by placement (ownership needs review). It is linked to
        # Haruka Sato (case_34) through an independent log matching her historical phone, never the screenshot.
        shot = [o for o in self.auditor.index.observations
                if o.evidence_id == "evidence_04" and o.resource_id == "support_9538"][0]
        self.assertEqual(shot.review_scope, "ownership")
        _, trace = self.audit("case_34")
        self.assertIn("evidence_01", trace["linked"]["support_9538"])
        self.assertNotIn("evidence_04", trace["linked"]["support_9538"])

    def test_case55_restore_state_stays_unknown_at_audit_time(self):
        # Reviewer decision: the April screenshot does not establish restorability, and the September log
        # (evidence_01: eligible=true deletion_block=false) must not reconstruct the April state.
        later = [o for o in self.auditor.index.by_resource["backup_9287"] if o.evidence_id == "evidence_01"][0]
        self.assertIs(later.restore_allowed, True)
        self.assertGreater(later.observed_at, self.evals["case_55"]["audit_at"])
        row, trace = self.audit("case_55")
        info = [r for r in trace["resources"] if r["resource_id"] == "backup_9287"][0]
        self.assertIsNone(info["state"]["restore_allowed"])
        self.assertEqual(row["steps"], [])
        self.assertIn("unresolved", row)
        with open(os.path.join(images.TRANSCRIPT_DIR, "backup_restore_console_20260429.png.txt"),
                  encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("are treated as visible evidence that the restore path works;", text.split("correction 3")[0])
        self.assertIn("NOT treated as evidence of current restorability", text)

    def test_rule06_identifier_match_is_not_exclusive_ownership(self):
        # Reviewer decision: support_9538 is linked by a PII scan of Haruka Sato's historical phone (audit_279);
        # that proves the identifier is inside, not that the attachment holds only her data.
        row, trace = self.audit("case_34")
        info = [r for r in trace["resources"] if r["resource_id"] == "support_9538"][0]
        self.assertEqual(info["action"], "REDACT")
        self.assertIn("rule_06 condition not established", info["policy_note"])
        ops = self.ops(row)
        self.assertIn(("REPORT_EXPOSED_PII", "support_9538", "evidence_01"), ops)
        self.assertIn(("REDACT_RECORD", "support_9538", ""), ops)
        self.assertIn(("VERIFY_RECORD_REDACTED", "support_9538", ""), ops)
        self.assertFalse({op for op, rid, _ in ops if rid == "support_9538"} &
                         {"REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"})
        self.assertNotIn("evidence_04", trace["linked"]["support_9538"])

    def test_rule06_applied_when_exclusive_ownership_is_established(self):
        for cid, rid in (("case_07", "support_001"), ("case_13", "support_4218"), ("case_61", "object_19")):
            row, trace = self.audit(cid)
            info = [r for r in trace["resources"] if r["resource_id"] == rid][0]
            self.assertEqual(info["action"], "DELETE", cid)
            self.assertIn("rule_06 applied", info["policy_note"], cid)
            self.assertIn("DELETE_RECORD", {op for op, r, _ in self.ops(row) if r == rid}, cid)

    def test_screenshot_provenance_is_distinguishable(self):
        kinds = {o.source_kind for o in self.auditor.index.observations if o.evidence_id == "evidence_04"}
        self.assertEqual(kinds, {"screenshot_transcript"})
        _, trace = self.audit("case_61")
        info = [r for r in trace["resources"] if r["resource_id"] == "object_19"][0]
        self.assertEqual(info["evidence_basis"], "screenshot_transcript_only")
        _, trace = self.audit("case_16")
        info = [r for r in trace["resources"] if r["resource_id"] == "account_994"][0]
        self.assertEqual(info["evidence_basis"], "non_screenshot_evidence")

    def test_audited_transcript_corrections(self):
        obs = {o.resource_id: o for o in self.auditor.index.observations if o.evidence_id in ("evidence_04", "evidence_28")}
        self.assertIn("on track for delivery on 26 May", obs["support_1034"].detail + self.auditor.index.texts["evidence_28"])
        self.assertIn("loyalty_tier=Silver", self.auditor.index.texts["evidence_04"])
        self.assertNotIn('status="Silver"', self.auditor.index.texts["evidence_04"])

    def test_transcript_hash_mismatch_is_reported_unread(self):
        tmp = tempfile.mkdtemp()
        try:
            fake = os.path.join(tmp, "backup_restore_console_20260429.png")
            with open(fake, "wb") as fh:
                fh.write(b"not the original image")
            res = images.extract(fake, {"file_id": "evidence_x", "captured_at": "2026-01-01T00:00:00Z"})
            self.assertIn("hash", res.unread)
            self.assertEqual(res.observations, [])
        finally:
            shutil.rmtree(tmp)


class TestReproducibility(PipelineFixture):
    def test_prediction_path_never_reads_sample_output(self):
        for root, _, files in os.walk(os.path.join(CODE, "ghost_audit")):
            for f in files:
                if f.endswith(".py"):
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        self.assertNotIn("sample_output", fh.read(), f)
        with open(os.path.join(CODE, "main.py"), encoding="utf-8") as fh:
            self.assertNotIn("sample_output", fh.read())

    def test_runs_are_deterministic(self):
        def run():
            a = Auditor(Dataset(DATASET))
            return [contract.dumps_steps(a.audit(r)[0]["steps"]) for r in self.samples.values()]
        self.assertEqual(run(), run())


if __name__ == "__main__":
    unittest.main()
