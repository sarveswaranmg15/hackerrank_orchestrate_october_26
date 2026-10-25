# Usage report: Ghost Data Still Exists

This report covers the full-dataset run that produced the submitted `output.csv`:

* Command (repository root): `python3 code/main.py --allow-unresolved`
* Run: 2026-10-24T18:23:34Z, Python 3.14.3, macOS
* Wall-clock time: 0.54 s (`/usr/bin/time -p`: real 0.54 s, user 0.46 s, sys 0.03 s)
* Output: 518 data rows; `output.csv` SHA-256 `faa9abc798e829ef44235b952bcbe768ddfab3b7892cf3a4ab392ba6373acfca`
* The run printed 2 contract-validation errors for documented deviations (case_55, case_63). See
  `evaluation_report.md` §6. The process exit code was 0, because `--allow-unresolved` does not turn
  validation errors into a failing exit code.

## Model usage in the final run

The pipeline is deterministic and uses only the Python standard library. Its imports were checked: argparse,
base64, collections, csv, dataclasses, datetime, hashlib, importlib, json, os, re, shutil, sqlite3, sys, tempfile
and zlib. There is no network or model client. Request intent, identity resolution, evidence extraction, policy
evaluation and step generation are all rule-based, so the run made **no model calls**.

| Provider | Model | Model calls | Input tokens | Output tokens | Total tokens | Avg tokens / request (518) | Est. total cost | Est. cost / request |
|---|---|---|---|---|---|---|---|---|
| none | none | 0 | 0 | 0 | 0 | 0 | $0.00 | $0.00 |
| **Overall** | | **0** | **0** | **0** | **0** | **0** | **$0.00** | **$0.00** |

## Development-time model use (not part of the run above; disclosed for transparency)

* The solution was developed with Claude Code, model `claude-opus-5-5`. The development transcript is in
  `log.txt` and in the separately submitted agent trace.
* No OCR engine was available. The 11 screenshot evidence files were therefore transcribed visually by Claude
  Code (`claude-opus-5-5`) during development, into `code/evidence_text/*.txt`. Each transcript records its
  source path, SHA-256 and method, and the run reads these transcripts instead of calling a model.
* Development token counts and costs were **not metered** by this project, so none are reported here. They can be
  derived from the agent-trace session file.
