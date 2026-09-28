"""Isolated native-PDF rendering process; no network or comparison operations."""
import json
import resource
import sys
from pathlib import Path

from buna.pdf_reports import generate_pdf


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_CPU, (100, 100))
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    try:
        output = Path(sys.argv[2])
        result = generate_pdf(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")), output)
        output.with_suffix(".json").write_text(json.dumps(result), encoding="utf-8")
    except Exception as exc:
        print(f"PDF generation failed: {type(exc).__name__}: {exc}")
        sys.exit(1)
