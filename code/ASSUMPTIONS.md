# Policy assumptions and known contract gaps

This file lists every decision the pipeline makes that is **not** stated explicitly in the written challenge
contract (`problem_statement.md`, `README.md`, `AGENTS.md`). Each one is labelled, configurable where noted, and
covered by regression tests in `code/tests/test_regressions.py`.

## 1. Time semantics (interpretation of the contract)

| Time | Source | Used for |
|---|---|---|
| Record time | row / log line / snapshot `captured_at` / `observed_at` | What was true at `audit_at` (facts) |
| File capture time | `evidence_files.csv` `captured_at` | What may be cited: no step cites a file captured after `audit_at` |
| Activity `event_time` | `system_activity.csv` | Ordering workflow claims only; activity results are never proof of state |

A record observed at or before `audit_at` inside a file captured later is treated as a fact. It is used in
`INCOMPLETE` verification steps, which cite nothing, but it is never cited.

**State reconstruction.** Observations of a record are replayed in record-time order. Observations sharing a
timestamp are merged, so a copy present in any replica counts as present.

* An explicit value replaces the previous one; for example, a redaction sets exposure to `False`.
* A silent attribute (`None`) keeps the last known value. A newer row that does not show identifier fields
  therefore cannot erase an earlier, citable exposure.
* An explicit absence resets the record. Attributes seen before a deletion are never carried onto a later
  re-creation. Citations come only from observations since the start of the current absence or presence run.

## 2. What is not proof

* **Absence from a snapshot.** No capture establishes complete coverage: inventories record only
  `snapshot_completed_at`, and SQL dumps contain a handful of rows. A record missing from a snapshot is therefore
  never treated as deleted.
* **Failed lookups.** 5xx, 403 and timeout responses prove nothing. Only a completed read (HTTP 404/410 or
  `rows=0`) counts as absence.
* **Negative lookups before the request.** A lookup that found nothing, made before the request's `requested_at`,
  does not tie a resource to the subject.
* **Workflow results** (`reported_status`, activity `reported_result`) are claims.

## 3. `SCOPE_PRIOR_AUDIT_EVIDENCE` (evidence-supported policy assumption; enabled)

*Not an explicit contractual requirement.*

If every observation of a record lies inside an **earlier, separate** deletion request's audit window
`[requested_at, audit_at]` by the **same person**, that evidence was gathered for the earlier request. The
record is then excluded from the later request's audit.

