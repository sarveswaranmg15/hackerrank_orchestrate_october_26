"""Read-only submission readiness check. It never creates or modifies files or archives.

Mirrors the README packaging command (zip -r code.zip README.md code, excluding caches, virtualenvs and
.DS_Store), then checks required deliverables, prohibited content, secrets patterns, interim markers, and, if
present, the root output.csv against the output contract.

Usage (from the repository root):  python3 code/package_check.py      # exit 0 only when submission-ready
"""
import os
import re
import sys

CODE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(CODE)
sys.path.insert(0, CODE)

EXCLUDED_DIRS = {"__pycache__", ".venv", "venv", "node_modules", ".git"}
EXCLUDED_FILES = {".DS_Store"}
ARCHIVE_EXT = (".zip", ".tar", ".gz", ".tgz", ".7z", ".rar")
REQUIRED_IN_ZIP = ["README.md", "code/main.py", "code/evaluation/evaluation_report.md",
                   "code/evaluation/usage_report.md"]
SECRET_PATTERNS = [re.compile(p) for p in (
    r"sk-[A-Za-z0-9_-]{16,}", r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)(api[_-]?key|secret|access[_-]?token|password)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]")]


def zip_file_list(root=REPO_ROOT):
    files = ["README.md"] if os.path.exists(os.path.join(root, "README.md")) else []
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, "code")):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        for f in sorted(filenames):
            if f in EXCLUDED_FILES or f.endswith(".pyc"):
                continue
            files.append(os.path.relpath(os.path.join(dirpath, f), root))
    return files


def check(root=REPO_ROOT):
    """Return (problems, warnings, files): problems block submission; warnings need a human decision."""
    problems, warnings = [], []
    files = zip_file_list(root)
    for req in REQUIRED_IN_ZIP:
        if req not in files:
            problems.append(f"missing from code.zip: {req}")
    for f in files:
        low = f.lower()
        if low.startswith("dataset/") or "/dataset/" in low:
            problems.append(f"prohibited (dataset) in code.zip: {f}")
        if low.endswith(ARCHIVE_EXT):
            problems.append(f"nested archive in code.zip: {f}")
        if os.path.basename(low).startswith(".env"):
            problems.append(f"environment file in code.zip: {f}")
        try:
            with open(os.path.join(root, f), encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                problems.append(f"possible secret ({pat.pattern}) in {f}")
    for rep in ("code/evaluation/evaluation_report.md", "code/evaluation/usage_report.md"):
        path = os.path.join(root, rep)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                if re.search(r"(?i)\binterim\b", fh.read()):
                    problems.append(f"{rep} is still marked interim")
    if any(f.startswith("code/evidence_text/") for f in files):
        warnings.append("code/evidence_text/ is included: fictional dataset-derived screenshot transcripts that the "
                        "pipeline needs to read screenshots (no OCR); confirm inclusion")
    if not os.path.exists(os.path.join(root, "log.txt")):
        problems.append("log.txt (chat_transcript) missing")
    problems.extend(_check_output(root))
    warnings.append("agent-trace.zip is produced outside the repository after the session ends "
                    "(agent_trace_instructions.md); not checked here")
    return problems, warnings, files


def _check_output(root):
    out = os.path.join(root, "output.csv")
    if not os.path.exists(out):
        return ["output.csv not generated (root)"]
    from ghost_audit import contract
    from ghost_audit.loaders import Dataset
    ds = Dataset(os.path.join(root, "dataset"))
    header, rows = contract.read_output(out)
    errors = [] if header == contract.OUTPUT_COLUMNS else [f"output.csv bad header {header}"]
    errors += contract.validate_rows(rows, [r["case_id"] for r in ds.requests()], ds.known_evidence_ids())
    return [f"output.csv: {e}" for e in errors[:20]] + (
        [f"output.csv: {len(errors) - 20} more errors"] if len(errors) > 20 else [])


def main():
    problems, warnings, files = check()
    print(f"code.zip would contain {len(files)} files (README.md + code/, caches excluded)")
    for w in warnings:
        print("  WARNING: " + w)
    for p in problems:
        print("  BLOCKER: " + p)
    print("submission-ready: " + ("YES" if not problems else "NO"))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
