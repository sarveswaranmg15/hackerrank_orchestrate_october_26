"""Reproducible evaluator: run the audit pipeline on the public samples and score it against sample_output.csv.

Usage (from the repository root):
    python3 code/evaluation/main.py                    # run pipeline on samples and score
    python3 code/evaluation/main.py --pred FILE.csv    # score an existing prediction file
    python3 code/evaluation/main.py --show 10          # print up to 10 mismatching cases

The gold file is read only here, for scoring. The prediction pipeline never reads sample_output.csv.
"""
import argparse
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(CODE)
sys.path.insert(0, CODE)

from ghost_audit import contract  # noqa: E402
from ghost_audit.loaders import Dataset  # noqa: E402

FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def load(path):
    header, rows = contract.read_output(path)
    return header, {r["case_id"]: r for r in rows}


def step_key(s, with_evidence=True):
    k = tuple(s[f] for f in FIELDS if with_evidence or f != "evidence_id")
    return k


def prf(tp, n_pred, n_gold):
    p = tp / n_pred if n_pred else (1.0 if not n_gold else 0.0)
    r = tp / n_gold if n_gold else (1.0 if not n_pred else 0.0)
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def score(gold, pred):
    m = Counter()
    field_hits, field_total = Counter(), 0
    status_conf = Counter()
    mismatches = []
    for cid in sorted(gold, key=contract.case_sort_key):
        g = gold[cid]
        p = pred.get(cid)
        gs = json.loads(g["steps_json"])
        ps = json.loads(p["steps_json"]) if p else []
        pstatus = p["deletion_status"] if p else "MISSING"
        status_conf[(g["deletion_status"], pstatus)] += 1
        m["cases"] += 1
        m["status_ok"] += g["deletion_status"] == pstatus
        for with_ev, tag in ((True, "step"), (False, "step_noev")):
            gk = Counter(step_key(s, with_ev) for s in gs)
            pk = Counter(step_key(s, with_ev) for s in ps)
            m[f"{tag}_tp"] += sum((gk & pk).values())
            m[f"{tag}_gold"] += sum(gk.values())
            m[f"{tag}_pred"] += sum(pk.values())
        # Field-level: positional alignment by step_no over the longer of the two sequences.
        n = max(len(gs), len(ps))
        field_total += n
        for i in range(n):
            for f in FIELDS:
                if i < len(gs) and i < len(ps) and gs[i][f] == ps[i][f]:
                    field_hits[f] += 1
        exact = [step_key(s) for s in gs] == [step_key(s) for s in ps] and g["deletion_status"] == pstatus
        m["exact"] += exact
        if not exact:
            mismatches.append((cid, g, p, gs, ps))
    return m, field_hits, field_total, status_conf, mismatches


def report(m, field_hits, field_total, status_conf):
    n = m["cases"]
    lines = [f"cases scored: {n}",
             f"case-level status accuracy: {m['status_ok']}/{n} = {m['status_ok'] / n:.3f}"]
    for tag, label in (("step", "step-level (all fields incl. evidence_id)"),
                       ("step_noev", "step-level (ignoring evidence_id)")):
        p, r, f = prf(m[f"{tag}_tp"], m[f"{tag}_pred"], m[f"{tag}_gold"])
        lines.append(f"{label}: matched {m[f'{tag}_tp']} | gold {m[f'{tag}_gold']} | pred {m[f'{tag}_pred']}"
                     f" | precision {p:.3f} recall {r:.3f} F1 {f:.3f}")
    lines.append(f"field-level accuracy (positional, over {field_total} step slots):")
    for f in FIELDS:
        lines.append(f"    {f:12s} {field_hits[f]}/{field_total} = {field_hits[f] / field_total if field_total else 0:.3f}")
    tot = sum(field_hits.values())
    lines.append(f"    {'all fields':12s} {tot}/{field_total * len(FIELDS)} = "
                 f"{tot / (field_total * len(FIELDS)) if field_total else 0:.3f}")
    lines.append(f"exact-sequence accuracy (status + full step list): {m['exact']}/{n} = {m['exact'] / n:.3f}")
    lines.append("status confusion (gold -> pred): " + ", ".join(
        f"{g}->{p}: {c}" for (g, p), c in sorted(status_conf.items())))
    return "\n".join(lines)


