"""Evidence extractors. Each returns an ExtractResult for one manifest entry."""
import os
from dataclasses import dataclass, field


@dataclass
class ExtractResult:
    observations: list = field(default_factory=list)
    scopes: list = field(default_factory=list)
    text: str = ""            # raw/decoded text, kept for identifier search and debugging
    unread: str = ""          # non-empty when the file could not be interpreted
    pending: list = field(default_factory=list)  # rows naming no resource id (resolved later via catalog aliases)
    provenance: str = ""      # how the text was obtained (e.g. visual transcription), for reporting


def extract_file(dataset_dir, meta):
    from . import images, logs, pdf, records, sql
    path = os.path.join(dataset_dir, meta["file_path"])
    ext = os.path.splitext(path)[1].lower()
    handler = {".sql": sql.extract, ".log": logs.extract, ".json": records.extract_json,
               ".txt": records.extract_txt, ".pdf": pdf.extract, ".png": images.extract,
               ".jpg": images.extract, ".jpeg": images.extract}.get(ext)
    if handler is None:
        return ExtractResult(unread=f"unsupported file type {ext}")
    try:
        return handler(path, meta)
    except Exception as exc:  # never silently skip: surface as unread with the reason
        return ExtractResult(unread=f"{type(exc).__name__}: {exc}")
