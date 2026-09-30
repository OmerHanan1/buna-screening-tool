"""Parse one explicitly shared PDF inside the existing no-network worker boundary."""
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import sys

from buna.hosted_runtime import atomic_json
from buna.public_worker import isolate, ParseTimeout


def validate_source(folder: Path):
    from buna.documents import extract_document, ExtractionError
    from buna.shared_library import cache_document, SharedLibraryError
    source = folder / "source.pdf"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    canonical = folder / ("shared-validate-" + digest + ".pdf")
    source.rename(canonical)
    try:
        document = extract_document(canonical, profile="source")
        document = cache_document(document)
        atomic_json(folder / "parsed.json", document, ensure_ascii=False)
    except (ExtractionError, SharedLibraryError, ParseTimeout) as exc:
        reason = str(exc) if isinstance(exc, SharedLibraryError) else (
            "Source parsing timed out. This file was not saved." if isinstance(exc, ParseTimeout)
            else "This PDF could not be extracted within the supported text, page and size limits. Scanned or encrypted PDFs are not supported.")
        atomic_json(folder / "failure.json", {"code": "source-validation", "reason": reason})
        raise
    finally:
        canonical.rename(source)


def main():
    folder = Path(sys.argv[1])
    isolate(int(sys.argv[2]))
    resource.setrlimit(resource.RLIMIT_CPU, (40, 45))
    resource.setrlimit(resource.RLIMIT_FSIZE, (40 * 1024 * 1024,) * 2)
    os.chdir(folder)
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(ParseTimeout()))
    signal.setitimer(signal.ITIMER_REAL, 35)
    validate_source(Path.cwd())


if __name__ == "__main__":
    main()