class Availability:
    """Scoring-side diagnostic: where does a resource id appear in participant-visible data?"""

    def __init__(self, ds):
        import re
        self.ds = ds
        self.texts = {}
        for m in ds.manifest:
            path = os.path.join(ds.dir, m["file_path"])
            if path.endswith((".png", ".jpg")):
                t = os.path.join(CODE, "evidence_text", os.path.basename(path) + ".txt")
                path = t if os.path.exists(t) else None
            if path is None:
                continue
            if path.endswith(".pdf"):
                from ghost_audit.extract.pdf import pdf_text
                self.texts[m["file_id"]] = pdf_text(path)
            else:
                self.texts[m["file_id"]] = open(path, encoding="utf-8", errors="replace").read()
        self.activity = {a["resource_id"] for a in ds.activity} | {
            r for a in ds.activity for r in re.findall(r"\b\w+_\d+\b", a["details"])}
        self.re = re

    def classify(self, rid):
        pat = self.re.compile(r"\b" + self.re.escape(rid) + r"\b")
        files = sorted(f for f, t in self.texts.items() if pat.search(t))
        if files:
            return f"in evidence {','.join(files[:4])}"
        if rid in self.activity:
            return "activity stream only (no evidence row names the subject)"
        return "not in any participant-visible data"


def show_mismatch(cid, g, p, gs, ps, availability=None):
    out = [f"--- {cid}: gold {g['deletion_status']} vs pred {p['deletion_status'] if p else 'MISSING'}"]
    gset, pset = Counter(step_key(s) for s in gs), Counter(step_key(s) for s in ps)
    for k in (gset - pset):
        where = f"   [{availability.classify(k[2])}]" if availability else ""
        out.append("   missing  " + " ".join(k) + where)
    for k in (pset - gset):
        out.append("   extra    " + " ".join(k))
    if not (gset - pset) and not (pset - gset):
        out.append("   same steps, different order")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=os.path.join(REPO_ROOT, "dataset"))
    ap.add_argument("--pred", help="score this prediction CSV instead of running the pipeline")
    ap.add_argument("--out", default=os.path.join(HERE, "sample_predictions.csv"))
    ap.add_argument("--show", type=int, default=8, help="number of mismatching cases to print")
    args = ap.parse_args(argv)
    ds = Dataset(args.dataset)
    if not args.pred:
        sys.path.insert(0, CODE)
        from main import run
        # Unresolved rows are written so they are scored and flagged by the contract validator, not hidden.
        run(args.dataset, "sample_deletion_requests.csv", args.out, allow_unresolved=True)
        args.pred = args.out
    header, pred = load(args.pred)
    _, gold = load(os.path.join(args.dataset, "sample_output.csv"))
    errors = [] if header == contract.OUTPUT_COLUMNS else [f"bad header {header}"]
    errors += contract.validate_rows(list(pred.values()), list(gold), ds.known_evidence_ids())
    print(f"contract validation: {'PASS' if not errors else f'FAIL ({len(errors)} errors)'}")
    for e in errors[:20]:
        print("  " + e)
    m, fh, ft, conf, mismatches = score(gold, pred)
    print(report(m, fh, ft, conf))
    if mismatches and args.show:
        availability = Availability(ds)
        print(f"\nrepresentative mismatches ({min(args.show, len(mismatches))} of {len(mismatches)}):")
        for mm in mismatches[: args.show]:
            print(show_mismatch(*mm, availability=availability))
    return 0


if __name__ == "__main__":
    sys.exit(main())
