"""Local, audited reference collections. Import only explicitly staged full texts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from buna.comparison import text_fingerprint
from buna.engine import MAX_SOURCE_UPLOAD, ParseFailure
from buna.library import LibraryError
from buna.library_providers import canonical_doi, public_url
from buna.storage import now


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference_id: int = Field(ge=1, le=1000)
    kind: Literal["Internet", "Publication"]
    title: str = Field(min_length=1, max_length=1000)
    authors: list[str] = Field(default_factory=list, max_length=100)
    year: int | None = None
    source_url: str | None = Field(default=None, max_length=2000)
    doi: str | None = None
    status: Literal["ready", "unavailable", "metadata-only", "mismatch", "unsupported", "unresolved"]
    reason: str = Field(min_length=1, max_length=4000)
    version: str = Field(default="unresolved", max_length=200)
    identity_evidence: str = Field(default="", max_length=4000)
    access_basis: str = Field(default="", max_length=1000)
    license: str = Field(default="Not established", max_length=500)
    license_url: str | None = Field(default=None, max_length=2000)
    retrieved_url: str | None = Field(default=None, max_length=2000)
    filename: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_identity(self):
        if self.doi:
            self.doi = canonical_doi(self.doi)
        for key in ("source_url", "license_url", "retrieved_url"):
            value = getattr(self, key)
            if value and (not value.startswith("https://") or public_url(value) != value):
                raise ValueError(f"{key} must be a public HTTPS URL without private query credentials.")
        if self.status == "ready" and not all((
            self.filename, self.sha256, self.identity_evidence, self.access_basis,
            self.version != "unresolved", self.authors,
        )):
            raise ValueError("Ready entries require a verified file, authors, version, identity and lawful-access evidence.")
        if self.filename and not re.fullmatch(r"[a-f0-9]{64}\.(pdf|txt)", self.filename):
            raise ValueError("Use generated SHA256.pdf/txt filenames, never paths.")
        if self.filename and self.sha256 != self.filename.split(".")[0]:
            raise ValueError("Filename must match the file SHA256.")
        return self


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    collection_id: UUID
    request_key: UUID
    name: str = Field(min_length=1, max_length=200)
    reference_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    authorization: Literal["user-authorized-reference-collection-import"]
    authorization_note: str = Field(min_length=1, max_length=2000)
    entries: list[Entry] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_references(self):
        if len({e.reference_id for e in self.entries}) != len(self.entries):
            raise ValueError("Reference IDs must be unique.")
        self.entries.sort(key=lambda entry: entry.reference_id)
        return self


class Collections:
    def __init__(self, library):
        self.library = library
        with library.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS library_collections(
                    id TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_collection_imports(
                    request_key TEXT PRIMARY KEY, digest TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_preferences(
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
            """)

    def _get(self, collection_id, db):
        row = db.execute("SELECT data FROM library_collections WHERE id=?", (collection_id,)).fetchone()
        if not row:
            raise LibraryError("Collection not found.", 404)
        return json.loads(row[0])

    def list(self):
        with self.library.lock, self.library.store.connect() as db:
            rows = db.execute("SELECT data FROM library_collections ORDER BY id").fetchall()
            preference = db.execute("SELECT value FROM library_preferences WHERE key='default_collection'").fetchone()
            collections = []
            for row in rows:
                item = json.loads(row[0])
                ready, fingerprints = [], set()
                for entry in item["entries"]:
                    entry["comparison_ready"] = False
                    if entry.get("paper_id"):
                        paper = self.library.get(entry["paper_id"], db)
                        if paper["status"] == "ready" and paper["sha256"] == entry.get("sha256"):
                            entry["comparison_ready"] = True
                            fingerprint = paper["text_fingerprint"]
                            if fingerprint not in fingerprints:
                                ready.append({"id": paper["id"], "title": paper["title"], "version": entry["version"]})
                                fingerprints.add(fingerprint)
                        else:
                            entry["reason"] = f"Library paper is {paper['status']}: {paper['reason']}"
                item.update(ready_papers=ready, ready_unique=len(ready),
                            ready_references=sum(e["comparison_ready"] for e in item["entries"]),
                            total_references=len(item["entries"]))
                collections.append(item)
            return {"collections": collections, "default_collection_id": preference[0] if preference and preference[0] else None}

    def set_default(self, collection_id):
        with self.library.lock, self.library.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if collection_id:
                self._get(collection_id, db)
            db.execute("INSERT INTO library_preferences VALUES('default_collection',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (collection_id or "",))
        return self.list()

    def import_manifest(self, raw, stage_root):
        manifest = Manifest.model_validate(raw)
        normalized = manifest.model_dump(mode="json")
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        request_key, collection_id = str(manifest.request_key), str(manifest.collection_id)
        library = self.library
        # Serialize before parsing so concurrent retries neither duplicate parsing nor partial imports.
        with library.lock, tempfile.TemporaryDirectory(prefix="collection-", dir=library.temp) as prepared_root:
            with library.store.connect() as db:
                prior = db.execute("SELECT digest,data FROM library_collection_imports WHERE request_key=?", (request_key,)).fetchone()
                if prior:
                    if prior[0] != digest:
                        raise LibraryError("This collection request key was used for a different manifest.")
                    return json.loads(prior[1])
            prepared, entries = {}, []
            staged_bytes = 0
            root = Path(stage_root).resolve(strict=True)
            for source in manifest.entries:
                entry = source.model_dump()
                entry["paper_id"] = None
                if source.status == "ready":
                    try:
                        if source.sha256 not in prepared:
                            path = root / source.filename
                            if path.is_symlink() or path.resolve(strict=True).parent != root:
                                raise LibraryError("Staged file must be a regular file directly inside the staging directory.", 422)
                            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                            with os.fdopen(fd, "rb") as stream:
                                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                                    raise LibraryError("Staged file is not a regular file.", 422)
                                size = os.fstat(stream.fileno()).st_size
                                if size > MAX_SOURCE_UPLOAD:
                                    raise LibraryError(f"Staged file exceeds the {MAX_SOURCE_UPLOAD // (1024 * 1024)} MiB source limit.", 422)
                                if staged_bytes + size > 256 * 1024 * 1024:
                                    raise LibraryError("Collection staging exceeds the 256 MiB batch limit.", 422)
                                content = stream.read(MAX_SOURCE_UPLOAD + 1)
                            if len(content) > MAX_SOURCE_UPLOAD:
                                raise LibraryError(f"Staged file exceeds the {MAX_SOURCE_UPLOAD // (1024 * 1024)} MiB source limit.", 422)
                            if hashlib.sha256(content).hexdigest() != source.sha256:
                                raise LibraryError("Staged file hash does not match the manifest.", 422)
                            staged_bytes += len(content)
                            if staged_bytes > 256 * 1024 * 1024:
                                raise LibraryError("Collection staging exceeds the 256 MiB batch limit.", 422)
                            file_digest, document = library._parse(content, path.suffix)
                            content_path = Path(prepared_root) / source.filename
                            document_path = content_path.with_suffix(".json")
                            content_path.write_bytes(content)
                            document_path.write_text(json.dumps(document, ensure_ascii=False))
                            os.chmod(content_path, 0o600)
                            os.chmod(document_path, 0o600)
                            prepared[source.sha256] = (content_path, file_digest, document_path)
                            del content, document
                    except (OSError, ParseFailure, LibraryError) as exc:
                        entry.update(status="unsupported", reason=f"Staged import failed: {exc}")
                entries.append(entry)
            with library.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT data FROM library_collections WHERE id=?", (collection_id,)).fetchone()
                if existing:
                    prior_collection = json.loads(existing[0])
                    if prior_collection["reference_sha256"] != manifest.reference_sha256:
                        raise LibraryError("A collection cannot be replaced with a different reference report.")
                    if not {e["reference_id"] for e in prior_collection["entries"]} <= {e.reference_id for e in manifest.entries}:
                        raise LibraryError("A collection update cannot silently drop reference entries.")
                for entry in entries:
                    if entry["status"] != "ready":
                        continue
                    content_path, file_digest, document_path = prepared[entry["sha256"]]
                    origin = {
                        "provider": "reference-collection", "collection_id": collection_id,
                        "reference_id": entry["reference_id"], "import_batch": request_key,
                        "source_url": entry["source_url"], "downloaded_url": entry["retrieved_url"],
                        "license": entry["license"], "license_url": entry["license_url"],
                        "version": entry["version"], "identity_evidence": entry["identity_evidence"],
                        "access_basis": entry["access_basis"], "authorization": manifest.authorization,
                        "authorization_note": manifest.authorization_note,
                    }
                    try:
                        paper = library._save_manual(db, content_path.read_bytes(), entry["filename"], entry["doi"],
                                                     file_digest, json.loads(document_path.read_text()), origin)
                    except LibraryError as exc:
                        entry.update(status="unresolved", reason=f"Library import conflict: {exc}")
                        continue
                    # Bibliographic metadata was verified against this specific file, not inferred by the parser.
                    paper.update(title=entry["title"], authors=entry["authors"], year=entry["year"])
                    library._save(db, paper)
                    entry["paper_id"] = paper["id"]
                result = {"id": collection_id, "name": manifest.name,
                          "reference_sha256": manifest.reference_sha256, "entries": entries,
                          "import_batch": request_key, "imported_at": now(),
                          "authorization": manifest.authorization, "authorization_note": manifest.authorization_note}
                db.execute("INSERT INTO library_collections VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                           (collection_id, json.dumps(result)))
                db.execute("INSERT INTO library_collection_imports VALUES(?,?,?)", (request_key, digest, json.dumps(result)))
                return result

    def attach(self, collection_id, job_id, paper_ids):
        library = self.library
        with library.lock:
            collection = next((c for c in self.list()["collections"] if c["id"] == collection_id), None)
            if not collection:
                raise LibraryError("Collection not found.", 404)
            allowed = {p["id"] for p in collection["ready_papers"]}
            if len(set(paper_ids)) != len(paper_ids) or not set(paper_ids) <= allowed:
                raise LibraryError("Selection must contain unique, currently ready members of this collection.", 422)
            try:
                job = library.store.get(job_id)
            except KeyError:
                raise LibraryError("Comparison not found.", 404) from None
            from buna.engine import ACTIVE
            if job.get("workflow") != "manual" or job["status"] in ACTIVE or job.get("report_available"):
                raise LibraryError("Collection selection can only change an inactive, unsaved manual comparison.")
            fingerprints = {text_fingerprint(job["document"]["text"])}
            fingerprints.update(p.get("text_fingerprint") for p in job["papers"] if not p.get("excluded"))
            selected, skipped = [], []
            for paper_id in paper_ids:
                paper = library.get(paper_id)
                if paper["text_fingerprint"] in fingerprints:
                    skipped.append({"id": paper_id, "reason": "Identical to the manuscript or an already selected source."})
                else:
                    selected.append(paper_id)
                    fingerprints.add(paper["text_fingerprint"])
        updated = library.attach(job_id, selected) if selected else job
        return {"job": updated, "skipped": skipped}
