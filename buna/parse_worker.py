"""Isolate untrusted document parsing from the HTTP server."""
import json
import resource
import sys
from pathlib import Path

from buna.documents import extract_document, PARSER_CPU_SECONDS, PARSER_MEMORY_BYTES


def main() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (PARSER_CPU_SECONDS, PARSER_CPU_SECONDS))
    # RLIMIT_AS is not reliably supported by macOS; input/page/text and wall-clock
    # limits remain enforced there.
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (PARSER_MEMORY_BYTES, PARSER_MEMORY_BYTES))
    try:
        result = extract_document(Path(sys.argv[1]), profile=sys.argv[2] if len(sys.argv) > 2 else "manuscript")
        print(json.dumps({"document": result}))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()
