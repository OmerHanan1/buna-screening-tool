"""Exact-hash private processing manifest; never infers permission from sign-in."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit


def load_team_corpus(root: Path, expected_manifest_sha: str, tenant: str, owner_oid: str):
    raw = (root / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha:
        raise ValueError("Private corpus manifest does not match its reviewed fingerprint.")
    manifest = json.loads(raw)
    if (manifest.get("schema_version") != 1 or manifest.get("purpose") != "private-team-processing"
            or manifest.get("authorized_identity") != {"tenant": tenant, "object_id": owner_oid}):
        raise ValueError("Private corpus identity/scope is not approved.")
    papers = manifest.get("papers", [])
    if not 1 <= len(papers) <= 50:
        raise ValueError("Private corpus must contain 1–50 individually approved files.")
    seen, total = set(), 0
    for paper in papers:
        digest, filename = paper["sha256"], paper["filename"]
        if (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest) or digest in seen
                or filename not in {digest + ".pdf", digest + ".txt"}):
            raise ValueError("Invalid private corpus file identity.")
        permission = paper.get("hosted_processing_permission") or {}
        if permission.get("approved") is not True or permission.get("scope") != "owner-hosted-research-reports-only":
            raise ValueError("Hosted processing permission is not confirmed for this file.")
        for field in ("basis", "evidence", "reviewer", "reviewed_at"):
            if not isinstance(permission.get(field), str) or not permission[field].strip():
                raise ValueError("Private corpus permission evidence is incomplete.")
        path = root / filename
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Invalid private corpus file.")
        total += path.stat().st_size
        if total > 160 * 1024 * 1024:
            raise ValueError("Private corpus exceeds its 160 MiB input limit.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Private corpus file fingerprint mismatch.")
        for field in ("title", "attribution", "version", "license", "license_url", "source_url"):
            if not isinstance(paper.get(field), str) or not paper[field].strip():
                raise ValueError("Private corpus provenance is incomplete.")
        for field in ("license_url", "source_url"):
            url = urlsplit(paper[field])
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("Invalid private corpus provenance URL.")
        seen.add(digest)
    return papers


def load_attested_corpus(root: Path, expected_manifest_sha: str):
    """Separate user-attested hosted-use basis; not a redistribution license."""
    raw = (root / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha:
        raise ValueError("Hosted corpus manifest fingerprint mismatch.")
    manifest = json.loads(raw)
    if (manifest.get("schema_version") != 1 or manifest.get("purpose") != "private-hosted-processing"
            or manifest.get("access_policy") != "email-gate-with-independent-visitor-capabilities"):
        raise ValueError("Hosted corpus scope is not approved.")
    attestation = manifest.get("user_attestation") or {}
    if (attestation.get("confirmed") is not True
            or attestation.get("scope") != "hosted-storage-processing-and-matching-excerpts"
            or not attestation.get("recorded_at") or not attestation.get("statement")):
        raise ValueError("Hosted-use attestation is incomplete.")
    papers = manifest.get("papers", [])
    if not 1 <= len(papers) <= 50:
        raise ValueError("Hosted corpus must contain 1–50 approved files.")
    seen, total = set(), 0
    for paper in papers:
        digest, filename = paper["sha256"], paper["filename"]
        if (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest) or digest in seen
                or filename not in {digest + ".pdf", digest + ".txt"}):
            raise ValueError("Invalid hosted corpus file identity.")
        if paper.get("hosted_processing_basis") != "user-attested-hosted-use":
            raise ValueError("File is outside the hosted-use attestation.")
        path = root / filename
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Invalid hosted corpus file.")
        total += path.stat().st_size
        if total > 160 * 1024 * 1024:
            raise ValueError("Hosted corpus exceeds the 160 MiB input bound.")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Hosted corpus file fingerprint mismatch.")
        from buna.corpus_cache import validate_cache
        validate_cache(root, paper)
        for field in ("title", "attribution", "version", "license", "license_url", "source_url"):
            if not isinstance(paper.get(field), str) or not paper[field].strip():
                raise ValueError("Hosted corpus provenance is incomplete.")
        for field in ("license_url", "source_url"):
            url = urlsplit(paper[field])
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError("Invalid hosted corpus provenance URL.")
        seen.add(digest)
    return papers
