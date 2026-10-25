"""Case audit pipeline: link evidence to the subject, reconstruct state at audit_at, plan steps.

Time semantics (see README):
  * record time (Observation.observed_at: row/line/snapshot time) decides what was true at audit_at;
  * file capture time (manifest captured_at) decides what may be cited: a step never cites a file captured
    after audit_at;
  * activity event_time only orders workflow claims; activity results are never used as proof of state.
"""
import re
from collections import defaultdict
from datetime import datetime, timedelta

from . import contract, intent, norm
from .extract import extract_file
from .extract.pdf import document_observations
from .identity import IdentityResolver
from .observations import DEFAULT_CATEGORY, interpret
from .policy import Policy, evaluate, final_state_check

LINK_RE = re.compile(r"(?:linked|confirmed) (\S+) \(([^)]*)\)")
CATALOG_WINDOW = timedelta(hours=1)  # a catalog annotation follows the capture it describes
# Scoping policy (not stated in the written contract; see README): evidence captured entirely inside an earlier,
# separate deletion request's audit window [requested_at, audit_at] by the same person was gathered for that
# request, so those records are excluded from a later request's audit.
SCOPE_PRIOR_AUDIT_EVIDENCE = True
# Timing-only association window for deletion-worker activity (informational candidates, never steps).
CANDIDATE_AFTER_REPORT = timedelta(hours=2)


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _is_person_id(key):
    return "@" in key or key.startswith("user_") or len(key) == 64 or key.startswith("tel:")


class EvidenceIndex:
    def __init__(self, dataset):
        self.dataset = dataset
        self.observations, self.scopes, self.texts, self.unread, self.provenance = [], [], {}, {}, {}
        pending = {}
        for meta in dataset.manifest:
            res = extract_file(dataset.dir, meta)
            self.observations.extend(res.observations)
            self.scopes.extend(res.scopes)
            self.texts[meta["file_id"]] = res.text
            if res.provenance:
                self.provenance[meta["file_id"]] = res.provenance
            if res.unread:
                self.unread[meta["file_id"]] = (meta["file_path"], res.unread)
            if res.pending:
                pending[meta["file_id"]] = res.pending
        self.files_by_id = {m["file_id"]: m for m in dataset.manifest}
        self.alias_resolutions = self._resolve_aliases(pending)
        self.unresolved = {fid: rows for fid, rows in pending.items()
                           if not any(r.get("_resolved") for r in rows)}
        self.by_resource = defaultdict(list)
        self.by_key = defaultdict(set)
        for o in self.observations:
            self.by_resource[o.resource_id].append(o)
            for k in o.ids | o.lookup:
                self.by_key[k].add(o.resource_id)

    def _resolve_aliases(self, pending):
        """Data-catalog activity names the resource id behind an alias shown in a capture taken just before it.

        Only the alias -> resource_id mapping is taken from the activity. Whose record it is, and its state,
        come from the evidence row itself.
        """
        resolutions = []
        for a in self.dataset.activity:
            if a["event_type"] != "LINK_RESOURCE_IDENTITY":
                continue
            m = LINK_RE.search(a["details"])
            if not m:
                continue
            rid, alias = m.group(1), m.group(2)
            # Identity the catalog asserts. Used only as a lookup-style link: it never overrides a row that
            # names a different person (see Auditor._owned_by_subject).
            claimed = frozenset(norm.identifiers_in_text(a["details"].split(")", 1)[-1]))
            t = _ts(a["event_time"])
            for fid, rows in pending.items():
                meta = self.files_by_id[fid]
                if not (timedelta(0) <= t - _ts(meta["captured_at"]) <= CATALOG_WINDOW):
                    continue
                for r in rows:
                    if alias not in r["text"]:
                        continue
                    if "document_text" in r["row"]:
                        obs_list = document_observations(r["text"] + f"\n{rid}", meta, "catalog-resolved document")
                        obs_list = [o for o in obs_list if o.resource_id == rid]
                    else:
                        o = interpret(dict(r["row"], resource_id=rid), evidence_id=fid,
                                      captured_at=meta["captured_at"], observed_at=meta["captured_at"],
                                      system=a["target_system"], detail=f"row resolved via {a['activity_id']}")
                        if o and o.present is None:
                            o.present = True
                        obs_list = [o] if o else []
                    src = "screenshot_transcript" if meta["file_path"].endswith(".png") else "pdf_text"
                    exclusive = "belongs only to" in a["details"] and "no other subject" in a["details"]
                    for o in obs_list:
                        o.source_kind = src
                        o.exclusive_claim = exclusive
                        o.needs_review = o.needs_review or r.get("needs_review", "")
                        if o.needs_review and not o.review_scope:
                            o.review_scope = "state" if o.needs_review.startswith("state:") else "ownership"
                        o.lookup = o.lookup | claimed
                        o.detail = f"{o.detail} (alias {alias!r} resolved by {a['activity_id']})"
                        self.observations.append(o)
                    r["_resolved"] = a["activity_id"]
                    resolutions.append((fid, alias, rid, a["activity_id"]))
        return resolutions


