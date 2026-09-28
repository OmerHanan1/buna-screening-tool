import hashlib
import json
import os
import secrets
import time
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from buna.engine import ACTIVE, MAX_UPLOAD, MAX_SOURCE_UPLOAD, Engine, ParseFailure, consent_digest, parse_file, public_job, summarize_report, write_json
from buna.reports import render_report
from buna.storage import Store, now
from buna.comparison import ALGORITHM_VERSION, text_fingerprint
from buna.presentation import manuscript_reader
from buna.pdf_reports import cached_pdf, PdfReportError


class Reference(BaseModel):
    id: str = Field(max_length=100)
    raw: str = Field(min_length=1, max_length=4000)
    doi: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=1000)


class EditJob(BaseModel):
    queries: list[str] = Field(max_length=8)
    references: list[Reference] = Field(max_length=500)
    target: int = Field(ge=100, le=200)


class ProviderChoice(BaseModel):
    provider: Literal["crossref", "openalex"] = "crossref"


class RunJob(ProviderChoice):
    mode: Literal["offline", "online"] = "offline"
    consent_token: str | None = None
    exclude_quotes: bool = True
    comparison_model: Literal["validated-lexical", "experimental-ordered"] = "validated-lexical"


class SourceSelection(BaseModel):
    excluded: bool = False
    reason: str = Field(default="", max_length=1000)
    doi: str | None = Field(default=None, max_length=300)


class BodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        source_upload = scope.get("method") == "POST" and (
            scope["path"] == "/api/library/uploads" or bool(re.fullmatch(r"/api/jobs/[^/]+/sources", scope["path"]))
        )
        limit = (MAX_SOURCE_UPLOAD if source_upload else MAX_UPLOAD) + 1024 * 1024
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = limit + 1
        if length > limit:
            await JSONResponse({"detail": f"Request exceeds the {limit // (1024 * 1024)} MiB upload envelope."}, status_code=413)(scope, receive, send)
            return
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(413, "Request exceeds the upload limit.")
            return message

        await self.app(scope, bounded_receive, send)


