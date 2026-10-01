"""Bounded, immutable shared sources backed by a private conditional-write catalog."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Protocol
from uuid import uuid4
from buna.upload_limits import MAX_FILE_BYTES

MAX_PAPERS = int(os.environ.get("BUNA_SHARED_MAX_PAPERS", "100"))
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ORIGINAL_BYTES = MAX_FILE_BYTES
MAX_PARSED_BYTES = 32 * 1024 * 1024
MAX_CATALOG_BYTES = 512 * 1024
MAX_NEW_PER_DAY = int(os.environ.get("BUNA_SHARED_MAX_NEW_PER_DAY", "50"))
if not 1 <= MAX_PAPERS <= 100 or not 1 <= MAX_NEW_PER_DAY <= 50:
    raise RuntimeError("Shared quotas must remain within 100 papers and 50 new papers per day.")
CATALOG = "catalog-v1.json"
_SHA = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^(0|[0-9a-f]{32})$")
RETIRE_SECONDS = 2 * 3600
logger = logging.getLogger(__name__)


class SharedLibraryError(ValueError):
    pass


class Conflict(Exception):
    pass


class LibraryChanged(SharedLibraryError):
    pass


class SourceNotFound(SharedLibraryError):
    pass


class BlobStore(Protocol):
    def read(self, name: str, limit: int) -> tuple[bytes, str] | None: ...
    def write(self, name: str, data: bytes, expected: str | None) -> str: ...
    def immutable(self, name: str, data: bytes) -> None: ...
    def delete(self, name: str) -> None: ...


class AzureBlobStore:
    def __init__(self, account_url: str, container: str, client_id: str):
        if not re.fullmatch(r"https://[a-z0-9]{3,24}\.blob\.core\.windows\.net", account_url):
            raise ValueError("Shared storage must be the configured Azure Blob HTTPS endpoint.")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", container):
            raise ValueError("Invalid private shared-storage container.")
        from azure.identity import ManagedIdentityCredential
        from azure.storage.blob import ContainerClient
        self.credential = ManagedIdentityCredential(client_id=client_id, retry_total=0,
                                                     connection_timeout=5, read_timeout=10)
        self.client = ContainerClient(account_url, container, credential=self.credential,
                                      retry_total=0, connection_timeout=5, read_timeout=20)

    def read(self, name, limit):
        from azure.core.exceptions import ResourceNotFoundError
        blob = self.client.get_blob_client(name)
        try:
            stream = blob.download_blob(max_concurrency=1)
            if stream.size > limit:
                raise SharedLibraryError("Shared data exceeds its documented size limit.")
            data = stream.readall()
            if len(data) > limit:
                raise SharedLibraryError("Shared data exceeds its documented size limit.")
            return data, stream.properties.etag
        except ResourceNotFoundError:
            return None

    def write(self, name, data, expected):
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError
        blob = self.client.get_blob_client(name)
        try:
            if expected is None:
                result = blob.upload_blob(data, overwrite=False)
            else:
                result = blob.upload_blob(data, overwrite=True, etag=expected,
                                          match_condition=MatchConditions.IfNotModified)
            return result["etag"]
        except (ResourceExistsError, ResourceModifiedError) as exc:
            raise Conflict() from exc

    def immutable(self, name, data):
        try:
            self.write(name, data, None)
        except Conflict:
            current = self.read(name, len(data))
            if current is None or hashlib.sha256(current[0]).digest() != hashlib.sha256(data).digest():
                raise SharedLibraryError("An existing shared object has an unexpected fingerprint.")

    def delete(self, name):
        from azure.core.exceptions import ResourceNotFoundError
        try:
            self.client.delete_blob(name)
        except ResourceNotFoundError:
            pass


def parser_profile():
    import pypdf
    from buna.documents import STRUCTURE_VERSION
    return f"pypdf-{pypdf.__version__}/structure-{STRUCTURE_VERSION}/source-v1"


def checked_document(document: dict) -> None:
    """Structural checks supplement the isolated parser, not a claim of semantic completeness."""
    from buna.documents import SOURCE_MAX_CHARACTERS, SOURCE_MAX_PAGES
    text, pages, segments = document.get("text"), document.get("pages"), document.get("segments")
    if not isinstance(text, str) or not text.strip() or len(text) > SOURCE_MAX_CHARACTERS:
        raise SharedLibraryError("No bounded readable full text was extracted.")
    if not isinstance(pages, list) or not 1 <= len(pages) <= SOURCE_MAX_PAGES:
        raise SharedLibraryError("Source page metadata is invalid.")
    extraction = document.get("extraction", {})
    if not isinstance(extraction, dict) or extraction.get("truncated", False) is not False:
        raise SharedLibraryError("Truncated extraction is not eligible for shared saving.")
    if (any(not isinstance(p, dict) or not isinstance(p.get("text"), str) or type(p.get("number")) is not int or p["number"] != i + 1
            for i, p in enumerate(pages)) or "\n\n".join(p["text"] for p in pages) != text):
        raise SharedLibraryError("Source page text does not match its extraction.")
    if not isinstance(segments, list) or not 1 <= len(segments) <= 100_000:
        raise SharedLibraryError("No readable source body was identified.")
    previous_end = 0
    for segment in segments:
        if not isinstance(segment, dict):
            raise SharedLibraryError("Source segment metadata is invalid.")
        start, end = segment.get("start"), segment.get("end")
        if (type(start) is not int or type(end) is not int or not previous_end <= start < end <= len(text)
                or segment.get("text") != text[start:end] or segment.get("kind") not in {"body", "heading", "reference"}
                or not isinstance(segment.get("section"), str) or len(segment["section"]) > 1000
                or type(segment.get("page")) is not int or not 1 <= segment["page"] <= len(pages)):
            raise SharedLibraryError("Source text offsets are inconsistent.")
        previous_end = end
    sections = {str(s.get("section", "")).casefold() for s in segments if s.get("kind") == "body"}
    if not sections or ("abstract" in sections and not sections - {"abstract", "unspecified"}):
        raise SharedLibraryError("Only an abstract or no readable body was identified; the paper was not saved.")


def cache_document(document):
    checked_document(document)
    from buna.documents import STRUCTURE_VERSION
    if document.get("structure_version") != STRUCTURE_VERSION:
        raise SharedLibraryError("Source extraction version is not current.")
    warnings = document.get("warnings", [])
    if not isinstance(warnings, list) or len(warnings) > 800 or any(not isinstance(w, str) or len(w) > 4000 for w in warnings):
        raise SharedLibraryError("Source extraction warnings are invalid.")
    # Persist only the fields consumed by comparison. Unknown worker-authored
    # objects cannot poison future shared-source parsing.
    return {
        "text": document["text"],
        "pages": [{"number": page["number"], "text": page["text"]} for page in document["pages"]],
        "segments": [{key: segment[key] for key in ("page", "section", "kind", "text", "start", "end")} for segment in document["segments"]],
        "references": [], "warnings": warnings, "title": document.get("title", "Shared source")[:500] if isinstance(document.get("title", "Shared source"), str) else "Shared source",
        "structure_version": STRUCTURE_VERSION,
        "extraction": {"profile": "source-v1", "parser": "pypdf", "pages_extracted": len(document["pages"]),
                       "characters_extracted": len(document["text"]), "truncated": False},
    }


class SharedLibrary:
    def __init__(self, store: BlobStore, curated_hashes: set[str]):
        self.store, self.curated_hashes = store, curated_hashes

    def _read(self):
        value = self.store.read(CATALOG, MAX_CATALOG_BYTES)
        if value is None:
            return {"schema_version": 1, "papers": {}, "reservations": []}, None
        catalog = json.loads(value[0])
        if (catalog.get("schema_version") != 1 or not isinstance(catalog.get("papers"), dict)
                or len(catalog["papers"]) > MAX_PAPERS or not isinstance(catalog.get("reservations"), list)
                or len(catalog["reservations"]) > 1000):
            raise SharedLibraryError("Shared catalog is invalid; it was not used.")
        total = 0
        for digest, item in catalog["papers"].items():
            if (not _SHA.fullmatch(digest) or item.get("sha256") != digest
                    or item.get("state") not in {"pending", "ready"}
                    or not _SHA.fullmatch(str(item.get("parsed_sha256", "")))):
                raise SharedLibraryError("Shared catalog contains an invalid entry.")
            if not _VERSION.fullmatch(str(item.get("object_version", "0"))):
                raise SharedLibraryError("Shared object version is invalid.")
            if item.get("writer") is not None and not re.fullmatch(r"[0-9a-f]{32}", str(item["writer"])):
                raise SharedLibraryError("Shared writer ownership is invalid.")
            if item["state"] == "ready" and item.get("writer"):
                raise SharedLibraryError("A ready source cannot have an unfinished writer.")
            if (type(item.get("original_bytes")) is not int or not 0 < item["original_bytes"] <= MAX_ORIGINAL_BYTES
                    or type(item.get("parsed_bytes")) is not int or not 0 < item["parsed_bytes"] <= MAX_PARSED_BYTES):
                raise SharedLibraryError("Shared catalog size metadata is invalid.")
            total += item["original_bytes"] + item["parsed_bytes"]
            for key in ("title", "attribution", "version", "parser_profile", "license", "license_url", "source_url"):
                if not isinstance(item.get(key), str) or len(item[key]) > 4000:
                    raise SharedLibraryError("Shared catalog text metadata is invalid.")
            if type(item.get("created_at")) not in (int, float) or not math.isfinite(item["created_at"]):
                raise SharedLibraryError("Shared catalog creation metadata is invalid.")
            if item["license_url"] or item["source_url"]:
                raise SharedLibraryError("Shared uploads cannot introduce arbitrary source URLs.")
        if any(type(t) not in (int, float) or not math.isfinite(t) for t in catalog["reservations"]):
            raise SharedLibraryError("Shared quota metadata is invalid.")
        removals = catalog.get("removals", {})
        retired = catalog.get("retired", [])
        if not isinstance(removals, dict) or len(removals) > 2048 or not isinstance(retired, list) or len(retired) > 500:
            raise SharedLibraryError("Library removal metadata exceeds its limits.")
        for digest, marker in removals.items():
            if (not _SHA.fullmatch(digest) or not isinstance(marker, dict)
                    or not _VERSION.fullmatch(str(marker.get("version", "")))
                    or not _VERSION.fullmatch(str(marker.get("previous_version", "")))
                    or type(marker.get("removed")) is not bool or marker.get("kind") not in {"bundled", "shared"}
                    or type(marker.get("time")) not in (int, float) or not math.isfinite(marker["time"])):
                raise SharedLibraryError("Library removal metadata is invalid.")
        retired_keys = set()
        for item in retired:
            if (not isinstance(item, dict) or not _SHA.fullmatch(str(item.get("sha256", "")))
                    or not _SHA.fullmatch(str(item.get("parsed_sha256", "")))
                    or not _VERSION.fullmatch(str(item.get("object_version", "")))
                    or type(item.get("original_bytes")) is not int or not 0 < item["original_bytes"] <= MAX_ORIGINAL_BYTES
                    or type(item.get("parsed_bytes")) is not int or not 0 < item["parsed_bytes"] <= MAX_PARSED_BYTES
                    or type(item.get("delete_after")) not in (int, float) or not math.isfinite(item["delete_after"])):
                raise SharedLibraryError("Retired source metadata is invalid.")
            key = (item["sha256"], item["object_version"])
            if key in retired_keys:
                raise SharedLibraryError("Duplicate retired source accounting.")
            retired_keys.add(key)
            total += item["original_bytes"] + item["parsed_bytes"]
        if total > MAX_TOTAL_BYTES:
            raise SharedLibraryError("Shared catalog exceeds its byte quota.")
        return catalog, value[1]

    def _cas(self, catalog, etag):
        data = json.dumps(catalog, ensure_ascii=False, sort_keys=True).encode()
        if len(data) > MAX_CATALOG_BYTES:
            raise SharedLibraryError("Shared catalog size limit reached.")
        return self.store.write(CATALOG, data, etag)

    @staticmethod
    def _version(catalog, digest):
        return catalog.get("removals", {}).get(digest, {}).get("version", "0")

    @staticmethod
    def _object_paths(item):
        digest, version = item["sha256"], item.get("object_version", "0")
        if not _SHA.fullmatch(digest) or not _VERSION.fullmatch(version) or not _SHA.fullmatch(item["parsed_sha256"]):
            raise SharedLibraryError("Invalid shared object identity.")
        prefix = f"papers/{digest}/" + (f"{version}/" if version != "0" else "")
        return prefix + "original.pdf", prefix + item["parsed_sha256"] + ".json"

    def save_version(self, digest):
        if not _SHA.fullmatch(digest):
            raise SharedLibraryError("Invalid source identity.")
        catalog, _ = self._read()
        return self._version(catalog, digest)

    def visible(self, curated):
        catalog, _ = self._read()
        if any(p["state"] == "ready" and p["parser_profile"] != parser_profile() for p in catalog["papers"].values()):
            raise SharedLibraryError("Shared extraction caches need validation for the current parser version.")
        ready = [copy.deepcopy(p) for p in catalog["papers"].values()
                 if p["state"] == "ready" and p["parser_profile"] == parser_profile()
                 and p["sha256"] not in self.curated_hashes
                 and not catalog.get("removals", {}).get(p["sha256"], {}).get("removed", False)]
        ready.sort(key=lambda p: (p["created_at"], p["sha256"]))
        bundled = [copy.deepcopy(p) for p in curated
                   if not catalog.get("removals", {}).get(p["sha256"], {}).get("removed", False)]
        return [{**p, "library_version": self._version(catalog, p["sha256"]),
                 "storage_kind": "bundled" if p["sha256"] in self.curated_hashes else "shared"}
                for p in bundled + ready]

    def list(self):
        return self.visible([])

    def assert_selected_current(self, versions):
        catalog, _ = self._read()
        for digest, version in versions.items():
            if (self._version(catalog, digest) != version
                    or catalog.get("removals", {}).get(digest, {}).get("removed", False)
                    or (digest not in self.curated_hashes and catalog["papers"].get(digest, {}).get("state") != "ready")):
                raise LibraryChanged("The library changed. Refresh the paper selection before comparing.")

    def remove(self, digest, expected_version):
        if not _SHA.fullmatch(digest) or not _VERSION.fullmatch(expected_version):
            raise SourceNotFound("Paper unavailable.")
        for _ in range(8):
            catalog, etag = self._read()
            marker = catalog.get("removals", {}).get(digest)
            if marker and marker["removed"]:
                if marker["previous_version"] != expected_version:
                    raise LibraryChanged("This paper's library version changed. Refresh before removing it.")
                return {"removed": True, "already_removed": True, "storage_kind": marker["kind"]}
            if self._version(catalog, digest) != expected_version:
                raise LibraryChanged("This paper's library version changed. Refresh before removing it.")
            kind = "bundled" if digest in self.curated_hashes else "shared"
            item = catalog["papers"].get(digest)
            if kind == "shared" and (not item or item["state"] != "ready"):
                raise SourceNotFound("Paper unavailable.")
            if "removals" not in catalog:
                try:
                    self.store.write("catalog-before-removals-v1.json", json.dumps(catalog, sort_keys=True).encode(), None)
                except Conflict:
                    pass
            if digest not in catalog.get("removals", {}) and len(catalog.get("removals", {})) >= 2048:
                raise SharedLibraryError("The bounded removal ledger needs operator maintenance.")
            catalog.setdefault("removals", {})[digest] = {
                "version": uuid4().hex, "previous_version": expected_version,
                "removed": True, "kind": kind, "time": time.time(),
            }
            if kind == "shared":
                if len(catalog.get("retired", [])) >= 500:
                    raise SharedLibraryError("Retired-source cleanup must finish before more papers can be removed.")
                retired = {key: item[key] for key in ("sha256", "parsed_sha256", "original_bytes", "parsed_bytes")}
                retired.update(object_version=item.get("object_version", "0"), delete_after=time.time() + RETIRE_SECONDS)
                catalog.setdefault("retired", []).append(retired)
                del catalog["papers"][digest]
            try:
                self._cas(catalog, etag)
                return {"removed": True, "already_removed": False, "storage_kind": kind}
            except Conflict:
                continue
        raise SharedLibraryError("The library is busy. Removal was not confirmed; retry.")

    def collect_retired(self):
        catalog, _ = self._read()
        expired = [item for item in catalog.get("retired", []) if item["delete_after"] <= time.time()][:5]
        for item in expired:
            for path in self._object_paths(item):
                self.store.delete(path)
            for _ in range(8):
                current, etag = self._read()
                current["retired"] = [p for p in current.get("retired", [])
                                      if (p["sha256"], p["object_version"]) != (item["sha256"], item["object_version"])]
                try:
                    self._cas(current, etag)
                    break
                except Conflict:
                    continue
            else:
                raise SharedLibraryError("Retired files were removed, but quota release needs retry.")

    def _release_writer(self, digest, writer):
        for _ in range(8):
            catalog, etag = self._read()
            current = catalog["papers"].get(digest)
            if not current or current.get("writer") != writer:
                return
            current.pop("writer")
            try:
                self._cas(catalog, etag)
                return
            except Conflict:
                continue
        raise SharedLibraryError("The unfinished writer needs operator review before retry.")

    def capacity(self):
        catalog, _ = self._read()
        used = sum(p["original_bytes"] + p["parsed_bytes"] for p in list(catalog["papers"].values()) + catalog.get("retired", []))
        daily = sum(t >= time.time() - 86400 for t in catalog["reservations"])
        return {"paper_limit": MAX_PAPERS, "papers_remaining": MAX_PAPERS - len(catalog["papers"]),
                "daily_limit": MAX_NEW_PER_DAY, "daily_remaining": max(0, MAX_NEW_PER_DAY - daily),
                "byte_limit": MAX_TOTAL_BYTES, "bytes_remaining": MAX_TOTAL_BYTES - used}

    def record_save_event(self, receipt: str, state: str, code: str = ""):
        """Bounded operator diagnostics, without visitor identity or document metadata."""
        if not re.fullmatch(r"[0-9a-f-]{36}", receipt) or state not in {
            "queued", "validating", "saving", "saved", "already-present", "failed", "cancelled"
        } or not re.fullmatch(r"[a-z-]{0,64}", code):
            raise SharedLibraryError("Invalid save receipt metadata.")
        for _ in range(8):
            catalog, etag = self._read()
            events = catalog.get("save_events", [])
            if not isinstance(events, list):
                raise SharedLibraryError("Shared save diagnostics are invalid.")
            events = [event for event in events if isinstance(event, dict)
                      and isinstance(event.get("time"), (int, float))
                      and event["time"] >= time.time() - 7 * 86400 and event.get("id") != receipt][-99:]
            events.append({"id": receipt, "time": time.time(), "state": state, "code": code})
            catalog["save_events"] = events
            try:
                self._cas(catalog, etag)
                return
            except Conflict:
                continue
        raise SharedLibraryError("The save receipt could not be recorded. Retry saving.")

    def save(self, original: bytes, document: dict, title: str, *, expected_version=None):
        digest = hashlib.sha256(original).hexdigest()
        if expected_version is None:
            expected_version = self.save_version(digest)
        if digest in self.curated_hashes:
            for _ in range(8):
                catalog, etag = self._read()
                if self._version(catalog, digest) != expected_version:
                    raise LibraryChanged("This paper was removed after the save was requested. Select the file again for a new save.")
                marker = catalog.get("removals", {}).get(digest)
                if not marker or not marker["removed"]:
                    return {"state": "already-present", "sha256": digest}
                marker["removed"] = False
                try:
                    self._cas(catalog, etag)
                    return {"state": "saved", "sha256": digest}
                except Conflict:
                    continue
            raise SharedLibraryError("The library is busy. Restoration was not confirmed.")
        if not original.startswith(b"%PDF-") or not 0 < len(original) <= MAX_ORIGINAL_BYTES:
            raise SharedLibraryError("Only supported comparison PDFs can be kept in the shared library.")
        document = cache_document(document)
        parsed = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if len(parsed) > MAX_PARSED_BYTES:
            raise SharedLibraryError("The parsed source exceeds the shared-cache size limit.")
        parsed_sha = hashlib.sha256(parsed).hexdigest()
        title = title[:255]
        writer = uuid4().hex
        blank_pages = sum(not page["text"].strip() for page in document["pages"])
        item = {
            "state": "pending", "sha256": digest, "parsed_sha256": parsed_sha, "object_version": expected_version, "writer": writer,
            "original_bytes": len(original), "parsed_bytes": len(parsed), "title": title,
            "created_at": time.time(), "parser_profile": parser_profile(),
            "version": f"Shared upload · {len(document['pages'])} PDF pages"
                       + (f" · {blank_pages} pages without extractable text" if blank_pages else ""),
            "license": "Uploader-authorized shared hosted comparison; no public redistribution grant inferred.",
            "license_url": "", "source_url": "",
            "attribution": f"User-provided comparison file: {title}. The uploader authorized shared hosted storage, processing and matching excerpts. "
                           "Original PDF bytes are retained privately. Text extraction and report annotations are generated by the service; no OCR or publisher endorsement is implied.",
        }
        reserved = None
        for _ in range(8):
            catalog, etag = self._read()
            if self._version(catalog, digest) != expected_version:
                raise LibraryChanged("This paper was removed after the save was requested. Select the file again for a new save.")
            existing = catalog["papers"].get(digest)
            if existing:
                if existing["state"] == "ready":
                    return {"state": "already-present", "sha256": digest}
                if existing["parsed_sha256"] != parsed_sha or existing["parser_profile"] != parser_profile():
                    raise SharedLibraryError("A pending immutable version needs operator review; it was not replaced.")
                if existing.get("writer"):
                    raise SharedLibraryError("Another save owns this source's unfinished writes. Retry after it finishes; an interrupted writer may need operator review.")
                existing["writer"] = writer
                try:
                    self._cas(catalog, etag)
                    reserved = existing
                    break
                except Conflict:
                    continue
            catalog["reservations"] = [t for t in catalog["reservations"] if isinstance(t, (int, float)) and t >= time.time() - 86400]
            if len(catalog["reservations"]) >= MAX_NEW_PER_DAY:
                raise SharedLibraryError("The shared library's daily new-paper quota is reached.")
            if len(catalog["papers"]) >= MAX_PAPERS:
                raise SharedLibraryError("The shared library's paper-count limit is reached.")
            used = sum(p["original_bytes"] + p["parsed_bytes"] for p in list(catalog["papers"].values()) + catalog.get("retired", []))
            if used + len(original) + len(parsed) > MAX_TOTAL_BYTES:
                raise SharedLibraryError("The shared library's byte quota is reached.")
            catalog["papers"][digest] = item
            catalog["reservations"].append(time.time())
            try:
                self._cas(catalog, etag)
                reserved = item
                break
            except Conflict:
                continue
        if reserved is None:
            raise SharedLibraryError("The shared catalog is busy; the save was not completed.")
        # Exclusive durable ownership has no expiry: a paused writer cannot be
        # superseded, followed by removal/GC, then recreate unaccounted blobs.
        try:
            original_path, parsed_path = self._object_paths(reserved)
            self.store.immutable(original_path, original)
            self.store.immutable(parsed_path, parsed)
            for _ in range(8):
                catalog, etag = self._read()
                if self._version(catalog, digest) != expected_version:
                    raise LibraryChanged("This paper was removed while saving. Its old save will not restore it.")
                current = catalog["papers"].get(digest)
                if not current or current["parsed_sha256"] != parsed_sha or current.get("writer") != writer:
                    raise SharedLibraryError("The shared writer reservation changed; the save was not confirmed.")
                current["state"] = "ready"
                current.pop("writer")
                if digest in catalog.get("removals", {}):
                    catalog["removals"][digest]["removed"] = False
                try:
                    self._cas(catalog, etag)
                    return {"state": "saved", "sha256": digest}
                except Conflict:
                    continue
            raise SharedLibraryError("The files were stored, but catalog publication was not confirmed.")
        finally:
            try:
                self._release_writer(digest, writer)
            except Exception as exc:
                logger.warning("Shared writer release needs operator review (%s).", type(exc).__name__)

    def materialize(self, paper: dict, folder: Path):
        digest, parsed_sha = paper["sha256"], paper["parsed_sha256"]
        if not _SHA.fullmatch(digest) or not _SHA.fullmatch(parsed_sha) or paper["state"] != "ready":
            raise SharedLibraryError("Invalid shared source selection.")
        value = self.store.read(self._object_paths(paper)[1], MAX_PARSED_BYTES)
        if not value or hashlib.sha256(value[0]).hexdigest() != parsed_sha:
            raise SharedLibraryError("The shared source extraction is missing or damaged.")
        path = folder / f"shared-{digest}.json"
        path.write_bytes(value[0])
        path.chmod(0o444)
        return {"cached_document": str(path), "cached_sha256": parsed_sha, "title": paper["title"], "path": ""}
