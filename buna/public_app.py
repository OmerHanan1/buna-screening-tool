"""Anonymous public gateway. Never mounts or serves the local application's store."""
import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import re
import hmac
import psutil
import logging
from uuid import uuid4, UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from buna.public_corpus import load_corpus
from buna.team_auth import TeamConfig, TeamAuthenticator
from buna.hosted_runtime import WALL_SECONDS, ERRORS, read_artifact, safe_progress, job_storage_bytes
from buna.pdf_reports import PDF_RENDERER_VERSION

RETENTION = 3600
BODY_LIMIT = 32 * 1024 * 1024
MAX_JOBS = 10
MAX_DISK = 768 * 1024 * 1024
logger = logging.getLogger(__name__)


class EmailGateInput(BaseModel):
    email: str = Field(max_length=254)


class PublicBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = BODY_LIMIT + 1
        if length < 0 or length > BODY_LIMIT:
            return await JSONResponse({"detail": "Upload exceeds the 32 MiB request limit."}, 413)(scope, receive, send)
        total = 0
        async def bounded():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > BODY_LIMIT:
                raise HTTPException(413, "Upload exceeds the request limit.")
            return message
        return await self.app(scope, bounded, send)


def create_public_app(corpus_root: Path | None = None, runtime_root: Path | None = None,
                      *, worker_runner=None, team_authenticator=None, shared_store=None, source_runner=None) -> FastAPI:
    corpus_root = (corpus_root or Path(os.environ["BUNA_PUBLIC_CORPUS"])).resolve()
    access_mode = os.environ.get("BUNA_ACCESS_MODE", "")
    if access_mode not in {"", "anonymous", "team", "email-gate"}:
        raise RuntimeError("Unknown access policy; refusing startup.")
    email_gate = access_mode == "email-gate"
    allowed_emails = tuple(entry.strip().casefold() for entry in
                           os.environ.get("BUNA_ALLOWED_EMAIL", "").split(",")) if email_gate else ()
    if email_gate and any(len(entry) > 254 or not re.fullmatch(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]+", entry)
                          for entry in allowed_emails):
        raise RuntimeError("Email gate requires valid comma-separated email allowlist entries.")
    team_config = None if email_gate else TeamConfig.from_environment()
    team_auth = team_authenticator or (TeamAuthenticator(team_config) if team_config else None)
    if access_mode == "team" and team_auth is None:
        raise RuntimeError("Team policy requires complete Microsoft authentication configuration.")
    private_manifest_sha = os.environ.get("BUNA_TEAM_CORPUS_SHA", "")
    attested_manifest_sha = os.environ.get("BUNA_ATTESTED_CORPUS_SHA", "")
    if private_manifest_sha and attested_manifest_sha:
        raise RuntimeError("Conflicting corpus permission policies.")
    if attested_manifest_sha:
        if not email_gate:
            raise RuntimeError("Attested hosted corpus requires the explicitly configured email gate.")
        from buna.team_corpus import load_attested_corpus
        papers = load_attested_corpus(corpus_root, attested_manifest_sha)
    elif private_manifest_sha:
        if not team_config or email_gate:
            raise RuntimeError("Private corpus requires complete team authentication configuration.")
        from buna.team_corpus import load_team_corpus
        papers = load_team_corpus(corpus_root, private_manifest_sha, team_config.tenant, team_config.owner_oid)
    else:
        papers = load_corpus(corpus_root)
    from buna.shared_library import SharedLibrary, AzureBlobStore, SharedLibraryError, MAX_PARSED_BYTES
    shared_settings = [os.environ.get(name, "") for name in
                       ("BUNA_SHARED_ACCOUNT_URL", "BUNA_SHARED_CONTAINER", "BUNA_SHARED_IDENTITY_CLIENT_ID")]
    if any(shared_settings) and not all(shared_settings):
        raise RuntimeError("Incomplete shared-library storage configuration.")
    if shared_store is not None or all(shared_settings):
        if not (email_gate or team_auth):
            raise RuntimeError("Shared saving requires an explicitly configured access gate.")
        shared = SharedLibrary(shared_store or AzureBlobStore(*shared_settings), {p["sha256"] for p in papers})
    else:
        shared = None
    origin = os.environ.get("BUNA_PUBLIC_ORIGIN", "")
    hosts = os.environ.get("BUNA_PUBLIC_HOSTS", "localhost,127.0.0.1,testserver").split(",")
    if worker_runner is None and (sys.platform != "linux" or os.geteuid() != 0 or not origin.startswith("https://")):
        raise RuntimeError("Public service requires its isolated Linux container and exact HTTPS frontend origin.")
    if worker_runner is None:
        from buna.public_selftest import verify_kernel
        verify_kernel()
    root = Path(tempfile.mkdtemp(prefix="public-runtime-", dir=runtime_root))
    os.chmod(root, 0o711)
    results_root = root / "results"
    results_root.mkdir(mode=0o700)
    save_root = root / "pending-saves"
    save_root.mkdir(mode=0o700)
    shared_cache = root / "shared-cache"
    shared_cache.mkdir(mode=0o711)
    index = root / "index.sqlite3"
    lock = threading.RLock()
    gate = threading.Lock()
    active = {}
    requests = defaultdict(deque)
    gate_attempts = defaultdict(deque)
    stopping = threading.Event()

    def connect():
        db = sqlite3.connect(index)
        db.row_factory = sqlite3.Row
        return db

    with connect() as db:
        db.executescript("""
        CREATE TABLE sessions(token TEXT PRIMARY KEY,created REAL NOT NULL);
        CREATE TABLE jobs(id TEXT PRIMARY KEY,owner TEXT NOT NULL,created REAL NOT NULL,status TEXT NOT NULL,uid INTEGER);
        CREATE TABLE submissions(created REAL NOT NULL,owner TEXT NOT NULL);
        CREATE TABLE identities(id INTEGER PRIMARY KEY AUTOINCREMENT);
        CREATE TABLE diagnostics(job TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE request_keys(owner TEXT NOT NULL,key TEXT NOT NULL,job TEXT NOT NULL,PRIMARY KEY(owner,key));
        CREATE TABLE library_saves(job TEXT PRIMARY KEY,data TEXT NOT NULL);
        """)
    os.chmod(index, 0o600)

    def cleanup():
        cleanup_source_saves()
        cleanup_source_uploads()
        with lock, connect() as db:
            rows = db.execute("SELECT id FROM jobs WHERE created<?", (time.time() - RETENTION,)).fetchall()
            for row in rows:
                if row["id"] not in active:
                    shutil.rmtree(root / row["id"], ignore_errors=False)
                    if (results_root / row["id"]).exists():
                        shutil.rmtree(results_root / row["id"])
                    if (save_root / row["id"]).exists():
                        shutil.rmtree(save_root / row["id"])
                    db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
                    db.execute("DELETE FROM diagnostics WHERE job=?", (row["id"],))
                    db.execute("DELETE FROM request_keys WHERE job=?", (row["id"],))
                    db.execute("DELETE FROM library_saves WHERE job=?", (row["id"],))
            db.execute("DELETE FROM sessions WHERE created<?", (time.time() - 4 * RETENTION,))
            db.execute("DELETE FROM submissions WHERE created<?", (time.time() - 86400,))
            now = time.time()
            for key in list(requests):
                if not requests[key] or requests[key][-1] < now - 60:
                    del requests[key]
            for key in list(gate_attempts):
                if not gate_attempts[key] or gate_attempts[key][-1] < now - 60:
                    del gate_attempts[key]

    def maintenance():
        while not stopping.wait(60):
            cleanup()

    @asynccontextmanager
    async def lifespan(app):
        thread = threading.Thread(target=maintenance, daemon=True)
        thread.start()
        yield
        stopping.set()
        thread.join(timeout=2)
        with lock:
            for process in active.values():
                if process:
                    os.killpg(process.pid, signal.SIGKILL)
        # Runtime is ephemeral; platform removes it after the process/container exits.

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.shared_library = shared
    app.add_middleware(PublicBodyLimit)
    app.add_middleware(CORSMiddleware, allow_origins=[origin] if origin else [],
                       allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
                       expose_headers=["Content-Disposition", "Retry-After"], allow_credentials=False)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    def session(request):
        if team_auth:
            return getattr(request.state, "team_owner", None) or team_auth.authenticate(request.headers.get("authorization", ""))
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer ") or len(authorization) > 128:
            raise HTTPException(401, "A visitor session is required.")
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        with connect() as db:
            row = db.execute("SELECT created FROM sessions WHERE token=?", (digest,)).fetchone()
        if not row or row["created"] < time.time() - 4 * RETENTION:
            raise HTTPException(401, "Session expired or service restarted. Start a new session.")
        return digest

    def owned(job_id, owner):
        with connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
        if not row or row["created"] < time.time() - RETENTION:
            raise HTTPException(404, "Comparison unavailable or expired.")
        return row

    @app.middleware("http")
    async def admission(request, call_next):
        rate_headers = {"Retry-After": "60"}
        if origin and request.headers.get("origin") == origin:
            rate_headers.update({"Access-Control-Allow-Origin": origin, "Access-Control-Expose-Headers": "Retry-After", "Vary": "Origin"})
        if request.url.path != "/health":
            now = time.time()
            # A global ceiling remains effective even if client IP/proxy headers are spoofed.
            key = request.client.host if request.client else "unknown"
            with lock:
                if len(requests) > 4096:
                    return JSONResponse({"detail": "Service busy."}, 429)
                for bucket, limit in (("global", 300), (key, 90)):
                    q = requests[bucket]
                    while q and q[0] < now - 60:
                        q.popleft()
                    if len(q) >= limit:
                        return JSONResponse({"detail": "Rate limit reached; wait a minute."}, 429, headers=rate_headers)
                requests["global"].append(now)
                requests[key].append(now)
        supplied_origin = request.headers.get("origin")
        if supplied_origin and supplied_origin != origin:
            return JSONResponse({"detail": "Origin not permitted."}, 403)
        if team_auth and request.url.path.startswith("/api/") and request.url.path != "/api/auth/config" and request.method != "OPTIONS":
            try:
                request.state.team_owner = await run_in_threadpool(team_auth.authenticate, request.headers.get("authorization", ""))
            except HTTPException as exc:
                headers = {"Cache-Control": "no-store"}
                if supplied_origin == origin:
                    headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
                return JSONResponse({"detail": exc.detail}, exc.status_code,
                                    headers=headers)
        if email_gate and request.url.path.startswith("/api/") and request.url.path not in {"/api/auth/config", "/api/public/session"} and request.method != "OPTIONS":
            try:
                session(request)
            except HTTPException as exc:
                headers = {"Cache-Control": "no-store"}
                if supplied_origin == origin:
                    headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
                return JSONResponse({"detail": exc.detail}, exc.status_code, headers=headers)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        return response

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "email-gate" if email_gate else "team-restricted" if team_auth else "public-isolated",
                "engine": "2.5.3", "pdf_renderer": PDF_RENDERER_VERSION, "runtime": "cached-corpus-v1"}

    @app.get("/api/auth/config")
    def auth_config():
        return {"mode": "email-gate", "identity_verified": False} if email_gate else team_auth.config.public() if team_auth else {"mode": "anonymous"}

    @app.post("/api/public/session")
    def new_session(request: Request, body: EmailGateInput | None = None):
        if team_auth:
            raise HTTPException(405, "Anonymous sessions are disabled; use Microsoft sign-in.")
        if email_gate:
            key = request.client.host if request.client else "unknown"
            now = time.time()
            with lock:
                attempts = gate_attempts[key]
                while attempts and attempts[0] < now - 60:
                    attempts.popleft()
                if len(attempts) >= 10:
                    raise HTTPException(429, "Too many access attempts. Try again later.")
                attempts.append(now)
            supplied = body.email.strip().casefold() if body else ""
            if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]+", supplied) or not sum(
                hmac.compare_digest(supplied.encode(), allowed_email.encode()) for allowed_email in allowed_emails
            ):
                raise HTTPException(403, "Access is not available for this entry.")
        cleanup()
        token = secrets.token_urlsafe(32)
        with lock, connect() as db:
            if db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 200:
                raise HTTPException(429, "Session capacity reached. Try later.")
            db.execute("INSERT INTO sessions VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(), time.time()))
        return {"token": token}

    def library_snapshot():
        warning = ""
        additions = []
        if shared:
            try:
                additions = shared.list()
            except Exception as exc:
                logger.warning("Shared catalog unavailable (%s).", type(exc).__name__)
                warning = "The shared library is temporarily unavailable. Only the curated papers are shown; shared saving is disabled until it reconnects."
        return papers + additions, warning

    @app.get("/api/public/library")
    def library():
        snapshot, warning = library_snapshot()
        capacity = None
        if shared and not warning:
            try:
                capacity = shared.capacity()
            except Exception as exc:
                logger.warning("Shared capacity unavailable (%s).", type(exc).__name__)
                warning = "Shared capacity could not be confirmed. Saving is disabled until it reconnects."
        return {"papers": [{k: p[k] for k in ("sha256", "title", "attribution", "license", "license_url", "source_url", "version")} for p in snapshot],
                "retention_seconds": RETENTION, "shared_saving_available": shared is not None and not warning,
                "shared_library_warning": warning, "immediate_shared_saving": shared is not None,
                "shared_capacity": capacity}

    from buna.public_source_saves import install_source_saves
    cleanup_source_saves = install_source_saves(
        app, shared=shared, root=root, connect=connect, session=session, lock=lock,
        gate=gate, active=active, stopping=stopping, runner=source_runner)
    from buna.public_source_uploads import install_source_uploads, MAX_FILES, MAX_BATCH_BYTES
    cleanup_source_uploads, snapshot_uploads = install_source_uploads(
        app, root=root, connect=connect, session=session, lock=lock)

    def persist_shared(job_id):
        with connect() as db:
            row = db.execute("SELECT data FROM library_saves WHERE job=?", (job_id,)).fetchone()
            job = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or not shared:
            return
        entries = json.loads(row[0])
        try:
            validations = json.loads(read_artifact(root / job_id, "share-validation.json", 16384))
        except (OSError, ValueError):
            validations = {}
        if not isinstance(validations, dict):
            validations = {}
        for entry in entries:
            if entry["state"] in {"saved", "already-present", "rejected", "cancelled"}:
                continue
            if job is None or job["status"] == "cancelled":
                entry.update(state="cancelled", reason="The comparison was cancelled; this paper was not saved.")
            elif not isinstance(validations.get(entry["source_id"]), dict):
                entry.update(state="rejected", reason="The comparison ended before this source was validated for sharing. It was not saved; use the independent save checkbox to try again.")
            elif validations[entry["source_id"]].get("validated") is not True:
                reason = validations[entry["source_id"]].get("reason")
                entry.update(state="rejected", reason=reason if isinstance(reason, str) and 0 < len(reason) <= 1000 else "The source did not pass shared-save validation and was not saved.")
            else:
                entry.update(state="saving", reason="")
                with connect() as db:
                    db.execute("UPDATE library_saves SET data=? WHERE job=?", (json.dumps(entries), job_id))
                try:
                    original = (save_root / job_id / (entry["sha256"] + ".pdf")).read_bytes()
                    if hashlib.sha256(original).hexdigest() != entry["sha256"]:
                        raise SharedLibraryError("The retained source fingerprint changed; nothing was saved.")
                    document = json.loads(read_artifact(root / job_id, "parsed-source-" + entry["source_id"] + ".json", MAX_PARSED_BYTES))
                    outcome = shared.save(original, document, entry["title"])
                    entry.update(state=outcome["state"], reason="")
                except SharedLibraryError as exc:
                    entry.update(state="failed", reason=str(exc))
                except Exception as exc:
                    logger.warning("Shared save not confirmed (%s).", type(exc).__name__)
                    entry.update(state="failed", reason="Persistent saving could not be confirmed. This file remains usable for this comparison; retry saving later.")
            with connect() as db:
                db.execute("UPDATE library_saves SET data=? WHERE job=?", (json.dumps(entries), job_id))

    def run_job(job_id, uid):
        folder = root / job_id
        status = "failed"
        started = time.monotonic()
        diagnostic = {"code": "worker-error", "stage": "starting"}
        operation = "launch-worker"
        def save_diagnostic():
            diagnostic.update(safe_progress(folder))
            diagnostic["elapsed_seconds"] = round(time.monotonic() - started, 1)
            with lock, connect() as db:
                db.execute("INSERT OR REPLACE INTO diagnostics VALUES(?,?)", (job_id, json.dumps(diagnostic)))
        try:
            if worker_runner:
                worker_runner(folder, uid)
            else:
                process = subprocess.Popen([sys.executable, "-m", "buna.public_worker", str(folder), str(uid)],
                                           env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": "/app",
                                                "HOME": str(folder), "TMPDIR": str(folder)},
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           start_new_session=True)
                with lock:
                    active[job_id] = process
                monitor = psutil.Process(process.pid)
                try:
                    deadline = time.monotonic() + WALL_SECONDS
                    last_progress = 0
                    while process.poll() is None:
                        operation = "monitor-worker-resources"
                        try:
                            cpu = monitor.cpu_times()
                            diagnostic["cpu_seconds"] = round(cpu.user + cpu.system, 1)
                            diagnostic["peak_rss_bytes"] = max(diagnostic.get("peak_rss_bytes", 0), monitor.memory_info().rss)
                        except psutil.NoSuchProcess:
                            pass
                        if time.monotonic() > deadline:
                            diagnostic["code"] = "wall-limit"
                            raise RuntimeError("Worker time limit.")
                        operation = "monitor-job-storage"
                        size = job_storage_bytes(folder)
                        if size > 256 * 1024 * 1024:
                            diagnostic["code"] = "storage-limit"
                            raise RuntimeError("Worker storage limit.")
                        if time.monotonic() - last_progress >= 2:
                            operation = "persist-progress"
                            save_diagnostic()
                            last_progress = time.monotonic()
                        time.sleep(.1)
                    code = process.returncode
                    operation = "worker-exit"
                    diagnostic["exit_code"] = code
                    if code:
                        diagnostic["code"] = "worker-signal" if code < 0 else "worker-error"
                        try:
                            failure = json.loads(read_artifact(folder, "failure.json", 4096))
                            if failure.get("code") in ERRORS:
                                diagnostic["code"] = failure["code"]
                        except (OSError, ValueError):
                            pass
                        raise RuntimeError("Worker failed.")
                finally:
                    # Kill descendants too, including a parser left behind after worker exit.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            operation = "collect-report"
            summary = json.loads(read_artifact(folder, "complete.json", 256 * 1024))
            if not isinstance(summary, dict):
                raise RuntimeError("Invalid result metadata.")
            public_summary = {k: summary[k] for k in ("checked", "total", "overlap_percent", "partial",
                              "warnings", "algorithm_version", "score_available", "comparison_model",
                              "classification_counts") if k in summary}
            pdf = read_artifact(folder, "report.pdf", 64 * 1024 * 1024)
            evidence = read_artifact(folder, "report.json", 64 * 1024 * 1024)
            if not pdf.startswith(b"%PDF-"):
                raise RuntimeError("Invalid PDF artifact.")
            destination = results_root / job_id
            destination.mkdir(mode=0o700)
            (destination / "complete.json").write_text(json.dumps(public_summary))
            (destination / "report.pdf").write_bytes(pdf)
            (destination / "report.json").write_bytes(evidence)
            status = "complete"
        except Exception as exc:
            status = "failed"
            diagnostic["supervisor_exception"] = type(exc).__name__
            diagnostic["supervisor_operation"] = operation
            if operation.startswith("monitor-") and diagnostic["code"] == "worker-error":
                diagnostic["code"] = "supervisor-monitor-error"
            try:
                summary = json.loads(read_artifact(folder, "comparison-complete.json", 256 * 1024))
                evidence = read_artifact(folder, "report.json", 64 * 1024 * 1024)
                if not isinstance(summary, dict):
                    raise ValueError("Invalid completed evidence metadata.")
                destination = results_root / job_id
                destination.mkdir(mode=0o700, exist_ok=True)
                (destination / "complete.json").write_text(json.dumps({
                    key: summary[key] for key in ("checked", "total", "overlap_percent", "partial", "warnings",
                                                 "algorithm_version", "score_available", "comparison_model",
                                                 "classification_counts") if key in summary
                }))
                (destination / "report.json").write_bytes(evidence)
                diagnostic["code"] = "pdf-error"
                status = "report-failed"
            except (OSError, ValueError):
                pass
        finally:
            try:
                save_diagnostic()
                with lock, connect() as db:
                    db.execute("UPDATE jobs SET status=? WHERE id=? AND status='running'", (status, job_id))
                persist_shared(job_id)
                with lock:
                    active.pop(job_id, None)
            finally:
                gate.release()

    @app.post("/api/public/jobs", status_code=202)
    async def submit(request: Request):
        owner = session(request)
        request_key = request.headers.get("idempotency-key")
        if request_key:
            try:
                request_key = str(UUID(request_key))
            except ValueError:
                raise HTTPException(422, "Invalid submission reference.") from None
            with lock, connect() as db:
                prior = db.execute("SELECT j.id,j.status FROM request_keys r JOIN jobs j ON j.id=r.job "
                                   "WHERE r.owner=? AND r.key=? AND j.created>=?",
                                   (owner, request_key, time.time() - RETENTION)).fetchone()
            if prior:
                return {"id": prior["id"], "status": prior["status"]}
        if not gate.acquire(blocking=False):
            raise HTTPException(429, "A comparison is running. Please try again shortly.")
        job_id = str(uuid4())
        folder = root / job_id
        accepted = False
        created_at = None
        try:
            cleanup()
            with lock, connect() as db:
                if db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] >= MAX_JOBS:
                    raise HTTPException(429, "Daily service comparison limit reached.")
                if db.execute("SELECT COUNT(*) FROM submissions WHERE owner=?", (owner,)).fetchone()[0] >= 3:
                    raise HTTPException(429, "Session comparison limit reached.")
                if job_storage_bytes(root) > MAX_DISK - 320 * 1024 * 1024:
                    raise HTTPException(429, "Temporary storage is full. Try later.")
                # Never recycle OS identities when quota/retention rows expire.
                uid = 10000 + db.execute("INSERT INTO identities DEFAULT VALUES").lastrowid
                if uid >= 60000:
                    raise HTTPException(503, "Service identity capacity exhausted; maintenance required.")
            folder.mkdir(mode=0o700)
            async with request.form(max_files=MAX_FILES + 1, max_fields=6, max_part_size=16384) as form:
                selected = json.loads(str(form.get("selected", "[]")))
                upload_ids = json.loads(str(form.get("uploaded_sources", "[]")))
                keep_indices = json.loads(str(form.get("save_sources", "[]")))
                if (not isinstance(keep_indices, list) or any(type(i) is not int for i in keep_indices)
                        or len(keep_indices) != len(set(keep_indices))):
                    raise HTTPException(422, "Invalid shared-save selection.")
                if keep_indices and (shared is None or str(form.get("share_authorized", "")) != "true"):
                    raise HTTPException(422, "Shared saving requires explicit hosted/shared-use authorization and available storage.")
                model = str(form.get("comparison_model", "validated-lexical"))
                if model not in {"validated-lexical", "classified-v1.1"}:
                    raise HTTPException(422, "Select a supported comparison model.")
                if not isinstance(selected, list) or not all(isinstance(i, str) for i in selected) or len(selected) != len(set(selected)):
                    raise HTTPException(422, "Invalid corpus selection.")
                snapshot, shared_warning = await run_in_threadpool(library_snapshot)
                approved = {p["sha256"]: p for p in snapshot}
                if not all(isinstance(i, str) and i in approved for i in selected):
                    raise HTTPException(503 if shared_warning else 422,
                                        shared_warning or "Only currently available comparison papers can be selected.")
                target = form.get("target")
                if not hasattr(target, "read"):
                    raise HTTPException(422, "Upload your manuscript.")
                async def save(upload, prefix, maximum):
                    suffix = Path(upload.filename or "").suffix.lower()
                    if suffix not in {".pdf", ".txt"}:
                        raise HTTPException(415, "Only PDF or plain text is supported.")
                    file = folder / (prefix + suffix)
                    total = 0
                    with file.open("xb") as output:
                        while chunk := await upload.read(65536):
                            total += len(chunk)
                            if total > maximum:
                                raise HTTPException(413, "Public file size limit exceeded.")
                            output.write(chunk)
                    if not total:
                        raise HTTPException(422, "Empty file.")
                    return file.name
                target_name = await save(target, "target", 10 * 1024 * 1024)
                target_digest = hashlib.sha256((folder / target_name).read_bytes()).hexdigest()
                sources = []
                selected_ids = {}
                # No worker is active while admission owns the gate; immutable
                # source caches from completed jobs can be safely evicted here.
                for cached in shared_cache.glob("shared-*.json"):
                    cached.unlink()
                shared_bytes = 0
                for digest in selected:
                    paper = approved[digest]
                    selected_ids[digest] = str(len(sources) + 1)
                    cache = paper.get("parsed_cache")
                    if paper.get("state") == "ready":
                        shared_bytes += paper["parsed_bytes"]
                        if shared_bytes > 192 * 1024 * 1024:
                            raise HTTPException(422, "Selected shared papers exceed the 192 MiB per-comparison cache limit. Deselect some papers.")
                        if job_storage_bytes(root) + paper["parsed_bytes"] > MAX_DISK - 320 * 1024 * 1024:
                            raise HTTPException(429, "Shared source snapshots exceed current temporary capacity. Retry after other work expires.")
                        try:
                            sources.append(await run_in_threadpool(shared.materialize, paper, shared_cache))
                        except Exception as exc:
                            logger.warning("Shared source unavailable during snapshot (%s).", type(exc).__name__)
                            sources.append({"title": paper["title"], "path": "", "unavailable_reason": "Shared source could not be loaded; it was not checked."})
                    elif cache:
                        sources.append({"path": str(corpus_root / paper["filename"]), "title": paper["title"],
                                        "cached_document": str(corpus_root / cache["filename"]),
                                        "cached_sha256": cache["sha256"]})
                    else:
                        name = "curated-" + paper["filename"]
                        shutil.copyfile(corpus_root / paper["filename"], folder / name)
                        sources.append({"path": name, "title": paper["title"]})
                manual = form.getlist("sources")
                if not isinstance(upload_ids, list) or len(manual) + len(upload_ids) > MAX_FILES:
                    raise HTTPException(422, "At most 50 personal comparison files. Use individual uploads for batches exceeding 32 MiB.")
                if any(i < 0 or i >= len(manual) for i in keep_indices):
                    raise HTTPException(422, "Only supplementary comparison files can be selected for sharing.")
                saves = []
                for i, upload in enumerate(manual):
                    if not hasattr(upload, "read"):
                        raise HTTPException(422, "Invalid source file.")
                    name = await save(upload, f"source-{i}", 8 * 1024 * 1024)
                    title = Path(upload.filename or "Source").name[:255]
                    content = (folder / name).read_bytes()
                    digest = hashlib.sha256(content).hexdigest()
                    if i in keep_indices and Path(name).suffix != ".pdf":
                        raise HTTPException(422, "Only supplementary PDFs can be kept in the shared library.")
                    if digest not in selected_ids:
                        selected_ids[digest] = str(len(sources) + 1)
                        sources.append({"path": name, "title": title, "keep_in_library": i in keep_indices})
                    elif i in keep_indices:
                        sources[int(selected_ids[digest]) - 1]["keep_in_library"] = True
                    if i in keep_indices and not any(entry["sha256"] == digest for entry in saves):
                        state, reason = "pending", ""
                        if digest == target_digest:
                            state, reason = "rejected", "This is identical to your manuscript and was not shared."
                        elif digest in approved:
                            state = "already-present"
                        else:
                            retained = save_root / job_id
                            retained.mkdir(mode=0o700, exist_ok=True)
                            (retained / (digest + ".pdf")).write_bytes(content)
                        saves.append({"source_id": selected_ids[digest], "sha256": digest, "title": title,
                                      "state": state, "reason": reason})
                staged = snapshot_uploads(upload_ids, owner, folder)
                uploaded_bytes = sum((folder / entry["path"]).stat().st_size for entry in staged)
                uploaded_bytes += sum((folder / name).stat().st_size for name in
                                      [p.name for p in folder.glob("source-*")])
                if uploaded_bytes > MAX_BATCH_BYTES:
                    raise HTTPException(413, "Comparison uploads exceed the 128 MiB batch budget.")
                for entry in staged:
                    if entry["sha256"] not in selected_ids:
                        selected_ids[entry["sha256"]] = str(len(sources) + 1)
                        sources.append(entry)
                    else:
                        (folder / entry["path"]).unlink()
                if job_storage_bytes(folder) > 144 * 1024 * 1024:
                    raise HTTPException(413, "Selected original files exceed the 144 MiB working-input budget. Deselect some uncached papers.")
                if not sources:
                    raise HTTPException(422, "Select or upload at least one comparison paper.")
                attribution_fields = ("title", "attribution", "version", "license", "license_url", "source_url")
                value = {"target": target_name, "title": Path(target.filename or "Manuscript").name[:255],
                         "sources": sources, "attributions": [{key: approved[i][key] for key in attribution_fields} for i in selected],
                         "reports_only": team_auth is not None or email_gate, "comparison_model": model}
                (folder / "request.json").write_text(json.dumps(value))
            if not worker_runner:
                os.chown(folder, uid, uid)
                for path in folder.iterdir():
                    os.chown(path, uid, uid)
                    os.chmod(path, 0o600)
            with lock, connect() as db:
                created_at = time.time()
                db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (job_id, owner, created_at, "running", uid))
                db.execute("INSERT INTO submissions VALUES(?,?)", (created_at, owner))
                if request_key:
                    db.execute("INSERT INTO request_keys VALUES(?,?,?)", (owner, request_key, job_id))
                if saves:
                    db.execute("INSERT INTO library_saves VALUES(?,?)", (job_id, json.dumps(saves)))
                active[job_id] = None
            thread = threading.Thread(target=run_job, args=(job_id, uid), daemon=True)
            try:
                thread.start()
            except RuntimeError as exc:
                raise HTTPException(503, "The worker could not start. No comparison was submitted; retry shortly.") from exc
            accepted = True
            return {"id": job_id, "status": "running", "source_count": len(sources)}
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(422, "Invalid upload fields.") from exc
        finally:
            if not accepted:
                with lock, connect() as db:
                    active.pop(job_id, None)
                    db.execute("DELETE FROM request_keys WHERE job=?", (job_id,))
                    db.execute("DELETE FROM diagnostics WHERE job=?", (job_id,))
                    db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                    db.execute("DELETE FROM library_saves WHERE job=?", (job_id,))
                    if created_at is not None:
                        db.execute("DELETE FROM submissions WHERE owner=? AND created=?", (owner, created_at))
                if folder.exists():
                    shutil.rmtree(folder)
                if (save_root / job_id).exists():
                    shutil.rmtree(save_root / job_id)
                gate.release()

    @app.get("/api/public/jobs/{job_id}")
    def status(job_id: str, request: Request):
        row = owned(job_id, session(request))
        value = {"id": job_id, "status": row["status"]}
        if row["status"] in {"complete", "report-failed"}:
            value.update(json.loads((results_root / job_id / "complete.json").read_text()))
        with connect() as db:
            entry = db.execute("SELECT data FROM diagnostics WHERE job=?", (job_id,)).fetchone()
        diagnostic = json.loads(entry[0]) if entry else {}
        with connect() as db:
            saving = db.execute("SELECT data FROM library_saves WHERE job=?", (job_id,)).fetchone()
        value["library_saves"] = [{key: item[key] for key in ("source_id", "title", "state", "reason")}
                                 for item in json.loads(saving[0])] if saving else []
        value["progress"] = {key: diagnostic[key] for key in ("stage", "source_index", "source_count",
                             "processed_sources", "checked_sources", "source_windows_visited",
                             "source_windows_total", "elapsed_seconds") if key in diagnostic}
        if row["status"] in {"failed", "report-failed"}:
            code = diagnostic.get("code", "worker-error")
            value.update(error=ERRORS.get(code, ERRORS["worker-error"]), error_code=code,
                         diagnostic_id=job_id, evidence_available=row["status"] == "report-failed")
        return value

    @app.get("/api/public/jobs/{job_id}/report.{format}")
    def report(job_id: str, format: str, request: Request):
        row = owned(job_id, session(request))
        if format not in {"pdf", "json"}:
            raise HTTPException(404)
        if row["status"] != "complete" and not (format == "json" and row["status"] == "report-failed"):
            raise HTTPException(409, "Report is not ready.")
        return FileResponse(results_root / job_id / f"report.{format}", filename=f"paper-overlap-report.{format}",
                            media_type="application/pdf" if format == "pdf" else "application/json")

    @app.delete("/api/public/jobs/{job_id}")
    def delete(job_id: str, request: Request):
        owner = session(request)
        with lock:
            # Status is read under the same lock used to enter publication.
            # A stale "running" snapshot must never acknowledge cancellation
            # after the supervisor has completed comparison and begun saving.
            row = owned(job_id, owner)
            if job_id in active:
                if row["status"] != "running":
                    raise HTTPException(409, "Shared saving is finishing. Try deleting shortly.")
                process = active[job_id]
                if process is None:
                    raise HTTPException(409, "Worker is starting; retry shortly.")
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                with connect() as db:
                    db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job_id,))
                return {"status": "cancelled"}
            shutil.rmtree(root / job_id)
            if (results_root / job_id).exists():
                shutil.rmtree(results_root / job_id)
            if (save_root / job_id).exists():
                shutil.rmtree(save_root / job_id)
            with connect() as db:
                db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                db.execute("DELETE FROM diagnostics WHERE job=?", (job_id,))
                db.execute("DELETE FROM request_keys WHERE job=?", (job_id,))
                db.execute("DELETE FROM library_saves WHERE job=?", (job_id,))
        return {"status": "deleted"}

    @app.post("/api/public/jobs/{job_id}/library-save-retry", status_code=202)
    def retry_save(job_id: str, request: Request):
        owner = session(request)
        if not gate.acquire(blocking=False):
            raise HTTPException(429, "A comparison or shared save is in progress; retry shortly.")
        try:
            with lock:
                row = owned(job_id, owner)
                if shared is None or row["status"] not in {"complete", "report-failed", "failed"}:
                    raise HTTPException(409, "This comparison has no completed shared-save operation to retry.")
                with connect() as db:
                    record = db.execute("SELECT data FROM library_saves WHERE job=?", (job_id,)).fetchone()
                if not record or not any(item["state"] == "failed" for item in json.loads(record["data"])):
                    raise HTTPException(409, "No failed shared save needs retrying.")
                active[job_id] = None
        except Exception:
            gate.release()
            raise
        def work():
            try:
                persist_shared(job_id)
            finally:
                with lock:
                    active.pop(job_id, None)
                gate.release()
        try:
            threading.Thread(target=work, daemon=True).start()
        except RuntimeError as exc:
            with lock:
                active.pop(job_id, None)
            gate.release()
            raise HTTPException(503, "The shared-save worker could not start.") from exc
        return {"status": "saving"}

    return app