def create_app(data_dir: Path | None = None, config: dict | None = None) -> FastAPI:
    store = Store(data_dir or Path(os.environ.get("BUNA_DATA_DIR", ".buna-data")))
    settings = config if config is not None else {
        "openalex_api_key": os.environ.get("BUNA_OPENALEX_API_KEY", ""),
        "contact_email": os.environ.get("BUNA_CONTACT_EMAIL", ""),
    }
    engine = Engine(store, settings)
    library = None
    if settings.get("enable_library", os.environ.get("BUNA_ENABLE_LIBRARY") == "1"):
        from buna.library import Library
        library = Library(store, engine, {
            **settings, "unpaywall_email": settings.get("unpaywall_email", os.environ.get("BUNA_UNPAYWALL_EMAIL", "")),
        })

    @asynccontextmanager
    async def lifespan(app):
        if library:
            library.start()
        try:
            yield
        finally:
            if library:
                library.close()
            engine.close()

    app = FastAPI(title="Paper Overlap Detector", lifespan=lifespan)
    app.state.store = store
    app.state.engine = engine
    app.state.library = library
    if library:
        from buna.library_api import library_router
        app.include_router(library_router(library))
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_origin_guard(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            parsed = urlsplit(origin)
            if parsed.scheme != "http" or parsed.netloc not in {
                "localhost:8000", "127.0.0.1:8000", "localhost:5173", "127.0.0.1:5173",
                request.headers.get("host", ""),
            }:
                return JSONResponse({"detail": "Cross-origin mutations are forbidden."}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site access is forbidden."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        pdf_response = request.url.path.endswith("/report.pdf")
        response.headers["X-Frame-Options"] = "SAMEORIGIN" if pdf_response else "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'self' blob:; frame-src 'self' blob:; "
            + ("frame-ancestors 'self'; " if pdf_response else "frame-ancestors 'none'; ")
            + "base-uri 'none'"
        )
        return response

    def get_job(job_id: str) -> dict:
        try:
            canonical = str(UUID(job_id))
            return store.get(canonical)
        except (ValueError, KeyError) as exc:
            raise HTTPException(404, "Job not found.") from exc

    def editable(job: dict) -> None:
        if job["status"] in ACTIVE:
            raise HTTPException(409, "Cancel or wait for the active screening before editing.")

    def invalidate(job: dict) -> None:
        job.update(consent=None, revision=job.get("revision", 0) + 1, report_available=False, status="draft")

    def save_upload(file: UploadFile, folder: Path, prefix: str, *, max_bytes: int = MAX_UPLOAD) -> Path:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".pdf", ".txt"}:
            raise HTTPException(415, "Only PDF and UTF-8 plain text files are supported.")
        path = folder / f"{prefix}-{uuid4()}{suffix}"
        total = 0
        try:
            with path.open("xb") as output:
                os.chmod(path, 0o600)
                while chunk := file.file.read(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(413, f"File exceeds the {max_bytes // (1024 * 1024)} MiB limit.")
                    output.write(chunk)
            if not total:
                raise HTTPException(422, "The uploaded file is empty.")
            if suffix == ".pdf":
                with path.open("rb") as source:
                    if source.read(5) != b"%PDF-":
                        raise HTTPException(422, "The uploaded file is not a PDF.")
            return path
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            file.file.close()

    def disclosure(job: dict, provider: str) -> dict:
        return {
            "consent_scope_version": 2,
            "provider": provider, "queries": job["queries"], "references": job["references"],
            "target": job["target"], "contact_email": settings.get("contact_email", ""),
            "credential": "Configured OpenAlex API key (sent only to OpenAlex)" if settings.get("openalex_api_key") else "None",
            "oa_resolution": {
                "enabled": provider == "crossref",
                "destination": "https://api.openalex.org/works",
                "data": (
                    "DOI identifiers of public records returned by Crossref are sent to OpenAlex "
                    "to find authorized open-access full text. These derived records are not known until discovery."
                    if provider == "crossref" else
                    "OpenAlex returns open-access locations directly for the approved queries and references."
                ),
            },
        }

    def destinations(provider: str) -> list[str]:
        return (
            (["https://api.crossref.org/works"] if provider == "crossref" else [])
            + ["https://api.openalex.org/works",
               "Public HTTPS open-access PDF hosts returned by OpenAlex (their URLs are recorded per source)."]
        )

    @app.get("/api/health")
    def health():
        return {"status": "ok", "scope": "local single-user", "algorithm_version": ALGORITHM_VERSION,
                "features": {"library": library is not None}}

    @app.get("/api/providers")
    def providers():
        return {
            "providers": [
                {"id": "crossref", "label": "Crossref + OpenAlex OA", "configured": True,
                 "description": "Crossref discovery followed by OpenAlex DOI lookup for authorized open-access PDFs. Consent covers both APIs; no key required."},
                {"id": "openalex", "label": "OpenAlex", "configured": True,
                 "description": "Uses provider-designated open-access PDF locations. Optional API key; keyless access is subject to provider quotas."},
            ],
            "disclosure": "Confirmed queries and references go to the selected discovery provider. The Crossref workflow also sends returned public DOIs to OpenAlex for OA resolution, disclosed in the consent preview. OA retrieval contacts publisher/repository hosts. No manuscript or full text is sent. Google Scholar has no official public search API; this app does not scrape it.",
        }

    @app.get("/api/jobs")
    def jobs(request: Request, include_test: bool = False, workflow: Literal["manual", "legacy"] | None = None):
        show_test_jobs = include_test or request.headers.get("x-buna-test-fixture") == "1"
        return [
            public_job(job, detail=False) for job in store.list()
            if (show_test_jobs or not job.get("is_test_fixture", False))
            and (workflow is None or job.get("workflow", "legacy") == workflow)
        ]

    @app.post("/api/jobs", status_code=201)
    def upload_manuscript(request: Request, file: UploadFile = File(...), manual: bool = Form(False)):
        job_id = str(uuid4())
        folder = store.folder(job_id)
        path = save_upload(file, folder, "manuscript")
        try:
            document = parse_file(path)
        except ParseFailure as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(422, str(exc)) from exc
        write_json(folder / "manuscript.json", document)
        title = document.get("title") or Path(file.filename or "Untitled manuscript").stem
        queries = [title[:250]] if title else []
        job = store.create({
            "id": job_id, "title": title, "created_at": now(), "updated_at": now(),
            "status": "draft", "stage": "extraction", "progress": 0,
            "message": "Extraction ready. Review text, queries and references before screening.",
            "document": document, "manuscript_file": path.name, "document_file": "manuscript.json",
            "manuscript_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "filename": Path(file.filename or "Your paper").name[:255],
            "workflow": "manual" if manual else "legacy",
            "queries": queries, "references": document.get("references", []),
            "target": 150, "mode": "offline", "papers": [], "warnings": document.get("warnings", []),
            "is_test_fixture": request.headers.get("x-buna-test-fixture") == "1",
            "consent": None, "revision": 0, "report_available": False, "cancel_requested": False,
        })
        return public_job(job)

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        return public_job(get_job(job_id))

    @app.patch("/api/jobs/{job_id}")
    def edit_job(job_id: str, body: EditJob):
        with engine.lock:
            job = get_job(job_id)
            editable(job)
            if job.get("workflow") == "manual" and job.get("report_available"):
                raise HTTPException(409, "This report is saved. Start a new comparison to change its files.")
            queries = [query.strip() for query in body.queries if query.strip()]
            if any(len(query) > 500 for query in queries):
                raise HTTPException(422, "Queries must be at most 500 characters.")
            if len({reference.id for reference in body.references}) != len(body.references):
                raise HTTPException(422, "Reference identifiers must be unique.")

            def change(current):
                current.update(queries=queries, references=[r.model_dump() for r in body.references], target=body.target)
                invalidate(current)
            return public_job(store.update(job["id"], change))

    @app.post("/api/jobs/{job_id}/sources")
    def upload_source(
        job_id: str, file: UploadFile = File(...), title: str = Form(""), paper_id: str = Form(""),
    ):
        with engine.lock:
            job = get_job(job_id)
            editable(job)
            if job.get("workflow") == "manual" and job.get("report_available"):
                raise HTTPException(409, "This report is saved. Start a new comparison to change its files.")
            if len(job["papers"]) >= 800 and not paper_id:
                raise HTTPException(422, "The local corpus is limited to 800 sources.")
            paper = next((p for p in job["papers"] if p["id"] == paper_id), None) if paper_id else None
            if paper_id and paper is None:
                raise HTTPException(404, "Source not found in this job.")
            folder = store.folder(job["id"])
            filename = Path(file.filename or "Comparison paper").name[:255]
            path = None
            try:
                path = save_upload(file, folder, "upload", max_bytes=MAX_SOURCE_UPLOAD)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if any(p.get("sha256") == digest and p["id"] != paper_id for p in job["papers"]):
                    raise HTTPException(409, "This comparison paper has already been added.")
                document = parse_file(path, profile="source")
                if job.get("workflow") == "manual":
                    fingerprint = text_fingerprint(document["text"])
                    if fingerprint == text_fingerprint(job["document"]["text"]):
                        raise HTTPException(422, "This is an identical copy of your paper. Choose a different comparison paper.")
                    if any(p.get("text_fingerprint") == fingerprint and p["id"] != paper_id for p in job["papers"]):
                        raise HTTPException(409, "This comparison text has already been added, possibly under another filename.")
            except (HTTPException, ParseFailure) as exc:
                if path:
                    path.unlink(missing_ok=True)
                if job.get("workflow") != "manual":
                    if isinstance(exc, HTTPException):
                        raise
                    raise HTTPException(422, str(exc)) from exc
                failure = {
                    "id": str(uuid4()), "title": filename, "filename": filename, "origins": ["manual"],
                    "provider": "manual", "status": "unavailable", "parsed": False, "compared": False,
                    "upload_failed": True, "error": str(exc.detail) if isinstance(exc, HTTPException) else str(exc),
                    "warnings": [],
                }
                def record_failure(current):
                    current["papers"].append(failure)
                    invalidate(current)
                return public_job(store.update(job["id"], record_failure))
            source_id = paper_id or str(uuid4())
            document_path = folder / f"source-{source_id}.json"
            write_json(document_path, document)
            if paper is None:
                paper = {
                    "id": source_id, "title": title[:1000] or document.get("title", "Supplementary source"),
                    "doi": None, "url": None, "origins": ["manual"], "provider": "manual",
                    "provenance": "User-supplied local full text; user is responsible for lawful access.",
                }
                job["papers"].append(paper)
            paper.update(
                file=path.name, document_file=document_path.name, status="parsed",
                resolved=True, downloaded=True, parsed=True, compared=False,
                warnings=document.get("warnings", []), error=None,
                sha256=digest,
                filename=filename, text_fingerprint=text_fingerprint(document["text"]),
            )

            def change(current):
                current["papers"] = job["papers"]
                invalidate(current)
            return public_job(store.update(job["id"], change))

    @app.delete("/api/jobs/{job_id}/sources/{source_id}")
    def remove_manual_source(job_id: str, source_id: str):
        with engine.lock:
            job = get_job(job_id)
            editable(job)
            if job.get("workflow") != "manual" or job.get("report_available"):
                raise HTTPException(409, "Only files in a new manual comparison can be removed.")
            if not any(p["id"] == source_id for p in job["papers"]):
                raise HTTPException(404, "Comparison paper not found.")
            def change(current):
                for paper in current["papers"]:
                    if paper["id"] == source_id:
                        paper.update(excluded=True, exclusion_reason="Removed from comparison by user.", status="excluded", compared=False)
                invalidate(current)
            return public_job(store.update(job["id"], change))

    @app.patch("/api/jobs/{job_id}/sources/{source_id}")
    def select_manual_source(job_id: str, source_id: str, body: SourceSelection):
        with engine.lock:
            job = get_job(job_id)
            editable(job)
            if job.get("workflow") != "manual" or job.get("report_available"):
                raise HTTPException(409, "Start a new manual comparison to change source selection.")
            if not any(p["id"] == source_id for p in job["papers"]):
                raise HTTPException(404, "Comparison paper not found.")
            if body.excluded and not body.reason.strip():
                raise HTTPException(422, "Give a reason for excluding this paper.")
            def change(current):
                for paper in current["papers"]:
                    if paper["id"] == source_id:
                        paper.update(excluded=body.excluded, exclusion_reason=body.reason.strip() if body.excluded else None,
                                     doi=body.doi.strip() if body.doi else None,
                                     status="excluded" if body.excluded else "parsed" if paper.get("parsed") else "unavailable")
                invalidate(current)
            return public_job(store.update(job["id"], change))

    @app.post("/api/jobs/{job_id}/consent-preview")
    def consent_preview(job_id: str, body: ProviderChoice):
        with engine.lock:
            job = get_job(job_id)
            editable(job)
            if job.get("workflow") == "manual":
                raise HTTPException(409, "Manual comparisons never use external services.")
            payload = disclosure(job, body.provider)
            token = secrets.token_urlsafe(32)
            store.update(job["id"], lambda current: current.update(consent={
                "token": token, "digest": consent_digest(payload), "expires": time.time() + 900,
            }))
            return {
                "token": token, "payload": payload,
                "destinations": destinations(body.provider),
                "description": "Approve only after reviewing the exact queries and references below. Crossref discovery also resolves its returned public DOIs through OpenAlex for authorized OA PDFs; both API destinations are listed. Contact email may accompany bibliographic requests; an optional OpenAlex credential goes only to OpenAlex. No manuscript/full text is transmitted. PDF downloads disclose your IP to each host. Provider results determine PDF destinations, so exact hosts cannot be known before discovery. Previous consent without this OA-resolution destination is not valid.",
            }

    @app.post("/api/jobs/{job_id}/run", status_code=202)
    def run_job(job_id: str, body: RunJob):
        with engine.lock:
            job = get_job(job_id)
            editable(job)
            if job.get("workflow") == "manual" and body.mode != "offline":
                raise HTTPException(409, "Manual comparisons can only compare locally uploaded papers.")
            if sum(current["status"] in ACTIVE for current in store.list()) >= 2:
                raise HTTPException(429, "Two screenings are already active. Wait or cancel one.")
            if body.mode == "offline" and not any(paper.get("parsed") and paper.get("document_file") and not paper.get("excluded") for paper in job["papers"]):
                raise HTTPException(
                    422,
                    "Add at least one readable comparison paper before starting."
                    if job.get("workflow") == "manual" else
                    "No local articles are available to compare. Offline mode does not discover papers. "
                    "Add lawful source files, or choose online discovery and explicitly approve the metadata preview.",
                )
            if body.mode == "online":
                consent = job.get("consent")
                if (
                    not consent or not body.consent_token
                    or not secrets.compare_digest(consent["token"], body.consent_token)
                    or consent["expires"] < time.time()
                    or consent["digest"] != consent_digest(disclosure(job, body.provider))
                ):
                    raise HTTPException(409, "Review and explicitly approve a fresh metadata consent preview before discovery.")
                if not job["queries"] and not job["references"]:
                    raise HTTPException(422, "Enter a topical query or reference before discovery.")
            updated = store.update(job["id"], lambda current: current.update(
                status="queued", stage="queued", progress=0, mode=body.mode, provider=body.provider,
                consent=None, report_available=False, cancel_requested=False, message="Screening queued.",
                warnings=list(current["document"].get("warnings", [])),
                comparison_settings={"exclude_quotes": body.exclude_quotes, "comparison_model": body.comparison_model},
                papers=[
                    {**paper, "compared": False, "partial_comparison": False, "comparison_status": "pending",
                     "status": "excluded" if paper.get("excluded") else "parsed" if paper.get("parsed") else paper["status"]}
                    for paper in current["papers"]
                ],
                consent_audit={
                    "approved_at": now(), "scope_version": 2,
                    "digest": consent_digest(disclosure(current, body.provider)),
                    "destinations": destinations(body.provider),
                } if body.mode == "online" else None,
            ))
            engine.start(job["id"], body.mode, body.provider)
            return public_job(updated)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        job = get_job(job_id)
        if job["status"] not in ACTIVE:
            raise HTTPException(409, "This screening is not running.")
        return public_job(engine.cancel(job["id"]))

    def report_path(job_id: str, suffix: str) -> Path:
        job = get_job(job_id)
        if not job.get("report_available"):
            raise HTTPException(409, "No current report is available. Finish a screening first.")
        path = store.folder(job["id"]) / f"report.{suffix}"
        if not path.exists():
            raise HTTPException(409, "Report artifact is missing. Run screening again.")
        return path

    @app.get("/api/jobs/{job_id}/report")
    def report(job_id: str):
        value = summarize_report(json.loads(report_path(job_id, "json").read_text(encoding="utf-8")))
        if not value["reader"]["pages"]:
            value["reader"] = manuscript_reader(value, get_job(job_id)["document"].get("pages", []))
        return value

    @app.get("/api/jobs/{job_id}/report.json")
    def download_json(job_id: str):
        return JSONResponse(report(job_id), headers={"Content-Disposition": 'attachment; filename="paper-overlap-report.json"'})

    @app.get("/api/jobs/{job_id}/report.pdf")
    def download_pdf(job_id: str, download: bool = False):
        job = get_job(job_id)
        raw = report_path(job_id, "json").read_bytes()
        try:
            path, mapping = cached_pdf(store.folder(job["id"]), job, json.loads(raw), raw)
        except PdfReportError as exc:
            raise HTTPException(422, str(exc)) from exc
        return FileResponse(path, media_type="application/pdf", filename="paper-overlap-report.pdf",
                            content_disposition_type="attachment" if download else "inline",
                            headers={"X-Buna-Pdf-Unmapped-Ranges": str(mapping["unmapped_regions"]),
                                     "X-Buna-Pdf-Mode": mapping["render_mode"],
                                     "X-Buna-Pdf-Renderer": mapping["renderer_version"]})

    @app.get("/api/jobs/{job_id}/report.pdf-mapping.json")
    def pdf_mapping(job_id: str):
        job = get_job(job_id)
        raw = report_path(job_id, "json").read_bytes()
        try:
            _, mapping = cached_pdf(store.folder(job["id"]), job, json.loads(raw), raw)
        except PdfReportError as exc:
            raise HTTPException(422, str(exc)) from exc
        return mapping

    @app.get("/api/jobs/{job_id}/sources/{source_id}/text")
    def source_text(job_id: str, source_id: str):
        job = get_job(job_id)
        current_report = report(job_id)
        paper = next((p for p in job["papers"] if p["id"] == source_id), None)
        if paper is None:
            raise HTTPException(404, "Source not found in this comparison.")
        if not paper.get("document_file"):
            raise HTTPException(409, "No readable local source text is available.")
        path = store.folder(job["id"]) / paper["document_file"]
        if not path.exists():
            raise HTTPException(409, "Saved source text is unavailable.")
        document = json.loads(path.read_text(encoding="utf-8"))
        spans = []
        for match in current_report.get("matches", []):
            if str(match["source_id"]) == source_id:
                passage = match["source"]
                spans.extend((passage["start"] + a, passage["start"] + b) for a, b in passage.get("highlights", []))
        pages, offset = [], 0
        for page in document["pages"]:
            end = offset + len(page["text"])
            pages.append({"number": page["number"], "text": page["text"],
                          "highlights": [[max(a, offset) - offset, min(b, end) - offset]
                                         for a, b in sorted(set(spans)) if a < end and b > offset]})
            offset = end + 2
        return {"title": paper.get("filename") or paper["title"], "pages": pages,
                "scope": "Local uploaded source only; highlights come from the saved report, not a new comparison."}

    @app.get("/api/jobs/{job_id}/report.html")
    def download_html(job_id: str):
        return HTMLResponse(
            render_report(report(job_id)),
            headers={"Content-Disposition": 'attachment; filename="paper-overlap-report.html"'},
        )

    static = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if static.exists():
        app.mount("/", StaticFiles(directory=static, html=True), name="frontend")
    else:
        @app.get("/")
        def missing_frontend():
            return JSONResponse({"message": "Build the UI: cd frontend && npm ci && npm run build; restart the server."})
    return app