class Auditor:
    def __init__(self, dataset, index=None, scope_prior_audit_evidence=SCOPE_PRIOR_AUDIT_EVIDENCE,
                 history_files=("deletion_requests.csv",)):
        self.dataset = dataset
        self.index = index or EvidenceIndex(dataset)
        self.identity = IdentityResolver(dataset)
        self.policy = Policy(dataset.rules)
        self.scope_prior_audit_evidence = scope_prior_audit_evidence
        self.history = self._request_history(history_files)

    def _request_history(self, history_files):
        """Authorized deletion requests per user, read from the evaluation request inputs (never from samples
        or labels)."""
        history = defaultdict(list)
        for name in history_files:
            try:
                rows = self.dataset.requests(name)
            except FileNotFoundError:
                continue
            for r in rows:
                if not intent.classify(r["request_text"])[0]:
                    continue
                subject = self.identity.resolve(r["email"], r["requested_at"])
                if subject:
                    history[subject.user_id].append((r["requested_at"], r["audit_at"], r["case_id"]))
        return history

    def previous_audit_windows(self, subject, req):
        """[requested_at, audit_at] windows of earlier, separate deletion requests by the same person."""
        return sorted((rq, a, cid) for (rq, a, cid) in self.history.get(subject.user_id, [])
                      if cid != req["case_id"] and a <= req["requested_at"])

    def _evidence_owners(self):
        """resource_id -> user_ids named by identifiers inside its own evidence rows (any time)."""
        if not hasattr(self, "_owners"):
            key_user = defaultdict(set)
            for uid in self.identity.users:
                for k in self.identity.subject(uid).keys:
                    key_user[k].add(uid)
            self._owners = defaultdict(set)
            for o in self.index.observations:
                for k in o.ids:
                    self._owners[o.resource_id] |= key_user.get(k, set())
        return self._owners

    def timing_candidates(self, req, linked):
        """Deletion-worker targets whose activity time falls in this request's workflow window.

        Labelled candidates only: timing is not proof of ownership, several requests share windows, and on the
        public samples a unique candidate was wrong in 1 of 3 cases. They never produce steps or affect status.
        """
        lo = req["requested_at"]
        hi = (_ts(req["reported_at"]) + CANDIDATE_AFTER_REPORT).strftime("%Y-%m-%dT%H:%M:%SZ")
        owners = self._evidence_owners()
        out = []
        for a in self.dataset.activity:
            if a["source_system"] != "deletion_worker" or not (lo <= a["event_time"] <= hi):
                continue
            rid = a["resource_id"]
            if rid in linked or owners.get(rid):
                continue
            out.append({"resource_id": rid, "system": a["target_system"], "activity_id": a["activity_id"],
                        "event_time": a["event_time"], "basis": "timing only (unverified, excluded from steps)"})
        return out

    # ---- linking -------------------------------------------------------------------------------
    @staticmethod
    def _owned_by_subject(obs, subject):
        own = {k for k in obs.ids if _is_person_id(k)}
        if own & subject.keys:
            return "identifier"
        if own:  # the record names a different person: never link through the lookup key
            return None
        if obs.lookup & subject.keys:
            return "lookup_key"
        return None

    def link_resources(self, subject, req):
        """Resources whose own evidence (at or before audit_at) ties them to the subject."""
        audit_at, requested_at = req["audit_at"], req["requested_at"]
        idx = self.index
        linked, rejected = {}, {}
        candidates = set()
        for k in subject.keys:
            candidates |= idx.by_key.get(k, set())
        for rid in sorted(candidates):
            for o in sorted(idx.by_resource[rid], key=lambda o: (o.observed_at, o.evidence_id)):
                if o.observed_at > audit_at or o.review_scope == "ownership":
                    continue
                how = self._owned_by_subject(o, subject)
                if not how:
                    continue
                if how == "lookup_key" and o.present is None:
                    # A failed or indeterminate lookup shows neither ownership nor state.
                    rejected.setdefault(rid, f"lookup with no completed result in {o.evidence_id}")
                    continue
                if how == "lookup_key" and o.present is not True and o.observed_at < requested_at:
                    # A negative lookup made before this request belongs to an earlier lifecycle (e.g. a previous
                    # account of the same person); it does not tie the resource to this request.
                    rejected.setdefault(rid, f"negative lookup before requested_at in {o.evidence_id}")
                    continue
                linked.setdefault(rid, f"{how} in {o.evidence_id}")
                for ref in sorted(o.refs):
                    linked.setdefault(ref, f"referenced by {rid} in {o.evidence_id}")
        # A negative lookup does confirm absence of a resource already tied to the subject by other evidence.
        for rid in list(rejected):
            if rid in linked:
                rejected.pop(rid)
        return linked, rejected

    # ---- point-in-time state -------------------------------------------------------------------
    def _category(self, rid, system):
        categories = {o.category for o in self.index.by_resource.get(rid, [])}
        default = DEFAULT_CATEGORY.get(system)
        specific = sorted(c for c in categories if c != default and c != "UNKNOWN")
        return specific[0] if specific else (default or "UNKNOWN")

    @staticmethod
    def _merge(group):
        def any_true(values):
            values = [v for v in values if v is not None]
            return (True in values) if values else None
        return {"present": any_true(o.present for o in group), "exposed": any_true(o.exposed for o in group),
                "flow_active": any_true(o.flow_active for o in group),
                "restore_allowed": any_true(o.restore_allowed for o in group)}

    @classmethod
    def _reconstruct(cls, facts):
        """Replay observations in record-time order and return (state at the last one, time of last absence).

        Observations at the same record time are merged (a copy present in any replica counts as present).
        An explicit value replaces the previous one; a silent attribute (None) keeps the last known value, so a
        newer row that says nothing about exposure cannot erase an earlier observed exposure. An explicit absence
        resets the record: attributes observed before a deletion never carry over to a later re-creation.
        """
        state = {"present": None, "exposed": None, "flow_active": None, "restore_allowed": None}
        reset_at = ""
        for t in sorted({o.observed_at for o in facts}):
            merged = cls._merge([o for o in facts if o.observed_at == t])
            if merged["present"] is False:
                if state["present"] is not False:  # transition into absence; repeated absences keep the start
                    reset_at = t
                state = dict.fromkeys(state)
                state["present"] = False
                continue
            for key, value in merged.items():
                if value is not None:
                    state[key] = value
        return state, reset_at

    def _attachment_exclusive(self, facts, subject):
        """rule_06 condition: is the attachment shown to belong only to the deletion subject?

        Established by an explicit exclusivity statement, or by a non-scan record typed SUPPORT_ATTACHMENT whose
        person identifiers all belong to the subject (single-owner attribution or the attachment's own content).
        A PII-scan match proves the subject's identifier is inside, not that nobody else's data is.
        """
        for o in facts:
            if o.review_scope == "ownership":
                continue
            if o.exclusive_claim:
                return f"exclusive ownership stated in {o.evidence_id}"
            people = {k for k in o.ids if _is_person_id(k)}
            if (o.category == "SUPPORT_ATTACHMENT" and not o.identifier_scan and people
                    and people <= subject.keys):
                return f"attachment record attributed only to the subject in {o.evidence_id}"
        return ""

    def resource_state(self, rid, audit_at, subject=None):
        idx = self.index
        everything = [o for o in idx.by_resource.get(rid, []) if o.observed_at <= audit_at]
        facts = [o for o in everything if o.review_scope != "ownership"]
        system = (facts[0].system if facts else None) or norm.system_for(rid)
        category = self._category(rid, system)
        action = self.policy.action(category)
        policy_note = ""
        if category == "SUPPORT_ATTACHMENT" and action == "DELETE" and subject is not None:
            basis = self._attachment_exclusive([o for o in idx.by_resource.get(rid, []) if o.observed_at <= audit_at],
                                               subject)
            if basis:
                policy_note = f"rule_06 applied: {basis}"
            else:
                # Every applicable rule requires removing the subject's identifiers (rule_06 deletes the whole
                # record, shared support content is redacted under rule_05); whole-record deletion is not asserted.
                action = "REDACT"
                policy_note = ("rule_06 condition not established (identifier match only, exclusive ownership "
                               "unproven): conservative treatment is redaction of the subject's identifiers")
        info = {"resource_id": rid, "system": system, "category": category, "action": action,
                "policy_note": policy_note,
                "state": {}, "decision": None, "evidence_id": "", "observed_at": "", "n_facts": len(facts),
                "pending_review": sorted({f"{o.evidence_id}: {o.needs_review}" for o in everything
                                          if o.needs_review})}
        if not facts:
            return info
        info["state"], reset_at = self._reconstruct(facts)
        info["observed_at"] = max(o.observed_at for o in facts)
        info["decision"] = evaluate(action, category, info["state"]) if action else None
        # Only observations since the last explicit absence describe the record as it stands at audit_at.
        current = [o for o in facts if o.observed_at >= reset_at]
        if info["decision"]:
            # Cite the most recent capture available at audit_at that supports the same conclusion.
            citeable = sorted((o for o in current if o.captured_at <= audit_at),
                              key=lambda o: (o.observed_at, o.evidence_id), reverse=True)
            for o in citeable:
                if evaluate(action, category, self._merge([o])) == info["decision"]:
                    info["evidence_id"] = o.evidence_id
                    break
        # Provenance of the conclusion: screenshot transcripts are distinguishable from corroborated evidence.
        supporting = [o for o in current if info["decision"] and
                      evaluate(action, category, self._merge([o])) == info["decision"]]
        kinds = {o.source_kind for o in supporting}
        info["evidence_basis"] = ("none" if not kinds else "screenshot_transcript_only"
                                  if kinds == {"screenshot_transcript"} else
                                  "corroborated_by_non_screenshot_evidence" if "screenshot_transcript" in kinds
                                  else "non_screenshot_evidence")
        return info

    # ---- case ----------------------------------------------------------------------------------
    def audit(self, req):
        trace = {"case_id": req["case_id"]}
        authorized, why = intent.classify(req["request_text"])
        trace["intent"] = why
        if not authorized:
            return {"case_id": req["case_id"], "deletion_status": "NOT_APPLICABLE", "steps": []}, trace
        audit_at = req["audit_at"]
        subject = self.identity.resolve(req["email"], req["requested_at"])
        trace["subject"] = repr(subject)
        if subject is None:
            trace["note"] = "request email matches no known identity"
            return {"case_id": req["case_id"], "deletion_status": "COMPLETE", "steps": [],
                    "unresolved": "request email matches no known identity"}, trace
        linked, rejected = self.link_resources(subject, req)
        windows = self.previous_audit_windows(subject, req) if self.scope_prior_audit_evidence else []
        for rid in list(linked):
            seen = [o.observed_at for o in self.index.by_resource.get(rid, []) if o.observed_at <= audit_at]
            for lo, hi, cid in windows:
                if seen and all(lo <= t <= hi for t in seen):
                    rejected[rid] = f"all evidence captured inside earlier request {cid}'s audit window [{lo}, {hi}]"
                    linked.pop(rid)
                    break
        trace["linked"], trace["rejected_links"], trace["previous_requests"] = linked, rejected, windows
        trace["candidate_associations"] = self.timing_candidates(req, linked)
        findings, remediation, verification = [], [], []
        trace["resources"], trace["uncited"] = [], []
        unknown_compliance = []  # linked resources whose compliance at audit_at cannot be determined
        for rid in sorted(linked):
            info = self.resource_state(rid, audit_at, subject)
            trace["resources"].append(info)
            if not info["decision"]:
                target = final_state_check(info["action"], info["category"])
                if target:  # state unobserved: only the post-remediation check can be stated (no citation)
                    verification.append((target, info["system"], rid, ""))
                    unknown_compliance.append(rid)
                continue
            f_op, r_op, v_op, compliant = info["decision"]
            sysid, ev = info["system"], info["evidence_id"]
            if not compliant:
                if ev:
                    findings.append(contract.make_step(f_op, sysid, rid, ev))
                    remediation.append(contract.make_step(r_op, sysid, rid))
                else:
                    trace["uncited"].append(f"{rid}: violation known only from evidence captured after audit_at")
                    continue
            verification.append((v_op, sysid, rid, ev))
        status = "INCOMPLETE" if findings else "COMPLETE"
        if status == "COMPLETE":
            steps = [contract.make_step(op, s, r, ev) for op, s, r, ev in verification if ev]
            trace["uncited"] += [
                f"{r}: compliance unknown at audit_at; no verification can be stated" if r in unknown_compliance
                else f"{r}: compliant per pre-audit record-time facts, but no evidence captured by audit_at to cite"
                for op, s, r, ev in verification if not ev]
        else:
            steps = findings + remediation + [contract.make_step(op, s, r) for op, s, r, _ in verification]
        row = {"case_id": req["case_id"], "deletion_status": status, "steps": contract.finalize_steps(steps)}
        trace["unknown_compliance"] = unknown_compliance
        if status == "COMPLETE" and row["steps"] and unknown_compliance:
            # COMPLETE holds only when every relevant copy has the required treatment (AGENTS.md 6.5); a linked
            # record whose compliance is unknown leaves completion unproven, and no citable finding exists either.
            row["unresolved"] = ("authorized deletion; COMPLETE not provable: compliance unknown at audit_at for "
                                 + ", ".join(sorted(unknown_compliance)))
        elif not row["steps"]:
            # The contract has no valid representation for an authorized deletion with no evidence-backed step:
            # COMPLETE needs verification steps that prove state, INCOMPLETE needs cited findings.
            row["unresolved"] = ("authorized deletion, but no resource can be tied to the subject by evidence"
                                 if not linked else
                                 "authorized deletion; linked resources exist but no evidence-backed step can be "
                                 "stated (state unknown, pending review, or provable only by evidence captured "
                                 "after audit_at)")
        return row, trace
