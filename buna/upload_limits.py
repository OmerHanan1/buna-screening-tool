"""Shared per-file limits and bounded hosted multipart parsing."""
from contextlib import asynccontextmanager

from fastapi import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser

MAX_FILE_BYTES = 40 * 1024 * 1024
REQUEST_BYTES = MAX_FILE_BYTES + 1024 * 1024
CHUNK_BYTES = 64 * 1024


class FileLimitedParser(MultiPartParser):
    def on_part_begin(self):
        super().on_part_begin()
        self.file_bytes = 0

    def on_part_data(self, data, start, end):
        if self._current_part.file is not None:
            self.file_bytes += end - start
            if self.file_bytes > MAX_FILE_BYTES:
                raise HTTPException(413, "File exceeds the 40 MiB limit.")
        super().on_part_data(data, start, end)

    async def parse(self):
        try:
            return await super().parse()
        except BaseException:
            # Older supported Starlette releases only close on multipart errors,
            # not disconnects, cancellation or the actual-byte HTTP body guard.
            for file in self._files_to_close_on_error:
                file.close()
            raise


@asynccontextmanager
async def upload_form(request, *, max_files, max_fields, max_part_size):
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise HTTPException(400, "A multipart upload form is required.")

    async def chunks():
        async for chunk in request.stream():
            for offset in range(0, len(chunk), CHUNK_BYTES):
                yield chunk[offset:offset + CHUNK_BYTES]

    parser = FileLimitedParser(request.headers, chunks(), max_files=max_files,
                               max_fields=max_fields, max_part_size=max_part_size)
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        raise HTTPException(400, exc.message) from exc
    try:
        yield form
    finally:
        await form.close()
