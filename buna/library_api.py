"""Additive, localhost-only library routes, registered only when explicitly enabled."""
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from buna.engine import MAX_SOURCE_UPLOAD, ParseFailure, public_job
from buna.library import LibraryError, visible
from buna.library_providers import DISCLOSURE, DISCLOSURE_VERSION, preview_dois


class Preview(BaseModel):
    text: str = Field(max_length=80_000)


class Import(Preview):
    request_key: UUID
    disclosure_version: int


class Retry(BaseModel):
    disclosure_version: int


class Attach(BaseModel):
    paper_ids: list[UUID] = Field(min_length=1, max_length=200)


class DefaultCollection(BaseModel):
    collection_id: UUID | None = None


def library_router(library):
    router = APIRouter(prefix="/api/library")
    from buna.collections import Collections
    collections = Collections(library)

    def call(action, *args):
        try:
            return action(*args)
        except LibraryError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        except (ValueError, ParseFailure) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/config")
    def config():
        return {"enabled": True, "disclosure": DISCLOSURE, "disclosure_version": DISCLOSURE_VERSION,
                "unpaywall_enabled": bool(library.resolver.config.get("unpaywall_email")),
                "scope": "Local single-user; one server process per data directory."}

    @router.get("/collections")
    def collection_list():
        return collections.list()

    @router.put("/collections/default")
    def collection_default(body: DefaultCollection):
        return call(collections.set_default, str(body.collection_id) if body.collection_id else None)

    @router.post("/collections/{collection_id}/comparisons/{job_id}/sources")
    def collection_attach(collection_id: UUID, job_id: UUID, body: Attach):
        result = call(collections.attach, str(collection_id), str(job_id), [str(value) for value in body.paper_ids])
        return {**result, "job": public_job(result["job"])}

    @router.post("/preview")
    def preview(body: Preview):
        return call(preview_dois, body.text)

    @router.post("/imports", status_code=202)
    def import_dois(body: Import):
        return call(library.import_dois, body.text, str(body.request_key), body.disclosure_version)

    @router.get("/imports/{batch_id}")
    def batch(batch_id: UUID):
        return call(library.batch, str(batch_id))

    @router.get("/papers")
    def papers(q: str = Query("", max_length=300), offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
        return library.list(q, offset, limit)

    @router.get("/papers/{paper_id}")
    def paper(paper_id: UUID):
        return visible(call(library.get, str(paper_id)))

    @router.post("/papers/{paper_id}/cancel")
    def cancel(paper_id: UUID):
        return visible(call(library.cancel, str(paper_id)))

    @router.post("/papers/{paper_id}/retry", status_code=202)
    def retry(paper_id: UUID, body: Retry):
        return visible(call(library.retry, str(paper_id), body.disclosure_version))

    @router.post("/uploads", status_code=201)
    def upload(file: UploadFile = File(...), doi: str = Form("", max_length=300)):
        try:
            content = file.file.read(MAX_SOURCE_UPLOAD + 1)
            if len(content) > MAX_SOURCE_UPLOAD:
                raise HTTPException(413, f"File exceeds the {MAX_SOURCE_UPLOAD // (1024 * 1024)} MiB source limit.")
            return visible(call(library.manual, content, file.filename or "paper", doi or None))
        finally:
            file.file.close()

    @router.post("/comparisons/{job_id}/sources")
    def attach(job_id: UUID, body: Attach):
        return public_job(call(library.attach, str(job_id), [str(value) for value in body.paper_ids]))

    @router.post("/comparisons/{job_id}/sources/{source_id}/save", status_code=201)
    def save(job_id: UUID, source_id: UUID):
        return visible(call(library.save_job_source, str(job_id), str(source_id)))

    return router
