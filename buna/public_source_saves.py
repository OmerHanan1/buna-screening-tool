"""Owner-scoped standalone source saving, sharing the comparison resource gate."""
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from buna.hosted_runtime import read_artifact, job_storage_bytes
from buna.shared_library import MAX_ORIGINAL_BYTES, MAX_PARSED_BYTES, SharedLibraryError

logger = logging.getLogger(__name__)
PENDING = {"receiving", "queued", "validating", "saving"}


def install_source_saves(app, *, shared, root, connect, session, lock, gate, active,
                         stopping, runner=None):
    parent = root / "source-saves"
    parent.mkdir(mode=0o711)
    with connect() as db:
        db.execute("""CREATE TABLE source_saves(
            id TEXT PRIMARY KEY,owner TEXT NOT NULL,created REAL NOT NULL,
            state TEXT NOT NULL,reason TEXT NOT NULL,digest TEXT NOT NULL,
            title TEXT NOT NULL,uid INTEGER NOT NULL,request_key TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 1,
            UNIQUE(owner,request_key))""")
        db.execute("CREATE TABLE source_save_admissions(created REAL NOT NULL)")

    def cleanup():
        with lock, connect() as db:
            for row in db.execute("SELECT id FROM source_saves WHERE created<?", (time.time() - 3600,)).fetchall():
                if row["id"] not in active:
                    shutil.rmtree(parent / row["id"], ignore_errors=False)
                    db.execute("DELETE FROM source_saves WHERE id=?", (row["id"],))
            db.execute("DELETE FROM source_save_admissions WHERE created<?", (time.time() - 86400,))

    def owned(receipt, owner):
        with connect() as db:
            row = db.execute("SELECT * FROM source_saves WHERE id=? AND owner=? AND created>?",
                             (receipt, owner, time.time() - 3600)).fetchone()
        if row is None:
            raise HTTPException(404, "Save receipt expired or unavailable. Check the shared library; retry with the original file if it is missing.")
        return row

    def public(row):
        return {key: row[key] for key in ("id", "state", "reason", "digest")}

    def update(receipt, state, reason=""):
        with lock, connect() as db:
            db.execute("UPDATE source_saves SET state=?,reason=? WHERE id=?", (state, reason, receipt))

    def event(receipt, state, code=""):
        shared.record_save_event(receipt, state, code)

    def run(receipt):
        held = False
        process = None
        folder = parent / receipt
        work = folder / "worker"
        state, reason, code = "failed", "Saving did not finish. Retry while this workspace is open.", "save-interrupted"
        try:
            deadline = time.monotonic() + 840
            while not stopping.is_set() and time.monotonic() < deadline:
                with lock, connect() as db:
                    row = db.execute("SELECT * FROM source_saves WHERE id=?", (receipt,)).fetchone()
                    if row is None or row["state"] == "cancelled":
                        state, reason, code = "cancelled", "Saving cancelled. No shared paper was removed.", "cancelled"
                        return
                if gate.acquire(timeout=.25):
                    held = True
                    break
            if not held:
                raise SharedLibraryError("The parser queue did not become available. Retry saving.")
            with lock, connect() as db:
                row = db.execute("SELECT * FROM source_saves WHERE id=?", (receipt,)).fetchone()
                if row["state"] == "cancelled":
                    state, reason, code = "cancelled", "Saving cancelled.", "cancelled"
                    return
                db.execute("UPDATE source_saves SET state='validating' WHERE id=?", (receipt,))
            event(receipt, "validating")
            # The original stays gateway-owned; the parser receives a disposable copy.
            if work.exists():
                shutil.rmtree(work)
            work.mkdir(mode=0o700)
            shutil.copyfile(folder / "original.pdf", work / "source.pdf")
            if runner:
                runner(work, row["uid"])
            else:
                os.chown(work, row["uid"], row["uid"])
                os.chown(work / "source.pdf", row["uid"], row["uid"])
                os.chmod(work / "source.pdf", 0o600)
                process = subprocess.Popen(
                    [sys.executable, "-m", "buna.public_source_worker", str(work), str(row["uid"])],
                    env={"PATH": "/usr/local/bin:/usr/bin:/bin", "PYTHONPATH": "/app",
                         "HOME": str(work), "TMPDIR": str(work)},
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                with lock:
                    active[receipt] = process
                deadline = time.monotonic() + 45
                while process.poll() is None:
                    if time.monotonic() > deadline or job_storage_bytes(work) > 96 * 1024 * 1024:
                        raise SharedLibraryError("Source parsing exceeded safe limits. This paper was not saved.")
                    time.sleep(.1)
                if process.returncode:
                    try:
                        failure = json.loads(read_artifact(work, "failure.json", 8192))
                        reason = failure["reason"]
                        if not isinstance(reason, str) or len(reason) > 1000:
                            raise ValueError("Invalid source failure.")
                    except (OSError, ValueError, KeyError):
                        reason = "Source parsing failed or exceeded safe limits. This paper was not saved."
                    raise SharedLibraryError(reason)
            document = json.loads(read_artifact(work, "parsed.json", MAX_PARSED_BYTES))
            original = (folder / "original.pdf").read_bytes()
            if hashlib.sha256(original).hexdigest() != row["digest"]:
                raise SharedLibraryError("The retained source fingerprint changed. Nothing was saved.")
            with lock, connect() as db:
                current = db.execute("SELECT state FROM source_saves WHERE id=?", (receipt,)).fetchone()
                if current["state"] == "cancelled":
                    state, reason, code = "cancelled", "Saving cancelled.", "cancelled"
                    return
                db.execute("UPDATE source_saves SET state='saving' WHERE id=?", (receipt,))
            event(receipt, "saving")
            result = shared.save(original, document, row["title"])
            state, reason, code = result["state"], "", ""
            # "Saved" reaches the browser only after both durable commits succeeded.
            event(receipt, state)
        except SharedLibraryError as exc:
            state, reason, code = "failed", str(exc), "source-or-storage-validation"
        except Exception as exc:
            logger.warning("Standalone shared save failed (%s).", type(exc).__name__)
            state, reason, code = "failed", "Persistent saving could not be confirmed. Retry saving; comparison is not required.", "storage-or-parser-error"
        finally:
            if process:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            with lock, connect() as db:
                current = db.execute("SELECT state FROM source_saves WHERE id=?", (receipt,)).fetchone()
                if current and current["state"] == "cancelled":
                    state, reason, code = "cancelled", "Saving cancelled. No shared paper was removed.", "cancelled"
            if state not in {"saved", "already-present"}:
                try:
                    event(receipt, state, code)
                except Exception as exc:
                    logger.warning("Save diagnostic unavailable (%s).", type(exc).__name__)
                    reason += " The durable diagnostic could not be updated."
            update(receipt, state, reason)
            with lock:
                active.pop(receipt, None)
            if held:
                gate.release()

    def start(receipt):
        with lock:
            active[receipt] = None
        try:
            threading.Thread(target=run, args=(receipt,), daemon=True).start()
        except RuntimeError:
            with lock:
                active.pop(receipt, None)
            update(receipt, "failed", "The save worker could not start. Retry saving.")
            raise HTTPException(503, "The save worker could not start. Retry saving.") from None

    @app.post("/api/public/source-saves", status_code=202)
    async def submit(request: Request):
        owner = session(request)
        if shared is None:
            raise HTTPException(503, "Shared saving is unavailable.")
        key = request.headers.get("Idempotency-Key", "")
        try:
            UUID(key)
        except ValueError:
            raise HTTPException(422, "A valid save request reference is required.") from None
        with lock, connect() as db:
            prior = db.execute("SELECT * FROM source_saves WHERE owner=? AND request_key=? AND created>?",
                               (owner, key, time.time() - 3600)).fetchone()
            if prior:
                return public(prior)
            if db.execute("SELECT COUNT(*) FROM source_saves WHERE state IN ('receiving','queued','validating','saving')").fetchone()[0] >= 5:
                raise HTTPException(429, "The source-save queue is full. Retry shortly.")
            if db.execute("SELECT COUNT(*) FROM source_save_admissions WHERE created>?", (time.time() - 86400,)).fetchone()[0] >= 20:
                raise HTTPException(429, "The source-save request limit is reached. Retry later.")
            uid = 10000 + db.execute("INSERT INTO identities DEFAULT VALUES").lastrowid
            if uid >= 60000:
                raise HTTPException(503, "Service identity capacity exhausted.")
            receipt = str(uuid4())
            db.execute("INSERT INTO source_saves VALUES(?,?,?,'receiving','','','',?,?,1)",
                       (receipt, owner, time.time(), uid, key))
            db.execute("INSERT INTO source_save_admissions VALUES(?)", (time.time(),))
        folder = parent / receipt
        folder.mkdir(mode=0o711)
        try:
            async with request.form(max_files=1, max_fields=1, max_part_size=8192) as form:
                source = form.get("source")
                if str(form.get("share_authorized", "")) != "true":
                    raise HTTPException(422, "Explicit shared hosted-use authorization is required.")
                if not hasattr(source, "read") or Path(source.filename or "").suffix.lower() != ".pdf":
                    raise HTTPException(422, "Select a supplementary comparison PDF to save.")
                if job_storage_bytes(root) > 768 * 1024 * 1024 - 128 * 1024 * 1024:
                    raise HTTPException(429, "Temporary storage is full. Retry later.")
                digest, total = hashlib.sha256(), 0
                with (folder / "original.pdf").open("xb") as output:
                    os.chmod(folder / "original.pdf", 0o600)
                    while chunk := await source.read(65536):
                        total += len(chunk)
                        if total > MAX_ORIGINAL_BYTES:
                            raise HTTPException(413, "Comparison PDFs must be at most 8 MiB.")
                        digest.update(chunk)
                        output.write(chunk)
                if not total or (folder / "original.pdf").read_bytes()[:5] != b"%PDF-":
                    raise HTTPException(422, "A nonempty PDF is required.")
                await run_in_threadpool(event, receipt, "queued")
                with lock, connect() as db:
                    db.execute("UPDATE source_saves SET state='queued',digest=?,title=? WHERE id=?",
                               (digest.hexdigest(), Path(source.filename).name[:255], receipt))
            start(receipt)
            return public(owned(receipt, owner))
        except Exception as exc:
            with lock, connect() as db:
                db.execute("DELETE FROM source_saves WHERE id=?", (receipt,))
            shutil.rmtree(folder)
            if isinstance(exc, HTTPException):
                raise
            logger.warning("Source save admission failed (%s).", type(exc).__name__)
            raise HTTPException(503, "Saving could not start. Nothing has been confirmed saved; retry.") from None

    @app.get("/api/public/source-saves/{receipt}")
    def status(receipt: str, request: Request):
        return public(owned(receipt, session(request)))

    @app.post("/api/public/source-saves/{receipt}/retry", status_code=202)
    def retry(receipt: str, request: Request):
        owner = session(request)
        with lock:
            row = owned(receipt, owner)
            if row["state"] != "failed" or receipt in active:
                raise HTTPException(409, "This save is not awaiting retry.")
            if stopping.is_set():
                raise HTTPException(503, "Service is stopping. Retry shortly.")
            with connect() as db:
                if row["attempts"] >= 3:
                    raise HTTPException(429, "This save reached its retry limit. Check the file and try again later.")
                if db.execute("SELECT COUNT(*) FROM source_saves WHERE state IN ('receiving','queued','validating','saving')").fetchone()[0] >= 5:
                    raise HTTPException(429, "The source-save queue is full. Retry shortly.")
                if db.execute("SELECT COUNT(*) FROM source_save_admissions WHERE created>?", (time.time() - 86400,)).fetchone()[0] >= 20:
                    raise HTTPException(429, "The source-save request limit is reached. Retry later.")
                uid = 10000 + db.execute("INSERT INTO identities DEFAULT VALUES").lastrowid
                if uid >= 60000:
                    raise HTTPException(503, "Service identity capacity exhausted.")
                db.execute("UPDATE source_saves SET state='queued',reason='',uid=?,attempts=attempts+1 WHERE id=?", (uid, receipt))
                db.execute("INSERT INTO source_save_admissions VALUES(?)", (time.time(),))
            start(receipt)
        return public(owned(receipt, owner))

    @app.delete("/api/public/source-saves/{receipt}")
    def cancel(receipt: str, request: Request):
        with lock:
            row = owned(receipt, session(request))
            if row["state"] in {"saving", "saved", "already-present"}:
                raise HTTPException(409, "This paper is being committed or is already saved. Removing it from a comparison does not delete it from the shared library.")
            update(receipt, "cancelled", "Saving cancelled.")
            process = active.get(receipt)
            if process:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        return public(owned(receipt, row["owner"]))

    return cleanup
