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
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from buna.public_corpus import load_corpus
from buna.team_auth import TeamConfig, TeamAuthenticator

RETENTION = 3600
BODY_LIMIT = 32 * 1024 * 1024
MAX_JOBS = 10
MAX_DISK = 768 * 1024 * 1024


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
                      *, worker_runner=None, team_authenticator=None) -> FastAPI:
    corpus_root = (corpus_root or Path(os.environ["BUNA_PUBLIC_CORPUS"])).resolve()
    access_mode = os.environ.get("BUNA_ACCESS_MODE", "")
    if access_mode not in {"", "anonymous", "team", "email-gate"}:
        raise RuntimeError("Unknown access policy; refusing startup.")
    email_gate = access_mode == "email-gate"
    allowed_email = os.environ.get("BUNA_ALLOWED_EMAIL", "").strip().casefold() if email_gate else ""
    if email_gate and not re.fullmatch(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]+", allowed_email):
        raise RuntimeError("Email gate requires one configured email allowlist entry.")
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
        """)
    os.chmod(index, 0o600)

    def cleanup():
        with lock, connect() as db:
            rows = db.execute("SELECT id FROM jobs WHERE created<?", (time.time() - RETENTION,)).fetchall()
            for row in rows:
                if row["id"] not in active:
                    shutil.rmtree(root / row["id"], ignore_errors=False)
                    if (results_root / row["id"]).exists():
                        shutil.rmtree(results_root / row["id"])
                    db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
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
    app.add_middleware(PublicBodyLimit)
    app.add_middleware(CORSMiddleware, allow_origins=[origin] if origin else [],
                       allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"],
                       expose_headers=["Content-Disposition"], allow_credentials=False)
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
                        return JSONResponse({"detail": "Rate limit reached; wait a minute."}, 429)
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
        return {"status": "ok", "mode": "email-gate" if email_gate else "team-restricted" if team_auth else "public-isolated", "engine": "2.5.3", "pdf_renderer": "5"}

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
            if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]+", supplied) or not hmac.compare_digest(supplied.encode(), allowed_email.encode()):
                raise HTTPException(403, "Access is not available for this entry.")
        cleanup()
        token = secrets.token_urlsafe(32)
        with lock, connect() as db:
            if db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 200:
                raise HTTPException(429, "Session capacity reached. Try later.")
            db.execute("INSERT INTO sessions VALUES(?,?)", (hashlib.sha256(token.encode()).hexdigest(), time.time()))
        return {"token": token}

    @app.get("/api/public/library")
    def library():
        return {"papers": [{k: p[k] for k in ("sha256", "title", "attribution", "license", "license_url", "source_url", "version")} for p in papers],
                "retention_seconds": RETENTION}

    def run_job(job_id, uid):
        folder = root / job_id
        status = "failed"
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
                try:
                    deadline = time.monotonic() + 240
                    while process.poll() is None:
                        if time.monotonic() > deadline:
                            raise RuntimeError("Worker time limit.")
                        size = sum(p.lstat().st_size for p in folder.rglob("*") if not p.is_symlink())
                        if size > 256 * 1024 * 1024:
                            raise RuntimeError("Worker storage limit.")
                        time.sleep(.1)
                    code = process.returncode
                    if code:
                        raise RuntimeError("Worker failed.")
                finally:
                    # Kill descendants too, including a parser left behind after worker exit.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            def read_artifact(name, limit):
                fd = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
                        raise RuntimeError("Invalid worker artifact.")
                    data = stream.read(limit + 1)
                if len(data) > limit:
                    raise RuntimeError("Oversized worker artifact.")
                return data
            summary = json.loads(read_artifact("complete.json", 256 * 1024))
            if not isinstance(summary, dict):
                raise RuntimeError("Invalid result metadata.")
            public_summary = {k: summary[k] for k in ("checked", "total", "overlap_percent", "partial",
                              "warnings", "algorithm_version", "score_available") if k in summary}
            pdf = read_artifact("report.pdf", 64 * 1024 * 1024)
            evidence = read_artifact("report.json", 64 * 1024 * 1024)
            if not pdf.startswith(b"%PDF-"):
                raise RuntimeError("Invalid PDF artifact.")
            destination = results_root / job_id
            destination.mkdir(mode=0o700)
            (destination / "complete.json").write_text(json.dumps(public_summary))
            (destination / "report.pdf").write_bytes(pdf)
            (destination / "report.json").write_bytes(evidence)
            status = "complete"
        except Exception:
            status = "failed"
        finally:
            with lock, connect() as db:
                db.execute("UPDATE jobs SET status=? WHERE id=? AND status='running'", (status, job_id))
                active.pop(job_id, None)
            gate.release()

    @app.post("/api/public/jobs", status_code=202)
    async def submit(request: Request):
        owner = session(request)
        if not gate.acquire(blocking=False):
            raise HTTPException(429, "A comparison is running. Please try again shortly.")
        job_id = str(uuid4())
        folder = root / job_id
        accepted = False
        try:
            cleanup()
            with lock, connect() as db:
                if db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] >= MAX_JOBS:
                    raise HTTPException(429, "Daily service comparison limit reached.")
                if db.execute("SELECT COUNT(*) FROM submissions WHERE owner=?", (owner,)).fetchone()[0] >= 3:
                    raise HTTPException(429, "Session comparison limit reached.")
                if sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) > MAX_DISK - 256 * 1024 * 1024:
                    raise HTTPException(429, "Temporary storage is full. Try later.")
                # Never recycle OS identities when quota/retention rows expire.
                uid = 10000 + db.execute("INSERT INTO identities DEFAULT VALUES").lastrowid
                if uid >= 60000:
                    raise HTTPException(503, "Service identity capacity exhausted; maintenance required.")
            folder.mkdir(mode=0o700)
            async with request.form(max_files=6, max_fields=2, max_part_size=4096) as form:
                selected = json.loads(str(form.get("selected", "[]")))
                if not isinstance(selected, list) or not all(isinstance(i, str) for i in selected) or len(selected) != len(set(selected)):
                    raise HTTPException(422, "Invalid corpus selection.")
                approved = {p["sha256"]: p for p in papers}
                if not all(isinstance(i, str) and i in approved for i in selected):
                    raise HTTPException(422, "Only approved public corpus papers can be selected.")
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
                sources = []
                for digest in selected:
                    paper = approved[digest]
                    name = "curated-" + paper["filename"]
                    shutil.copyfile(corpus_root / paper["filename"], folder / name)
                    sources.append({"path": name, "title": paper["title"]})
                manual = form.getlist("sources")
                if len(manual) > 5:
                    raise HTTPException(422, "At most five personal comparison files.")
                for i, upload in enumerate(manual):
                    if not hasattr(upload, "read"):
                        raise HTTPException(422, "Invalid source file.")
                    name = await save(upload, f"source-{i}", 8 * 1024 * 1024)
                    sources.append({"path": name, "title": Path(upload.filename or "Source").name[:255]})
                if not sources:
                    raise HTTPException(422, "Select or upload at least one comparison paper.")
                attribution_fields = ("title", "attribution", "version", "license", "license_url", "source_url")
                value = {"target": target_name, "title": Path(target.filename or "Manuscript").name[:255],
                         "sources": sources, "attributions": [{key: approved[i][key] for key in attribution_fields} for i in selected],
                         "reports_only": team_auth is not None or email_gate}
                (folder / "request.json").write_text(json.dumps(value))
            if not worker_runner:
                os.chown(folder, uid, uid)
                for path in folder.iterdir():
                    os.chown(path, uid, uid)
                    os.chmod(path, 0o600)
            with lock, connect() as db:
                db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (job_id, owner, time.time(), "running", uid))
                db.execute("INSERT INTO submissions VALUES(?,?)", (time.time(), owner))
                active[job_id] = None
            thread = threading.Thread(target=run_job, args=(job_id, uid), daemon=True)
            thread.start()
            accepted = True
            return {"id": job_id, "status": "running"}
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(422, "Invalid upload fields.") from exc
        finally:
            if not accepted:
                if folder.exists():
                    shutil.rmtree(folder)
                gate.release()

    @app.get("/api/public/jobs/{job_id}")
    def status(job_id: str, request: Request):
        row = owned(job_id, session(request))
        value = {"id": job_id, "status": row["status"]}
        if row["status"] == "complete":
            value.update(json.loads((results_root / job_id / "complete.json").read_text()))
        elif row["status"] == "failed":
            value["error"] = "Comparison could not finish within safe limits, or a document could not be parsed. Try smaller text-based files."
        return value

    @app.get("/api/public/jobs/{job_id}/report.{format}")
    def report(job_id: str, format: str, request: Request):
        row = owned(job_id, session(request))
        if format not in {"pdf", "json"}:
            raise HTTPException(404)
        if row["status"] != "complete":
            raise HTTPException(409, "Report is not ready.")
        return FileResponse(results_root / job_id / f"report.{format}", filename=f"paper-overlap-report.{format}",
                            media_type="application/pdf" if format == "pdf" else "application/json")

    @app.delete("/api/public/jobs/{job_id}")
    def delete(job_id: str, request: Request):
        owned(job_id, session(request))
        with lock:
            if job_id in active:
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
            with connect() as db:
                db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return {"status": "deleted"}

    return app
