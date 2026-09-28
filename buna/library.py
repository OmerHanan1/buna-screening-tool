"""Opt-in persistent paper library; SQLite owns bytes and extraction atomically."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from buna.comparison import text_fingerprint
from buna.engine import ACTIVE as JOB_ACTIVE, MAX_SOURCE_UPLOAD, ParseFailure, parse_file
from buna.library_providers import (DISCLOSURE_VERSION, Resolver, canonical_doi, is_transient, jats_text,
                                    landing_pdf_link, preview_dois, public_url)
from buna.network import DiscoveryCancelled, NetworkError, RetryDeferred, safe_fetch
from buna.storage import now

logger = logging.getLogger(__name__)
ACTIVE = {"queued", "resolving", "downloading", "processing"}
RETRYABLE = {"failed", "unavailable", "metadata-only", "cancelled"}


class LibraryError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def visible(paper):
    return {key: value for key, value in paper.items() if key not in {"cancel_requested"}}


class Library:
    def __init__(self, store, engine, config):
        self.store, self.engine = store, engine
        self.resolver = Resolver(config)
        self.resolver.fetch = self.fetch
        self.lock = threading.RLock()
        self.parse_lock = threading.Lock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.lease = None
        self.temp = store.root / "library-tmp"
        self.temp.mkdir(mode=0o700, exist_ok=True)
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS library_schema(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO library_schema VALUES (1);
                CREATE TABLE IF NOT EXISTS library_papers(
                    id TEXT PRIMARY KEY, doi TEXT UNIQUE COLLATE NOCASE,
                    status TEXT NOT NULL, title TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS library_status ON library_papers(status);
                CREATE TABLE IF NOT EXISTS library_blobs(
                    sha256 TEXT PRIMARY KEY, suffix TEXT NOT NULL,
                    content BLOB NOT NULL, document TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_imports(
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                    digest TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS library_rate_limits(
                    host TEXT PRIMARY KEY, retry_at REAL NOT NULL
                );
            """)

    def start(self):
        self.lease = (self.store.root / "library.lock").open("a")
        os.chmod(self.store.root / "library.lock", 0o600)
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lease.close()
            self.lease = None
            raise RuntimeError("Paper library requires a single server process per data directory.") from None
        with self.lock, self.store.connect() as db:
            for (raw,) in db.execute("SELECT data FROM library_papers"):
                paper = json.loads(raw)
                if paper["status"] in ACTIVE:
                    reason = "Server restarted during import. Retry explicitly; no automatic network restart."
                    paper.update(status="cancelled" if paper.get("cancel_requested") else "failed",
                                 reason=reason, parser_status="interrupted", worker_active=False)
                    paper["errors"].append({"stage": "recovery", "reason": reason})
                    self._save(db, paper)
                elif paper.get("worker_active"):
                    paper["worker_active"] = False
                    self._save(db, paper)
        for path in self.temp.iterdir():
            if re.fullmatch(r"parse-[a-zA-Z0-9_-]+\.(pdf|txt)", path.name):
                path.unlink(missing_ok=True)
        # Only generated library attachment files are eligible for crash cleanup.
        for job in self.store.list():
            folder = self.store.root / job["id"]
            referenced = {p.get(key) for p in job["papers"] for key in ("file", "document_file")}
            for path in folder.glob("library-*"):
                if re.fullmatch(r"library-[0-9a-f-]{36}\.(pdf|txt|json)", path.name) and path.name not in referenced:
                    path.unlink(missing_ok=True)
        self.thread = threading.Thread(target=self._worker, daemon=True, name="buna-library")
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join()
        if self.lease:
            fcntl.flock(self.lease, fcntl.LOCK_UN)
            self.lease.close()
            self.lease = None

    def _save(self, db, paper):
        paper["updated_at"] = now()
        db.execute(
            "INSERT INTO library_papers VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET doi=excluded.doi,status=excluded.status,"
            "title=excluded.title,updated_at=excluded.updated_at,data=excluded.data",
            (paper["id"], paper.get("doi"), paper["status"], paper["title"],
             paper["created_at"], paper["updated_at"], json.dumps(paper)),
        )

    def _cooldown(self, url):
        try:
            host = urlsplit(url).hostname
        except ValueError:
            raise NetworkError("Invalid public HTTPS destination.") from None
        with self.store.connect() as db:
            row = db.execute("SELECT retry_at FROM library_rate_limits WHERE host=?", (host,)).fetchone()
        if row and row[0] > time.time():
            raise NetworkError("This host's Retry-After cooldown is still active. Retry explicitly later.")

    def fetch(self, url, *args, **kwargs):
        try:
            self._cooldown(url)
            return safe_fetch(url, *args, before_request=self._cooldown, **kwargs)
        except RetryDeferred as exc:
            with self.store.connect() as db:
                db.execute("INSERT INTO library_rate_limits VALUES(?,?) ON CONFLICT(host) DO UPDATE SET retry_at=max(retry_at,excluded.retry_at)",
                           (exc.host, exc.retry_at))
            raise

    def _new(self, doi=None, filename=None):
        return {"id": str(uuid4()), "doi": doi, "title": filename or doi or "Local paper",
                "authors": [], "year": None, "status": "queued", "reason": "Waiting to import.",
                "parser_status": "not-started", "created_at": now(), "updated_at": now(),
                "imported_at": None, "errors": [], "provenance": [], "attempts": 0,
                "sha256": None, "blob_ref": None, "cancel_requested": False}

    def get(self, paper_id, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.get(paper_id, connection)
        row = db.execute("SELECT data FROM library_papers WHERE id=?", (paper_id,)).fetchone()
        if not row:
            raise LibraryError("Library paper not found.", 404)
        return json.loads(row[0])

    def update(self, paper_id, **values):
        with self.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            paper = self.get(paper_id, db)
            paper.update(values)
            if paper.get("cancel_requested") and values.get("status") not in {None, "cancelled", "queued"}:
                paper.update(status="cancelled", reason="Import cancelled. Retry explicitly to resume.")
            self._save(db, paper)
            return paper

    def list(self, search="", offset=0, limit=25):
        with self.store.connect() as db:
            query = " FROM library_papers WHERE instr(lower(title),lower(?))>0 OR instr(lower(coalesce(doi,'')),lower(?))>0"
            total = db.execute("SELECT count(*)" + query, (search, search)).fetchone()[0]
            rows = db.execute("SELECT data" + query + " ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                              (search, search, limit, offset)).fetchall()
            active = db.execute("SELECT count(*) FROM library_papers WHERE status IN ('queued','resolving','downloading','processing')").fetchone()[0]
        return {"papers": [visible(json.loads(row[0])) for row in rows], "total": total,
                "offset": offset, "limit": limit, "active": active}

    def import_dois(self, text, request_key, disclosure_version):
        preview = preview_dois(text)
        if not preview["dois"] or preview["invalid"]:
            raise LibraryError("Correct invalid DOI entries before importing.", 422)
        if disclosure_version != DISCLOSURE_VERSION:
            raise LibraryError("Review the current import disclosure before importing.", 422)
        digest = hashlib.sha256(json.dumps(preview["dois"]).encode()).hexdigest()
        with self.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT digest,data FROM library_imports WHERE request_key=?", (request_key,)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise LibraryError("This request key was used for a different DOI list.")
                return json.loads(prior[1])
            queued = db.execute("SELECT count(*) FROM library_papers WHERE status IN ('queued','resolving','downloading','processing')").fetchone()[0]
            if queued + len(preview["dois"]) > 1000:
                raise LibraryError("The import queue is full. Wait or cancel before adding papers.", 429)
            ids = []
            for doi in preview["dois"]:
                row = db.execute("SELECT id FROM library_papers WHERE doi=?", (doi,)).fetchone()
                if row:
                    ids.append(row[0])
                    continue
                paper = self._new(doi)
                paper["consent"] = {"disclosure_version": disclosure_version, "approved_at": now()}
                self._save(db, paper)
                ids.append(paper["id"])
            batch = {"id": str(uuid4()), "paper_ids": ids, "created_at": now(),
                     "note": "Existing entries reused unchanged; retry unsuccessful entries explicitly."}
            db.execute("INSERT INTO library_imports VALUES(?,?,?,?,?)",
                       (batch["id"], request_key, digest, batch["created_at"], json.dumps(batch)))
        self.wake.set()
        return batch

    def batch(self, batch_id):
        with self.store.connect() as db:
            row = db.execute("SELECT data FROM library_imports WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise LibraryError("Import not found.", 404)
            batch = json.loads(row[0])
            papers = [visible(self.get(paper_id, db)) for paper_id in batch["paper_ids"]]
        return {**batch, "papers": papers, "finished": sum(p["status"] not in ACTIVE for p in papers),
                "total": len(papers)}

    def cancel(self, paper_id):
        with self.lock:
            paper = self.get(paper_id)
            if paper["status"] not in ACTIVE:
                return paper
            return self.update(paper_id, cancel_requested=True, status="cancelled",
                               reason="Import cancelled. Retry explicitly to resume.")

    def retry(self, paper_id, disclosure_version):
        with self.lock:
            paper = self.get(paper_id)
            if not paper.get("doi"):
                raise LibraryError("Upload a readable file to retry this local paper.", 422)
            if paper["status"] not in RETRYABLE:
                return paper
            if disclosure_version != DISCLOSURE_VERSION:
                raise LibraryError("Review the import disclosure before retrying.", 422)
            if paper.get("worker_active"):
                raise LibraryError("Cancellation is still finishing. Retry in a moment.")
            paper = self.update(paper_id, status="queued", cancel_requested=False,
                                reason="Explicit retry queued.", parser_status="not-started",
                                consent={"disclosure_version": disclosure_version, "approved_at": now()})
        self.wake.set()
        return paper

    def _cancelled(self, paper_id):
        return self.stop.is_set() or self.get(paper_id).get("cancel_requested", False)

    def _parse(self, content, suffix, cancelled=None):
        with self.parse_lock:
            return self._parse_serial(content, suffix, cancelled)

    def _parse_serial(self, content, suffix, cancelled=None):
        if not content:
            raise ParseFailure("The file is empty.")
        if len(content) > MAX_SOURCE_UPLOAD:
            raise ParseFailure("File exceeds the 32 MiB source limit.")
        if suffix not in {".pdf", ".txt"}:
            raise ParseFailure("Only PDF and UTF-8 text files are supported.")
        if suffix == ".pdf" and not content.startswith(b"%PDF-"):
            raise ParseFailure("The source is not a PDF (possibly a login or HTML landing page).")
        digest = hashlib.sha256(content).hexdigest()
        with self.store.connect() as db:
            row = db.execute("SELECT document FROM library_blobs WHERE sha256=?", (digest,)).fetchone()
        if row:
            return digest, json.loads(row[0])
        with tempfile.NamedTemporaryFile(prefix="parse-", suffix=suffix, dir=self.temp, delete=False) as temp:
            path = Path(temp.name)
            temp.write(content)
        try:
            document = parse_file(path, profile="source", cancelled=cancelled)
            if not document.get("text", "").strip() or not document.get("segments"):
                raise ParseFailure("No readable full text was extracted. Scanned PDFs need local OCR.")
            sections = {s.get("section", "").lower() for s in document["segments"] if s.get("kind") == "body"}
            if "abstract" in sections and not sections - {"abstract", "unspecified"}:
                raise ParseFailure("Only an abstract section could be identified; upload full text with readable body sections.")
            return digest, document
        finally:
            path.unlink(missing_ok=True)

    def _ready(self, paper_id, content, suffix, digest, document, provenance):
        with self.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            paper = self.get(paper_id, db)
            if paper.get("cancel_requested") or self.stop.is_set():
                raise DiscoveryCancelled("Import cancelled.")
            db.execute("INSERT OR IGNORE INTO library_blobs VALUES(?,?,?,?)",
                       (digest, suffix, content, json.dumps(document)))
            paper.update(status="ready", reason="Full text parsed and saved locally.", parser_status="parsed",
                         sha256=digest, blob_ref="sha256:" + digest, imported_at=now(),
                         text_fingerprint=text_fingerprint(document["text"]),
                         warnings=document.get("warnings", []))
            paper["provenance"].append(provenance)
            if not paper.get("doi"):
                paper["title"] = document.get("title") or paper["title"]
            self._save(db, paper)
            return paper

    def manual(self, content, filename, doi=None, origin=None):
        doi = canonical_doi(doi) if doi else None
        suffix = Path(filename).suffix.lower()
        try:
            digest, document = self._parse(content, suffix)
        except ParseFailure as exc:
            with self.lock, self.store.connect() as db:
                paper = self._new(filename=Path(filename).name[:255])
                paper.update(status="failed", parser_status="failed", reason=str(exc),
                             errors=[{"stage": "parser", "provider": "manual", "reason": str(exc)}],
                             provenance=[{"provider": "manual", "claimed_doi": doi}])
                self._save(db, paper)
            raise
        provenance = {"provider": "manual", "filename": Path(filename).name[:255], "saved_at": now(),
                      "reason": "User-supplied local full text; user is responsible for lawful access.",
                      **(origin or {})}
        with self.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._save_manual(db, content, filename, doi, digest, document, provenance)

    def _save_manual(self, db, content, filename, doi, digest, document, provenance):
        """Save already validated local text in the caller's transaction."""
        row = db.execute("SELECT data FROM library_papers WHERE doi=?", (doi,)).fetchone() if doi else None
        if not row and not doi:
            row = db.execute("SELECT data FROM library_papers WHERE json_extract(data,'$.sha256')=? ORDER BY created_at,id LIMIT 1", (digest,)).fetchone()
        paper = json.loads(row[0]) if row else self._new(doi, Path(filename).name[:255])
        if row and (paper["status"] in ACTIVE or paper.get("worker_active")):
            raise LibraryError("This DOI import or cancellation is still finishing; wait before saving a local copy.")
        if paper["status"] == "ready" and paper["sha256"] != digest:
            raise LibraryError("This DOI already has a different saved full-text version; it was not replaced.")
        db.execute("INSERT OR IGNORE INTO library_blobs VALUES(?,?,?,?)",
                   (digest, Path(filename).suffix.lower(), content, json.dumps(document)))
        paper.update(status="ready", parser_status="parsed", reason="Local full text parsed and saved.",
                     sha256=digest, blob_ref="sha256:" + digest, imported_at=now(), cancel_requested=False,
                     title=document.get("title") or paper["title"], warnings=document.get("warnings", []),
                     text_fingerprint=text_fingerprint(document["text"]))
        if doi:
            paper["doi"] = doi
        if provenance not in paper["provenance"]:
            paper["provenance"].append(provenance)
        self._save(db, paper)
        return paper

    def save_job_source(self, job_id, source_id):
        with self.engine.lock:
            try:
                job = self.store.get(job_id)
            except KeyError:
                raise LibraryError("Comparison not found.", 404) from None
            if job["status"] in JOB_ACTIVE:
                raise LibraryError("Wait for the comparison to finish before saving a source.")
            paper = next((p for p in job["papers"] if p["id"] == source_id), None)
            if not paper or not paper.get("parsed") or not paper.get("file"):
                raise LibraryError("This source has no readable local full text.", 422)
            folder = self.store.folder(job_id)
            path = (folder / paper["file"]).resolve()
            if path.parent != folder or not path.is_file():
                raise LibraryError("The saved source file is unavailable.", 409)
            return self.manual(path.read_bytes(), paper.get("filename") or path.name, paper.get("doi"),
                               {"comparison_id": job_id, "source_id": source_id})

    def attach(self, job_id, paper_ids):
        if not paper_ids or len(paper_ids) != len(set(paper_ids)):
            raise LibraryError("Select one or more unique library papers.", 422)
        created = []
        with self.engine.lock, self.lock:
            try:
                job = self.store.get(job_id)
            except KeyError:
                raise LibraryError("Comparison not found.", 404) from None
            if job.get("workflow") != "manual" or job["status"] in JOB_ACTIVE or job.get("report_available"):
                raise LibraryError("Select a new, inactive manual comparison to attach library papers.")
            folder = self.store.folder(job_id)
            papers = list(job["papers"])
            try:
                for paper_id in paper_ids:
                    library_paper = self.get(paper_id)
                    if library_paper["status"] != "ready":
                        raise LibraryError(f"{library_paper['title']} is not comparison-ready: {library_paper['reason']}", 422)
                    if any(p.get("library_paper_id") == paper_id and not p.get("excluded") for p in papers):
                        continue
                    with self.store.connect() as db:
                        blob = db.execute("SELECT suffix,content,document FROM library_blobs WHERE sha256=?",
                                          (library_paper["sha256"],)).fetchone()
                    if not blob or hashlib.sha256(blob[1]).hexdigest() != library_paper["sha256"]:
                        raise LibraryError("Saved full text is missing or damaged; nothing was attached.", 409)
                    fingerprint = library_paper["text_fingerprint"]
                    if fingerprint == text_fingerprint(job["document"]["text"]):
                        raise LibraryError("A selected library paper is identical to your manuscript.", 422)
                    if any(p.get("text_fingerprint") == fingerprint and not p.get("excluded") for p in papers):
                        raise LibraryError("A selected paper is already attached, possibly under another name or DOI.", 409)
                    if len(papers) >= 800:
                        raise LibraryError("The local corpus is limited to 800 sources.", 422)
                    source_id = str(uuid4())
                    file = folder / f"library-{source_id}{blob[0]}"
                    parsed = folder / f"library-{source_id}.json"
                    for path, data in ((file, blob[1]), (parsed, blob[2].encode())):
                        with path.open("xb") as stream:
                            created.append(path)
                            os.chmod(path, 0o600)
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                    papers.append({
                        **visible(library_paper), "id": source_id, "library_paper_id": paper_id,
                        "library_snapshot": visible(library_paper), "filename": library_paper["title"] + blob[0],
                        "file": file.name, "document_file": parsed.name, "status": "parsed",
                        "origins": ["manual", "library"], "provider": "library", "manual": True,
                        "parsed": True, "downloaded": True, "resolved": True, "compared": False,
                        "error": None, "warnings": library_paper.get("warnings", []),
                    })
                def change(current):
                    current.update(papers=papers, status="draft", report_available=False,
                                   consent=None, revision=current.get("revision", 0) + 1)
                return self.store.update(job_id, change)
            except Exception:
                for path in created:
                    path.unlink(missing_ok=True)
                raise

    def _worker(self):
        while not self.stop.is_set():
            self.wake.clear()
            with self.lock, self.store.connect() as db:
                rows = db.execute("SELECT data FROM library_papers WHERE status='queued' ORDER BY created_at LIMIT 50").fetchall()
                ids = []
                for row in rows:
                    paper = json.loads(row[0])
                    if (paper.get("consent") or {}).get("disclosure_version") != DISCLOSURE_VERSION:
                        # Never contact providers the user has not been shown for this entry.
                        paper.update(status="cancelled", reason="The import disclosure changed. Review it and retry explicitly.")
                        self._save(db, paper)
                        continue
                    paper.update(status="resolving", worker_active=True,
                                 attempts=paper["attempts"] + 1, reason="Resolving public DOI metadata and OA locations.")
                    self._save(db, paper)
                    ids.append(paper["id"])
            if not ids:
                self.wake.wait(.5)
                continue
            try:
                papers = [self.get(paper_id) for paper_id in ids]
                oa = self.resolver.openalex_batch([p["doi"] for p in papers],
                    lambda: self.stop.is_set() or all(self._cancelled(paper_id) for paper_id in ids))
                for paper in papers:
                    self._import_one(paper["id"], oa[paper["doi"]])
            except DiscoveryCancelled:
                for paper_id in ids:
                    self.update(paper_id, status="cancelled", worker_active=False,
                                reason="Import interrupted or cancelled. Retry explicitly to resume.")
            except Exception:
                logger.error("Unexpected library worker failure; affected entries marked failed.", exc_info=False)
                for paper_id in ids:
                    if self.get(paper_id)["status"] in ACTIVE:
                        self.update(paper_id, status="failed", reason="Unexpected import error; see local server log.",
                                    worker_active=False)
            finally:
                for paper_id in ids:
                    self.update(paper_id, worker_active=False)

    def _download(self, location, cancelled, errors):
        """Returns (bytes, suffix, provenance extras) or raises NetworkError/ParseFailure."""
        def fetch(url, accept, limit=MAX_SOURCE_UPLOAD):
            return self.fetch(url, limit, timeout=15, headers={"Accept": accept}, cancelled=cancelled,
                              strict_retry_after=True,
                              on_retry=lambda reason: errors.append({"provider": location["provider"],
                                  "stage": "retry", "url": public_url(url), "reason": reason}))
        kind = location.get("kind", "pdf")
        if kind == "jats":
            data, mime, final_url = fetch(location["pdf_url"], "application/xml")
            if "xml" not in mime.lower():
                raise ParseFailure("Europe PMC did not return XML full text.")
            try:
                text = jats_text(data)
            except ValueError as exc:
                raise ParseFailure(str(exc)) from None
            return text.encode(), ".txt", {"downloaded_url": public_url(final_url), "source_format": "JATS XML converted to text",
                                           "xml_sha256": hashlib.sha256(data).hexdigest()}
        url = location.get("pdf_url")
        extras = {}
        if kind == "landing":
            data, mime, page_url = fetch(location["landing_page_url"], "text/html,application/pdf", 3_000_000)
            media = mime.split(";")[0].strip().lower()
            if media == "application/pdf" and data.startswith(b"%PDF-"):
                return data, ".pdf", {"downloaded_url": public_url(page_url)}
            if media not in {"text/html", "application/xhtml+xml"}:
                raise ParseFailure("The open-access landing page is neither HTML nor PDF.")
            url = landing_pdf_link(data, page_url)
            extras["landing_resolved_from"] = public_url(page_url)
            if cancelled():
                raise DiscoveryCancelled("Import cancelled.")
        data, mime, final_url = fetch(url, "application/pdf")
        if mime.split(";")[0].strip().lower() not in {"application/pdf", "application/octet-stream", "binary/octet-stream"}:
            raise ParseFailure("Source returned a non-PDF content type (possibly HTML or a login page).")
        return data, ".pdf", {**extras, "pdf_url": public_url(url), "downloaded_url": public_url(final_url)}

    @staticmethod
    def _no_location_reason(oa):
        epmc = oa.get("europepmc") or {}
        checked = ", ".join(oa.get("checked") or [])
        if epmc.get("free_fulltext") and not epmc.get("open_access_subset"):
            return (f"Europe PMC lists free full text ({epmc.get('pmcid') or 'no PMCID'}), but not in its open-access "
                    "API subset. A website viewer may be available; no permitted automated full-text route was returned. "
                    "Open it yourself and upload a copy you may lawfully use.")
        if not oa.get("openalex_record"):
            return f"OpenAlex has no record for this DOI; no open-access copy was found by the checked providers ({checked})."
        status = oa.get("openalex_oa_status") or "unknown"
        return (f"No open-access copy was found by the checked providers ({checked}; OpenAlex OA status: {status}). "
                "This is what those providers report, not a verified paywall check. Upload a copy you can access.")

    def _import_one(self, paper_id, oa):
        cancelled = lambda: self._cancelled(paper_id)
        errors = list(self.get(paper_id)["errors"])
        try:
            if cancelled():
                raise DiscoveryCancelled("Import cancelled.")
            result = self.resolver.resolve(self.get(paper_id)["doi"], oa, cancelled)
            errors.extend(result["errors"])
            self.update(paper_id, **result["metadata"], provenance=result["provenance"], errors=errors, oa=result.get("oa", {}))
            failures = []
            for location in result["locations"]:
                if cancelled():
                    raise DiscoveryCancelled("Import cancelled.")
                safe_location = {k: public_url(v) if isinstance(v, str) and "url" in k else v
                                 for k, v in location.items()}
                try:
                    self.update(paper_id, status="downloading", reason="Downloading open-access full text.")
                    content, suffix, extras = self._download(location, cancelled, errors)
                    if cancelled():
                        raise DiscoveryCancelled("Import cancelled.")
                    self.update(paper_id, status="processing", parser_status="processing", reason="Extracting full text locally.")
                    digest, document = self._parse(content, suffix, cancelled=cancelled)
                    self.update(paper_id, errors=errors)
                    self._ready(paper_id, content, suffix, digest, document,
                                {**safe_location, **extras, "retrieved_at": now()})
                    return
                except (NetworkError, ParseFailure) as exc:
                    transient = isinstance(exc, NetworkError) and is_transient(str(exc))
                    failures.append((str(exc), transient))
                    errors.append({"provider": location["provider"], "stage": "fulltext",
                                   "url": public_url(location.get("pdf_url") or location.get("landing_page_url") or ""),
                                   "reason": str(exc), "transient": transient, "provenance": safe_location})
                    self.update(paper_id, errors=errors, parser_status="failed", reason=str(exc))
            if failures:
                transient = sum(flag for _, flag in failures)
                refused = sum("HTTP 403" in reason for reason, _ in failures)
                if transient:
                    status = "failed"
                    reason = (f"{transient} of {len(failures)} open-access source(s) had temporary host/network errors. "
                              "Retry later; see Source details.")
                else:
                    status = "unavailable"
                    reason = (f"{len(failures)} open-access source(s) were tried; none gave readable full text"
                              + (f" ({refused} host(s) refused automated download, HTTP 403)" if refused else "")
                              + ". See Source details; upload a copy you can access.")
            elif result["metadata"]:
                status = "failed" if any(e["stage"] == "metadata" and e["provider"] in {"openalex", "unpaywall", "europepmc"}
                                         for e in result["errors"]) else "metadata-only"
                reason = ("An open-access provider lookup failed, so full-text availability is unknown. Retry explicitly."
                          if status == "failed" else self._no_location_reason(result.get("oa") or {}))
            else:
                status, reason = "failed", "No usable metadata returned. See provider errors and retry explicitly."
            self.update(paper_id, status=status, reason=reason, errors=errors)
        except DiscoveryCancelled:
            self.update(paper_id, status="cancelled", reason="Import cancelled. Retry explicitly to resume.", errors=errors)
        except Exception:
            logger.error("Unexpected single-paper import failure; entry marked failed.", exc_info=False)
            errors.append({"stage": "internal", "reason": "Unexpected import failure; see local server log."})
            self.update(paper_id, status="failed", reason=errors[-1]["reason"], errors=errors)
        finally:
            self.update(paper_id, worker_active=False)
