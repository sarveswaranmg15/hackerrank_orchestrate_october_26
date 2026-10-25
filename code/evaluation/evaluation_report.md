# Evaluation report: Ghost Data Still Exists

Status: **final run, with documented contract deviations.** `output.csv` (518 rows) was generated with
`python3 code/main.py --allow-unresolved`. It is **not fully validator-clean**: 3 evaluation cases have no valid
representation under the written contract and are emitted with an evidence-only fallback (see §6). The fallback
does **not** establish successful deletion for those cases.

All numbers below come from actual runs on 2026-10-24, Python 3.14.3, standard library only, with no model calls.

## How to reproduce

```bash
python3 code/evaluation/main.py                  # 32 public samples: contract validation + scores + mismatches
python3 code/evaluation/custom_cases.py          # 4 participant-created synthetic cases
python3 -X dev -m unittest discover -s code/tests   # 51 regression tests
```

The public-sample gold file (`dataset/sample_output.csv`) is read only by the scorer. The prediction code
(`code/ghost_audit/`, `code/main.py`) never reads it or any sample file, and a regression test enforces this.

## 1. Public samples (32 cases)

| Metric | Result |
|---|---|
| Case-level `deletion_status` accuracy | **32/32 = 1.000** |
| Step-level, all fields incl. `evidence_id` | precision **1.000**, recall **0.860**, F1 **0.925** (166 of 193 gold steps; 166 predicted) |
| Step-level, ignoring `evidence_id` | precision 1.000, recall 0.860, F1 0.925 |
| Exact-sequence accuracy (status + full ordered step list) | **12/32 = 0.375** |
| Contract validation of the sample predictions | **FAIL**: 3 errors (case_01, case_02, case_04: `COMPLETE case has no steps`) |

Field-level accuracy, comparing steps by position over 193 step slots:

| phase | system_id | resource_id | operation | state | evidence_id | all fields |
|---|---|---|---|---|---|---|
| 0.860 | 0.648 | 0.648 | 0.699 | 0.699 | 0.860 | 0.736 |

Positional comparison understates field quality: one missing step shifts every later step in the case. All 27
missing steps are verification steps, and the pipeline produces no extra or wrong steps.

### Remaining sample mismatches (27 missing steps, 0 extra)

| Cause | Steps | Examples |
|---|---|---|
| Resource tied to the case only by deletion-worker activity timing; every evidence row is a 404 or has null identifiers | 21 | accounts for cases 01, 02, 07, 08, 10, 18, 20–23, 25, 28–32; anonymized analytics for 21, 25–28 |
| Resource identified only in evidence captured after `audit_at` | 1 | `account_3866` (case_27) |
| Resource not present in any participant-visible data | 3 | `audit_record_6645`, `audit_record_5569`, `account_004` |
| Anonymized record with all identifiers null and no other link | 2 | `analytics_9406` (case_03), `analytics_901` (case_04) |

These are evidence or specification limits, not code defects. The pipeline deliberately does not treat timing or
snapshot absence as proof (see `code/ASSUMPTIONS.md`). Where the sample outputs appear to rely on those
inferences, a clarification has been drafted for the organizers. It has not been sent, and no official answer
exists.

## 2. Participant-created evaluation cases (`code/evaluation/custom_cases.py`)

These run on a fictional mini-dataset built at run time. The expected outputs were derived by hand from the
written contract, not from the samples.

| Case | What it tests | Result |
|---|---|---|
| custom_01 | Negated deletion ("please don't delete anything") with another purpose gives `NOT_APPLICABLE`, `[]` | PASS |
| custom_02 | Request email is a historical identifier from `user_audit_log.csv`; a completed 404 readback in the request window plus a remaining search entry gives `INCOMPLETE` with a cited finding | PASS |
| custom_03 | A failed (503) lookup must not link a resource; a capture taken after `audit_at` that shows the account again must be ignored; the result is `COMPLETE` with pre-audit citations | PASS |
| custom_04 | Active data flow + restorable backup: correct finding/remediation/verification order; a look-alike decoy (`dana.jr@`) found with the subject's lookup key must not be linked | PASS |

Totals: status 4/4, step precision/recall/F1 1.000/1.000/1.000 (13/13), exact-sequence 4/4, and contract
validation PASS. A mutation check confirmed the suite catches the failed-lookup defect: with that fix removed,
custom_03 becomes unresolved and the regression test fails.

## 3. Full evaluation set: the submitted `output.csv` (518 cases)

Generated 2026-10-24T18:23:34Z by `python3 code/main.py --allow-unresolved` (exit code 0; see §6). The written file
was checked independently:

* Header `case_id,deletion_status,steps_json`; 518 unique data rows equal to the request IDs, sorted numerically
  (case_33 … case_550); LF line endings.
