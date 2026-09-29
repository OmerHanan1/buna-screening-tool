"""Bounded hosted runtime budgets and privacy-safe diagnostics."""
import json
import os
from pathlib import Path
import stat

CPU_SECONDS = 720
WALL_SECONDS = 780
COMPARISON_SECONDS = 480
STAGES = {"starting", "parse-manuscript", "load-source", "compare", "write-evidence", "render-pdf", "attribution", "complete"}
ERRORS = {
    "parse-manuscript-invalid": "The manuscript could not be parsed. Use a readable PDF or plain-text file.",
    "parse-manuscript-empty": "The manuscript has no extractable text. Scanned pages need OCR before comparison.",
    "parse-manuscript-encrypted": "The manuscript is encrypted; this service does not unlock protected PDFs.",
    "parse-manuscript-limit": "The manuscript exceeds a documented page or text limit.",
    "parse-timeout": "Document extraction reached its time limit; no automatic retry was started.",
    "memory-limit": "Processing reached the worker memory limit. No automatic retry was started.",
    "cpu-limit": "Processing reached its total CPU budget. Any completed evidence is retained below.",
    "wall-limit": "Processing reached its total runtime budget. Any completed evidence is retained below.",
    "storage-limit": "Processing reached the temporary output-size limit.",
    "worker-signal": "The isolated worker stopped unexpectedly. Any completed evidence is retained below.",
    "worker-error": "Processing failed unexpectedly. The reference code identifies privacy-safe diagnostic details.",
    "supervisor-monitor-error": "The service could not safely monitor this comparison. No automatic retry was started; the reference code identifies the failure.",
    "pdf-error": "Comparison evidence was completed, but the PDF could not be generated. Download the evidence JSON.",
}


def atomic_json(path: Path, value: dict, *, ensure_ascii: bool = True):
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(value, ensure_ascii=ensure_ascii))
    temporary.replace(path)


def read_artifact(folder: Path, name: str, limit: int) -> bytes:
    fd = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("Invalid worker artifact.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Oversized worker artifact.")
    return data


def safe_progress(folder: Path) -> dict:
    try:
        data = json.loads(read_artifact(folder, "progress.json", 4096))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("stage") not in STAGES:
        return {}
    result = {"stage": data["stage"]}
    for key in ("source_index", "source_count", "processed_sources", "checked_sources", "source_windows_visited", "source_windows_total"):
        value = data.get(key)
        if isinstance(value, int) and 0 <= value <= 10_000_000:
            result[key] = value
    return result


def job_storage_bytes(folder: Path) -> int:
    """Measure a live directory without treating atomic replacement as failure."""
    total = 0
    for path in folder.rglob("*"):
        try:
            info = path.lstat()
        except FileNotFoundError:
            # The worker publishes progress/evidence by atomic rename. The next
            # sample sees the replacement; never follow a substituted symlink.
            continue
        if not stat.S_ISLNK(info.st_mode):
            total += info.st_size
    return total
