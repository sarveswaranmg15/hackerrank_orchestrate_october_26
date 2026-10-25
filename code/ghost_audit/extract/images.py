"""Application screenshots.

No OCR was performed: no OCR engine is installed. Each screenshot is read from an agent-produced visual
transcript at code/evidence_text/<file name>.txt (written and re-checked by Claude Code reading the PNG; not yet
confirmed by a human reviewer). A transcript records the source path and SHA-256 of the image; if the hash
does not match the image on disk the transcript is rejected and the image is reported as unread.
Transcripts are literal: one `row key=value ...` line per on-screen record. Visible UI values are mapped to
state here, in code, so the mapping is reviewable. A row carrying needs_review="..." (or an interpretation flagged
below) is an ambiguous claim: it is kept for diagnostics but excluded from linking and state decisions until a
human confirms the original image.
"""
import hashlib
import os
import re

from ..observations import interpret
from . import ExtractResult
from .logs import _kv

TRANSCRIPT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                              "evidence_text")
ACTIVE_FLOW_STATUS = {"healthy", "active", "running", "live"}


def _row_state(row):
    """Map literal on-screen values to state fields understood by interpret()."""
    row = dict(row)
    system = row.get("system", "")
    status = str(row.get("status", "")).lower()
    if system == "integration_service":
        try:
            events = int(str(row.get("events_per_hour", "0")).replace(",", ""))
        except ValueError:
            events = 0
        if status in ACTIVE_FLOW_STATUS or (str(row.get("mode", "")).lower().startswith("continuous") and events > 0):
            row["enabled"] = True
    if system == "backup_service":
        # A completed restore or restore drill shown for the archive is visible evidence of a working restore path.
        if row.get("last_drill") or "restored" in str(row.get("activity", "")).lower():
            # Not asserted: a drill or past restore suggests, but does not show, that restore is allowed now.
            row["needs_review"] = ("state: restore_allowed=true would be inferred from a restore drill/completed "
                                   "restore; no restore-allowed flag is displayed")
    for key in ("name_shown", "email_shown", "phone_shown"):
        row.pop(key, None)
    for key in ("account", "selector"):  # identifiers displayed in list columns
        if key in row:
            value = row.pop(key)
            row["customer_email" if "@" in str(value) else key] = value
    return row


def extract(path, meta):
    transcript = os.path.join(TRANSCRIPT_DIR, os.path.basename(path) + ".txt")
    if not os.path.exists(transcript):
        return ExtractResult(unread="image: no OCR engine and no transcript")
    with open(transcript, encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(r"^# sha256: ([0-9a-f]{64})", text, re.M)
    with open(path, "rb") as fh:
        actual = hashlib.sha256(fh.read()).hexdigest()
    if not m or m.group(1) != actual:
        return ExtractResult(unread="image: transcript hash does not match the image file")
    method = re.search(r"^# method: (.*)$", text, re.M)
    res = ExtractResult(text=text, provenance=method.group(1).strip() if method else "transcript")
    for line in text.splitlines():
        if not line.startswith("row "):
            continue
        row = _row_state(_kv(line[4:]))
        review = row.pop("needs_review", "")
        if row.get("resource_id"):
            obs = interpret(row, evidence_id=meta["file_id"], captured_at=meta["captured_at"],
                            observed_at=meta["captured_at"], system=row.get("system"),
                            detail=f"screenshot row: {line[4:200]}")
            if obs:
                if obs.present is None:
                    obs.present = True  # a record displayed on screen exists at capture time
                obs.source_kind, obs.needs_review = "screenshot_transcript", review
                obs.review_scope = review.split(":", 1)[0] if review.startswith(("state:", "ownership:")) else (
                    "ownership" if review else "")
                res.observations.append(obs)
        else:
            res.pending.append({"row": row, "text": line[4:], "needs_review": review})
    return res