* Supporting evidence: in the public samples, case_07's `object_19` (seen only 2026-02-13, inside case_61's
  window [02-11, 02-14]) and case_17's `tombstone_17` (seen only 2026-03-10, inside case_46's window
  [03-08, 03-11]) are absent from the expected outputs. In the evaluation set the rule changes one case
  (case_33, `export_1260`, seen only 2026-06-18, inside case_52's window [06-16, 06-19]).
* Request history is read only from `dataset/deletion_requests.csv`, which holds inputs. Sample files and
  labels are never read.
* Configure: `Auditor(scope_prior_audit_evidence=False)` or `python3 code/main.py --no-prior-audit-scoping`.

## 4. Data-catalog activity (`LINK_RESOURCE_IDENTITY`)

* It is used to map an alias shown in a capture taken at most 1 hour earlier to a resource id, for example
  `NST-2026-0718-1842` to `export_1842`.
* Its identity assertion is a lookup-style claim. It never overrides a row that names a different person
  (`marketing_001` shows `sj.park@naver.com`, so the claim that it belongs to `kwame-mensah@proton.me` is
  rejected).

## 5. Activity-timing associations (labelled candidates, never proof)

Deletion-worker activity inside a request's window `[requested_at, reported_at + 2h]` is recorded in the trace as
`candidate_associations` with the basis "timing only". Such candidates never produce steps and never affect status.
Windows overlap: on the public samples a unique candidate was wrong in 1 of 3 cases.

## 6. Screenshot transcripts

* **No OCR was performed.** The 11 PNGs were transcribed visually by Claude Code (claude-opus-5-5) into
  `code/evidence_text/*.txt`, one file per image.
* Each transcript records its source path and SHA-256. A mismatch makes the image "unread".
* Each transcript also carries audit notes, corrections and ambiguity notes.
* Observations from transcripts carry `source_kind = "screenshot_transcript"`. The trace reports
  `evidence_basis`, which distinguishes screenshot-only conclusions from corroborated ones.
* **Ambiguous claims are not confirmed.** Rows or interpretations marked `needs_review` are excluded from
  linking and state decisions until a human confirms the original image. Currently flagged:
  * `backup_9287` / `backup_9286` (scope: state): restore-allowed is NOT asserted. No restore flag is displayed,
    the drill (29 Mar) predates the archive's creation (26 Apr), and the 25 Apr restore predates `backup_9286`'s
    listed creation. Reviewer decision (2026-10-24): not proof of restorability at case_55's audit; later
    (Aug/Sep) evidence is not used to reconstruct the April state. case_55 remains unresolved.
    restore-allowed flag is shown.
  * `support_9538` (scope: ownership): tied to `user_11` only by placement on that customer's page. Reviewer
    decision: rejected as ownership evidence. The record is linked only through `evidence_01` (an attachment PII
    scan matching Haruka Sato's historical phone, `audit_279`).

## 7. Known contract gap: authorized deletion with no linkable resource

The written contract defines no representation for an authorized deletion request when no resource can be
tied to the subject by evidence:

* `NOT_APPLICABLE` + `[]` is reserved for requests that do not authorize deletion;
* `COMPLETE` must contain verification steps proving the state at `audit_at`;
* `INCOMPLETE` must contain findings that cite evidence.

No fallback is documented in `problem_statement.md`, `README.md`, `AGENTS.md`, `agent_trace_instructions.md`,
`dataset/output.csv` or `dataset/sample_output.csv`, and no official validator is shipped in the repository.

The same applies when a case would be `COMPLETE` but a linked, relevant record has unknown compliance at
`audit_at`, for example `tombstone_17` in case_46, whose screenshot shows no identifier fields. Completion is
then unproven, and no citable finding exists either. A record whose compliance *is* established by pre-audit
record-time facts but cannot be cited is omitted from `COMPLETE` and listed under `uncited` in the trace.

Such rows are marked `unresolved` in the trace. `code/main.py` refuses to write `output.csv` while any exist,
unless `--allow-unresolved` is passed. The contract validator is not relaxed, so it reports these rows as
`COMPLETE case has no steps`.

## 8. Conditional rules: `rule_06` (support attachment, "belongs only to the deletion subject")

Reviewer decision (2026-10-24): a confirmed identifier match is not proof of exclusive data ownership.

* **Exclusivity established.** `rule_06` DELETE is applied when one of the following holds:
  * the evidence states exclusivity (catalog "belongs only to … no other subject is linked"); or
  * a non-scan record typed `SUPPORT_ATTACHMENT` names only the subject. This covers a single-owner record
    attribution, or the attachment's full content when it is visible.
* **Exclusivity not established.** This covers a PII-scan match only (`attachment_pii_scan`), optionally
  alongside an untyped ticket row. Whole-record deletion is not asserted. Every applicable rule requires removing
  the subject's identifiers (rule_06 deletes the record; shared support content is redacted under rule_05), so
  the narrowest supported treatment is used: `REPORT_EXPOSED_PII` → `REDACT_RECORD` → `VERIFY_RECORD_REDACTED`.
  The trace's `policy_note` records which branch was taken and why.
* Effect: the public samples are unchanged (case_07, case_13). Seven evaluation cases move from
  delete to redact: case_34, case_71, case_119, case_263, case_371, case_419 and case_515.
* Records in the `ticket_attachments` collection (JSON bundles and SQL `record_json` rows) are categorized
  `SUPPORT_ATTACHMENT`.
* `uploaded_by` counts as an exposed identifier only when it holds an email or phone. Whose identifier it is
  remains a linking decision: an uploader who is not the subject neither links the record to the subject nor
  establishes exclusivity.
* An attachment record whose only named person is the subject as uploader is treated as a single-owner
  attribution, so rule_06 applies. This uses the same criterion as the sample handover row (case_13).

## 9. Public-sample conventions considered and not adopted (no compatibility mode)

The 27 gold sample steps the pipeline does not reproduce were analysed on the scoring side only; the gold file
never enters the prediction path.

* No evidence row contains the subject's identifiers for any of these resources. The gold's link from resource to
  case therefore comes from deletion-worker activity timing.
  * 11 of the steps cite a file that contains both the subject and the resource. These are multi-subject exports
    (for example `evidence_92`), so the co-occurrence does not identify the owner.
  * 4 cite a file that does not mention the resource at all (`evidence_54`, `evidence_51`), which treats snapshot
    absence as proof.
  * 12 cite nothing.
* Every sample backup and tombstone decision rests on an explicit pre-audit flag (`restore_allowed` / exposed).
  No sample resolves an *unknown* restore or exposure state.

The only consistent alternative the samples imply is "activity timing identifies the requester's resources and
snapshot absence proves deletion". That is identity from timing alone, which this project excludes, and it would
not resolve case_46 or case_55 in any case. No sample-compatible mode is implemented.

## 10. Submission decision for unresolved rows, and the packaging of transcripts (2026-10-24)

* **Unresolved rows.** No organizer clarification exists. The submitted `output.csv` was generated with
  `--allow-unresolved`, the least-risk documented fallback: each unresolved authorized request is emitted as
  `COMPLETE` with only the evidence-backed verification steps that exist. This affects case_46 (1 cited step;
  `tombstone_17` compliance unknown), case_55 (`[]`) and case_63 (`[]`).
  * This breaks no literal contract clause, but it does not establish successful deletion for those requests.
  * Every alternative breaks explicit text: an `INCOMPLETE` row without findings, `NOT_APPLICABLE` for a deletion
    request, or a missing row.
  * The validator is unchanged and reports case_55 and case_63.
* **Transcripts.** `code/evidence_text/` is included in `code.zip`, because the program needs it to read the 11
  screenshots. Without it, 10 evaluation rows change. The packaging rules forbid the `dataset/` directory, real
  personal data, secrets, caches and nested archives. The transcripts are none of these: they are derived text
  from participant-facing evidence about fictional people.
