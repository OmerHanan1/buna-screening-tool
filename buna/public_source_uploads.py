"""Private, owner-scoped uploads attached to a single immutable comparison snapshot."""
import hashlib
from pathlib import Path
import shutil
import time
from uuid import UUID, uuid4

from fastapi import HTTPException, Request

from buna.hosted_runtime import job_storage_bytes

MAX_FILES = 50
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 128 * 1024 * 1024
RETENTION = 3600


def install_source_uploads(app, *, root, connect, session, lock):
    parent = root / "source-uploads"
    parent.mkdir(mode=0o700)
    with connect() as db:
        db.execute("""CREATE TABLE source_uploads(
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL,
            request_key TEXT NOT NULL, title TEXT NOT NULL, suffix TEXT NOT NULL,
            bytes INTEGER NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
            UNIQUE(owner, request_key))""")

    def cleanup():
        with lock, connect() as db:
            for row in db.execute("SELECT id FROM source_uploads WHERE created<? AND state='ready'",
                                  (time.time() - RETENTION,)).fetchall():
                (parent / row["id"]).unlink(missing_ok=True)
                db.execute("DELETE FROM source_uploads WHERE id=?", (row["id"],))

    def owned(db, reference, owner):
        row = db.execute("SELECT * FROM source_uploads WHERE id=? AND owner=? AND created>?",
                         (reference, owner, time.time() - RETENTION)).fetchone()
        if row is None:
            raise HTTPException(404, "A comparison upload expired or is unavailable. Remove it and upload the original again.")
        return row

    def public(row):
        return {key: row[key] for key in ("id", "title", "bytes", "digest", "state")}

    @app.post("/api/public/source-uploads", status_code=201)
    async def submit(request: Request):
        owner = session(request)
        key = request.headers.get("Idempotency-Key", "")
        try:
            UUID(key)
        except ValueError:
            raise HTTPException(422, "A valid upload request reference is required.") from None
        cleanup()
        reference = str(uuid4())
        with lock, connect() as db:
            prior = db.execute("SELECT * FROM source_uploads WHERE owner=? AND request_key=?", (owner, key)).fetchone()
            if prior:
                if prior["state"] != "ready":
                    raise HTTPException(409, "This upload is still arriving. Retry shortly.")
                return public(prior)
            count = db.execute("SELECT COUNT(*) FROM source_uploads WHERE owner=?", (owner,)).fetchone()[0]
            if count >= MAX_FILES:
                raise HTTPException(422, "At most 50 private comparison uploads per workspace. Remove an upload before adding another.")
            receiving = db.execute("SELECT COUNT(*) FROM source_uploads WHERE state='receiving'").fetchone()[0]
            if receiving >= 2 or job_storage_bytes(root) + (receiving + 1) * MAX_FILE_BYTES > 384 * 1024 * 1024:
                raise HTTPException(429, "Temporary upload capacity is busy. Retry shortly.", headers={"Retry-After": "10"})
            db.execute("INSERT INTO source_uploads VALUES(?,?,?,?,?,?,?,'','receiving')",
                       (reference, owner, time.time(), key, "", "", MAX_FILE_BYTES))
        try:
            async with request.form(max_files=1, max_fields=0, max_part_size=8192) as form:
                source = form.get("source")
                if not hasattr(source, "read"):
                    raise HTTPException(422, "Upload one comparison file.")
                suffix = Path(source.filename or "").suffix.lower()
                if suffix not in {".pdf", ".txt"}:
                    raise HTTPException(415, "Only PDF or plain text is supported.")
                if source.size is None or source.size > MAX_FILE_BYTES:
                    raise HTTPException(413, "Comparison files must be at most 8 MiB.")
                with lock, connect() as db:
                    used = db.execute("SELECT COALESCE(SUM(bytes),0) FROM source_uploads WHERE owner=? AND id<>?",
                                      (owner, reference)).fetchone()[0]
                    if used + source.size > MAX_BATCH_BYTES:
                        raise HTTPException(413, "Private comparison uploads exceed the 128 MiB workspace budget (including in-flight reservations). Remove some files.")
                    db.execute("UPDATE source_uploads SET bytes=? WHERE id=?", (source.size, reference))
                digest, size = hashlib.sha256(), 0
                with (parent / reference).open("xb") as output:
                    while chunk := await source.read(65536):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise HTTPException(413, "Comparison files must be at most 8 MiB.")
                        digest.update(chunk)
                        output.write(chunk)
                if not size:
                    raise HTTPException(422, "Empty comparison file.")
                if suffix == ".pdf":
                    with (parent / reference).open("rb") as source_stream:
                        if source_stream.read(5) != b"%PDF-":
                            raise HTTPException(422, "A PDF signature is required.")
                with lock, connect() as db:
                    db.execute("UPDATE source_uploads SET title=?,suffix=?,bytes=?,digest=?,state='ready' WHERE id=?",
                               (Path(source.filename).name[:255], suffix, size, digest.hexdigest(), reference))
                    return public(owned(db, reference, owner))
        except BaseException:
            with lock, connect() as db:
                (parent / reference).unlink(missing_ok=True)
                db.execute("DELETE FROM source_uploads WHERE id=?", (reference,))
            raise

    @app.delete("/api/public/source-uploads/{reference}")
    def remove(reference: str, request: Request):
        with lock, connect() as db:
            row = owned(db, reference, session(request))
            if row["state"] != "ready":
                raise HTTPException(409, "The upload is still arriving. Retry removal shortly.")
            (parent / reference).unlink()
            db.execute("DELETE FROM source_uploads WHERE id=?", (reference,))
        return {"state": "deleted"}

    @app.delete("/api/public/source-uploads")
    def clear(request: Request):
        owner = session(request)
        with lock, connect() as db:
            rows = db.execute("SELECT id,state FROM source_uploads WHERE owner=?", (owner,)).fetchall()
            if any(row["state"] != "ready" for row in rows):
                raise HTTPException(409, "Wait for current uploads to finish before clearing this workspace.")
            for row in rows:
                (parent / row["id"]).unlink(missing_ok=True)
            db.execute("DELETE FROM source_uploads WHERE owner=?", (owner,))
        return {"state": "deleted", "count": len(rows)}

    def snapshot(references, owner, folder):
        if (not isinstance(references, list) or len(references) > MAX_FILES
                or any(not isinstance(value, str) for value in references)
                or len(set(references)) != len(references)):
            raise HTTPException(422, "Select up to 50 distinct private upload references.")
        entries = []
        with lock, connect() as db:
            for index, reference in enumerate(references):
                row = owned(db, reference, owner)
                if row["state"] != "ready":
                    raise HTTPException(409, "Wait for all comparison uploads to finish.")
                name = f"staged-{index}{row['suffix']}"
                shutil.copyfile(parent / reference, folder / name)
                with (folder / name).open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != row["digest"]:
                    raise HTTPException(409, "A retained comparison upload changed. Upload the original again.")
                entries.append({"path": name, "title": row["title"], "sha256": digest})
        return entries

    return cleanup, snapshot
