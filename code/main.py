"""Ghost Data Still Exists - deletion audit agent.

Usage (from the repository root):
    python3 code/main.py                                  # audit dataset/deletion_requests.csv -> output.csv
    python3 code/main.py --requests sample_deletion_requests.csv --out /tmp/sample_pred.csv
    python3 code/main.py --explain case_42                # print the evidence trail for one case
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ghost_audit import contract  # noqa: E402
from ghost_audit.loaders import Dataset  # noqa: E402
from ghost_audit.pipeline import Auditor  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(dataset_dir, requests_file, out_path=None, only=None, allow_unresolved=False,
        scope_prior_audit_evidence=True):
    ds = Dataset(dataset_dir)
    auditor = Auditor(ds, scope_prior_audit_evidence=scope_prior_audit_evidence)
    requests = ds.requests(requests_file)
    if only:
        requests = [r for r in requests if r["case_id"] in only]
    rows, traces = [], []
    for req in requests:
        row, trace = auditor.audit(req)
        rows.append(row)
        traces.append(trace)
    unresolved = [r for r in rows if r.get("unresolved")]
    if unresolved:
        print(f"{len(unresolved)} case(s) have no valid contract representation:", file=sys.stderr)
        for r in unresolved:
            print(f"  {r['case_id']}: {r['unresolved']} (emitted as {r['deletion_status']} with "
                  f"{len(r['steps'])} step(s))", file=sys.stderr)
        if out_path and not allow_unresolved:
            print("refusing to write output: review these cases or pass --allow-unresolved", file=sys.stderr)
            out_path = None
    if out_path:
        contract.write_output(out_path, rows)
        header, written = contract.read_output(out_path)
        errors = [] if header == contract.OUTPUT_COLUMNS else [f"bad header {header}"]
        errors += contract.validate_rows(written, [r["case_id"] for r in requests], ds.known_evidence_ids())
        if errors:
            print(f"CONTRACT VALIDATION FAILED ({len(errors)} errors):", file=sys.stderr)
            for e in errors[:50]:
                print("  " + e, file=sys.stderr)
        else:
            print(f"wrote {len(written)} rows to {out_path}; contract validation passed")
    if auditor.index.unread:
        print(f"{len(auditor.index.unread)} evidence files could not be interpreted:", file=sys.stderr)
        for fid, (path, why) in sorted(auditor.index.unread.items()):
            print(f"  {fid} {path}: {why}", file=sys.stderr)
    return rows, traces


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=os.path.join(REPO_ROOT, "dataset"))
    ap.add_argument("--requests", default="deletion_requests.csv", help="request CSV inside --dataset")
    ap.add_argument("--out", default=os.path.join(REPO_ROOT, "output.csv"))
    ap.add_argument("--explain", nargs="+", metavar="CASE_ID", help="print the evidence trail; no file written")
    ap.add_argument("--no-prior-audit-scoping", action="store_true",
                    help="disable the SCOPE_PRIOR_AUDIT_EVIDENCE policy assumption (see code/ASSUMPTIONS.md)")
    ap.add_argument("--allow-unresolved", action="store_true",
                    help="write the file even if some authorized requests have no evidence-backed step")
    args = ap.parse_args(argv)
    if args.explain:
        scope = not args.no_prior_audit_scoping
        rows, traces = run(args.dataset, args.requests, None, set(args.explain), scope_prior_audit_evidence=scope)
        if not rows:
            requests = "sample_deletion_requests.csv"
            rows, traces = run(args.dataset, requests, None, set(args.explain), scope_prior_audit_evidence=scope)
        for row, trace in zip(rows, traces):
            print(json.dumps(trace, indent=2, default=str))
            print(json.dumps(row, indent=2))
        return 0
    rows, _ = run(args.dataset, args.requests, args.out, allow_unresolved=args.allow_unresolved,
                  scope_prior_audit_evidence=not args.no_prior_audit_scoping)
    return 1 if any(r.get("unresolved") for r in rows) and not args.allow_unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
