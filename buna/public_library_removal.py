"""Explicitly confirmed global library removal; never a file-path deletion API."""
from collections import defaultdict, deque
import logging
import re
import threading
import time

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from buna.shared_library import LibraryChanged, SourceNotFound, SharedLibraryError, RETIRE_SECONDS

logger = logging.getLogger(__name__)


class RemovalInput(BaseModel):
    confirm_sha256: str = Field(min_length=64, max_length=64)
    expected_version: str = Field(min_length=1, max_length=32)
    affects_everyone: bool


def install_library_removal(app, *, shared, session):
    attempts = defaultdict(deque)
    lock = threading.Lock()

    @app.delete("/api/public/library/{digest}")
    def remove(digest: str, body: RemovalInput, request: Request):
        owner = session(request)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HTTPException(404, "Paper unavailable.")
        if body.confirm_sha256 != digest or body.affects_everyone is not True:
            raise HTTPException(422, "Confirm this specific paper's removal for all app users.")
        if not re.fullmatch(r"0|[0-9a-f]{32}", body.expected_version):
            raise HTTPException(422, "Refresh the library before removing this paper.")
        if shared is None:
            raise HTTPException(503, "Persistent library removal is unavailable.")
        with lock:
            now = time.monotonic()
            for key in list(attempts):
                while attempts[key] and attempts[key][0] <= now - 60:
                    attempts[key].popleft()
                if not attempts[key]:
                    del attempts[key]
            if len(attempts[owner]) >= 10 or sum(map(len, attempts.values())) >= 30:
                raise HTTPException(429, "Removal rate limit reached. Wait a minute before retrying.")
            attempts[owner].append(now)
        try:
            result = shared.remove(digest, body.expected_version)
        except SourceNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except LibraryChanged as exc:
            raise HTTPException(409, str(exc)) from None
        except SharedLibraryError as exc:
            raise HTTPException(503, str(exc)) from None
        except Exception as exc:
            logger.warning("Library removal not confirmed (%s).", type(exc).__name__)
            raise HTTPException(503, "Removal could not be confirmed. Refresh the library and retry.") from None
        return {**result, "sha256": digest, "scope": "all-users",
                "private_cleanup_after_seconds": RETIRE_SECONDS if result["storage_kind"] == "shared" else None,
                "packaged_bytes_retained": result["storage_kind"] == "bundled"}
