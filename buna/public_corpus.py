"""Fail-closed loader for a separate, redistribution-reviewed public corpus."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
from buna.upload_limits import MAX_FILE_BYTES

ALLOWED_LICENSES = {"CC0-1.0", "CC-BY-4.0", "CC-BY-3.0", "CC-BY-2.0", "CC-BY-SA-4.0"}


def load_corpus(root: Path) -> list[dict]:
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("schema_version") != 1 or manifest.get("purpose") != "public-redistribution":
        raise ValueError("A reviewed public redistribution manifest is required.")
    papers = manifest.get("papers", [])
    if not papers or len(papers) > 20:
        raise ValueError("Public corpus must contain 1–20 reviewed papers.")
    seen = set()
    total_bytes = 0
    for paper in papers:
        digest = paper["sha256"]
        name = paper["filename"]
        if (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                or name not in {digest + ".pdf", digest + ".txt"} or digest in seen):
            raise ValueError("Invalid or duplicate public corpus identity.")
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Invalid public corpus file.")
        total_bytes += path.stat().st_size
        if total_bytes > 128 * 1024 * 1024:
            raise ValueError("Public corpus exceeds the 128 MiB aggregate bound.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Public corpus checksum mismatch.")
        if paper.get("license") not in ALLOWED_LICENSES or paper.get("redistribution_approved") is not True:
            raise ValueError("Uncleared redistribution license.")
        for field in ("title", "attribution", "license_url", "source_url", "version", "license_evidence"):
            if not isinstance(paper.get(field), str) or not paper[field].strip():
                raise ValueError("Missing public attribution or license evidence.")
        for field in ("license_url", "source_url"):
            url = urlsplit(paper[field])
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("Public attribution links must be HTTPS.")
        seen.add(digest)
    return papers
