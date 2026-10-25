"""Minimal stdlib PDF text extraction (ReportLab-style PDFs: Flate/ASCII85 streams, ToUnicode CMaps).

Business documents rarely carry explicit state, so each resource id mentioned in a document becomes a
'present' observation carrying the identifiers found in that document.
"""
import base64
import re
import zlib

from .. import norm
from ..observations import Observation
from . import ExtractResult

OBJ_RE = re.compile(rb"(?m)^(\d+) 0 obj")
TEXT_OP_RE = re.compile(rb"/(F[\w+]+) [\d.]+ Tf|<([0-9a-fA-F]*)> Tj|\(((?:[^()\\]|\\.)*)\) Tj|T\*|Td|TD|ET")


def _objects(data):
    pos = [(m.start(), int(m.group(1))) for m in OBJ_RE.finditer(data)]
    return {n: data[p:(pos[i + 1][0] if i + 1 < len(pos) else len(data))] for i, (p, n) in enumerate(pos)}


def _stream(body):
    m = re.search(rb"stream\r?\n(.*?)endstream", body, re.S)
    if not m:
        return None
    s = m.group(1)
    if b"ASCII85Decode" in body:
        s = s.strip()
        if s.endswith(b"~>"):
            s = s[:-2]
        s = base64.a85decode(s)
    if b"FlateDecode" in body:
        s = zlib.decompress(s)
    return s


def _cmap(stream):
    mp = {}
    for blk in re.findall(rb"beginbfchar(.*?)endbfchar", stream, re.S):
        for a, u in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            mp[int(a, 16)] = bytes.fromhex(u.decode()).decode("utf-16-be", "replace")
    for blk in re.findall(rb"beginbfrange(.*?)endbfrange", stream, re.S):
        for a, b, u in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            for i in range(int(a, 16), int(b, 16) + 1):
                mp[i] = chr(int(u, 16) + i - int(a, 16))
    return mp


def pdf_text(path):
    with open(path, "rb") as fh:
        data = fh.read()
    objs = _objects(data)
    font_maps = {}
    for n, body in objs.items():
        m = re.search(rb"/ToUnicode (\d+) 0 R", body)
        if m:
            font_maps[n] = _cmap(_stream(objs[int(m.group(1))]) or b"")
    fonts = {}
    for body in objs.values():
        m = re.search(rb"/Font (\d+) 0 R", body)
        if m and int(m.group(1)) in objs:
            for name, ref in re.findall(rb"/(F[\w+]+) (\d+) 0 R", objs[int(m.group(1))]):
                fonts[name.decode()] = font_maps.get(int(ref))
    out = []
    for n in sorted(objs):
        for c in re.findall(rb"/Contents (\d+) 0 R", objs[n]):
            content = _stream(objs[int(c)]) or b""
            cmap = None
            for tok in TEXT_OP_RE.finditer(content):
                if tok.group(1):
                    cmap = fonts.get(tok.group(1).decode())
                elif tok.group(2) is not None:
                    h = tok.group(2).decode()
                    codes = [int(h[i:i + 2], 16) for i in range(0, len(h), 2)]
                    out.append("".join(cmap.get(x, "?") for x in codes) if cmap else bytes(codes).decode("latin-1"))
                elif tok.group(3) is not None:
                    out.append(tok.group(3).decode("latin-1").replace("\\(", "(").replace("\\)", ")"))
                else:
                    out.append("\n")
    return re.sub(r"\n+", "\n", "".join(out)).strip()


def document_observations(text, meta, kind):
    """Observations for every resource id a single-subject business document mentions."""
    observations = []
    ids = norm.identifiers_in_text(text)
    exposed = any("@" in i or i.startswith("tel:") for i in ids)
    lower = text.lower()
    for rid in sorted(norm.resources_in_text(text)):
        system = norm.system_for(rid)
        category = None
        if system == "support_platform" and ("attachment" in lower or ".pdf" in lower):
            category = "SUPPORT_ATTACHMENT"
        from ..observations import DEFAULT_CATEGORY
        observations.append(Observation(
            evidence_id=meta["file_id"], captured_at=meta["captured_at"], observed_at=meta["captured_at"],
            system=system, resource_id=rid, category=category or DEFAULT_CATEGORY.get(system, "UNKNOWN"),
            present=True, exposed=exposed, ids=frozenset(ids), detail=f"{kind} document mentions {rid}",
            source_kind="screenshot_transcript" if kind.startswith("image") else "pdf_text"))
    return observations


def extract(path, meta):
    text = pdf_text(path)
    if not text:
        return ExtractResult(unread="no extractable text layer")
    res = ExtractResult(observations=document_observations(text, meta, "pdf"), text=text,
                        provenance="stdlib PDF text extraction")
    if not res.observations:
        # The document names no resource id; a catalog alias (e.g. a statement number) may identify it later.
        res.pending.append({"row": {"document_text": text}, "text": text})
    return res
