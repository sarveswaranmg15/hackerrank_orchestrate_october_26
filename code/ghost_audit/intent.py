"""Decide whether a request text actually authorizes deletion (multilingual, negation-aware rules)."""
import re

# Deletion verbs/nouns per language. Matches must not be negated to count.
DELETION = [
    r"\bdelet\w*", r"\beras\w*", r"\bremov\w*", r"\bclos(e|ing)\b", r"\bpurg\w*", r"\bwip(e|ing)\b",
    r"\bsupprim\w*", r"\bsuppression\b", r"\beffac\w*",                      # fr
    r"\belimin\w*", r"\bborr(ar|en|ado)\w*", r"\bsuprim\w*",                 # es
    r"\bexclus[aã]o\b", r"\bexclu(ir|am)\b", r"\bapag\w*",                   # pt
    r"\bl[öo]sch\w*",                                                         # de
    r"削除", r"消去", r"삭제", r"탈퇴",                                          # ja / ko
    # Requests to confirm an earlier deletion also authorize the deletion audit.
    r"\b(is|was|are|were) gone\b", r"\bcannot identify me\b", r"\bno longer (exists?|identif\w*)\b",
]
# What the deletion must target for it to be an account/personal-data deletion.
TARGET = [
    r"\baccount\b", r"\bprofile\b", r"\bpersonal\b", r"\bdata\b", r"\brecords?\b", r"\binformation\b",
    r"\bcompte\b", r"\bcuenta\b", r"\bconta\b", r"\bkonto\b", r"アカウント", r"계정", r"\bmy details\b",
]
NEGATION = re.compile(
    r"(\bnot\b|\bno\b|n't\b|\bnever\b|\bwithout\b|\bdo not\b|\bdon't\b|\bne\b|\bpas\b|\bnão\b|\bnicht\b|しない|않)"
    r"[^.?!]{0,30}$", re.I)
# Statements that the person wants to keep the account / is asking for something else.
RETENTION = [
    r"\bleave (the|my) (account|profile)\b", r"\bkeep (the|my) account\b", r"\bcontinue using\b",
    r"\bstill (need|want|use)\b", r"\bin place\b", r"\b(lists?|emails?) only\b", r"\bonly\b.{0,20}\bmarketing\b",
    r"\bleave the account active\b", r"\bas it is\b",
]
OTHER_PURPOSE = [
    r"\bdownload\b", r"\bcopy of\b", r"\bportable\b", r"\bexport\b", r"\bcorrect\b", r"\bmisspel\w*",
    r"\bchange the\b", r"\breplace it\b", r"\breset\w*\b", r"\bpromotional\b", r"\bmarketing\b",
    r"\bwhat personal information\b", r"\binvoices?\b", r"\bout of date\b", r"\bopt[- ]?out\b",
]


def classify(text):
    """Return (authorized: bool, reason: str)."""
    t = " ".join((text or "").split())
    low = t.lower()
    positive, negated = [], []
    for pat in DELETION:
        for m in re.finditer(pat, low):
            window = low[max(0, m.start() - 60): m.end() + 60]
            if not any(re.search(tp, window) for tp in TARGET):
                continue
            (negated if NEGATION.search(low[max(0, m.start() - 40): m.start()]) else positive).append(m.group(0))
    retention = [p for p in RETENTION if re.search(p, low)]
    purpose = [p for p in OTHER_PURPOSE if re.search(p, low)]
    if positive and not retention and not negated:
        return True, f"deletion requested ({', '.join(sorted(set(positive)))})"
    if positive and not retention and len(positive) > len(negated) and not purpose:
        return True, f"deletion requested despite negated mention ({positive}, negated {negated})"
    reasons = []
    if negated:
        reasons.append(f"negated deletion {negated}")
    if retention:
        reasons.append("asks to keep the account")
    if purpose:
        reasons.append("other purpose")
    if not positive and not negated:
        reasons.append("no deletion request found")
    return False, "; ".join(reasons)