* All 518 `steps_json` values are valid JSON arrays with the exact step fields and consecutive `step_no`
  (2,862 steps in total).
* No step cites a missing ID or evidence captured after its case's `audit_at`.
* Status distribution: 353 `INCOMPLETE`, 155 `COMPLETE`, 10 `NOT_APPLICABLE`. The 10 match the 10 non-deletion
  request texts.
* Contract validator on the written file: **2 errors**: `case_55: COMPLETE case has no steps` and
  `case_63: COMPLETE case has no steps`. The other 516 rows pass. case_46 passes the validator but is still a
  documented deviation (§6).
* `python3 code/main.py` without `--allow-unresolved` refuses to write and exits 1.

## 4. Failures found and improvements made

| Stage | Change | Effect (actual runs) |
|---|---|---|
| Baseline | First end-to-end pipeline | status 31/32; step P/R/F1 0.987/0.808/0.889; all-field 0.675; exact 8/32; 3 contract errors |
| Time semantics | Record time decides facts; file capture time decides citations | Security-audit records in later-captured bundles found (cases 09, 23, 24, 29, 30) |
| Snapshot absence | A record missing from a partial snapshot is no longer proof of deletion | case_05 correctly `INCOMPLETE` (failover-replica copy remains) |
| Failed lookups | 5xx/403 never mean absence; later, failed lookups never link (custom_03) | Rule gaps closed; 0 real rows changed by the linking part |
| Lifecycle | Negative lookups before `requested_at` don't link; evidence captured only inside an earlier request's window is out of scope (configurable policy) | Extra steps removed in case_07 and case_17 |
| Screenshots | No OCR available: hash-checked visual transcripts with provenance; ambiguous claims need review | 0 unreadable images; 2 ambiguous claims excluded |
| Catalog claims | Catalog alias/identity claims never override a row naming another person | `marketing_001` not linked to Kwame Mensah (contradicted by `evidence_60`) |
| rule_06 | Whole-attachment deletion only with exclusive ownership; PII-scan matches lead to redaction | 7 eval attachments handled conservatively |
| Attachment evidence | `ticket_attachments` category, `uploaded_by` identifiers, and silent newer rows no longer erase earlier exposure | case_179 resolved by pre-audit evidence; 13 eval rows corrected |
| COMPLETE proof | `COMPLETE` requires known compliance of every linked record | case_46 no longer claims an unproven `COMPLETE` |
| Current | | status 32/32; step P/R/F1 1.000/0.860/0.925; all-field 0.736; exact 12/32 |

The regression suite has grown with these fixes to 51 tests (`code/tests/test_regressions.py`).

## 5. Do the public samples imply an alternative interpretation?

We analysed this on the scoring side only (see `code/ASSUMPTIONS.md` §9). For the resources the pipeline does not
reproduce, the samples link resource to case only through deletion-worker activity timing (12 of the 27 steps have
no citation; 4 cite a snapshot that does not list the resource). Every sample backup and tombstone decision cites
an explicit pre-audit state flag. Timing-only identity is excluded by this project's rules, and no sample resolves
an unknown state, so **no sample-compatible mode is justified or implemented.**

## 6. Documented contract deviations in `output.csv`

These three authorized deletion requests have no representation that is both valid under the written contract
and honest about the evidence. In each case `COMPLETE` would be unproven, `INCOMPLETE` has no citable finding,
`NOT_APPLICABLE` does not apply, and omitting the row breaks "one row per request". They are emitted with the
least-risk fallback: `COMPLETE` with **only** the evidence-backed verification steps that exist. **This fallback
does not establish successful deletion for these cases, and the output is not fully validator-clean.**

| Case | Emitted | Limitation |
|---|---|---|
| case_46 | `COMPLETE`, 1 cited step (`VERIFY_RECORD_ABSENT account_6523`, `evidence_67`) | `tombstone_17`'s compliance remains unknown. Its screenshot (`evidence_42`) shows no identifier fields, which is not proof of redaction. The validator does not flag this row. |
| case_55 | `COMPLETE`, `[]` | No valid evidence-backed finding or verification step can be produced. `backup_9287` belongs to the requester (`evidence_32`), but no evidence captured by `audit_at` states its restore state, and later evidence is excluded. |
| case_63 | `COMPLETE`, `[]` | The requester cannot be reliably identified. No evidence row contains their identifiers, and the only catalog claim (`activity_4008`) is contradicted by `evidence_60`. |

The validator was not changed. The rule these rows break ("`COMPLETE` case has no steps") is this project's
reading of "verification steps proving the current state", not literal contract text. Public samples case_01,
case_02 and case_04 fail the same rule for the same underlying reason. A clarification request for the organizers
has been drafted but not sent, and no organizer answer exists.
