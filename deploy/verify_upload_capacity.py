"""Generate original image-bearing boundary PDFs; optionally check extraction locally.

No cloud access, production corpus, worker-isolation bypass or storage mutation.
Native gateway/Blob persistence checks remain a separate release gate.
"""
import argparse
import json
import os
from pathlib import Path
import resource
import shutil
import tempfile
import time

import pymupdf


def image_pdf(path: Path, size: int, *, variant: str = ""):
    """An original RGB sensor image plus eight pages of extractable study text."""
    width = 4096
    height = (size - 16_384) // (width * 3)
    with pymupdf.open() as document:
        for number in range(1, 9):
            page = document.new_page()
            page.insert_text((45, 45), f"Introduction - synthetic sensor study page {number}")
            page.insert_text((45, 65), "Amber sensors record stable laboratory measurements during controlled cycles.")
            page.insert_text((45, 85), "Discussion - original experimental observations support the reported results.")
            if variant:
                page.insert_text((45, 105), variant, fontsize=9)
            if number == 1:
                image = pymupdf.Pixmap(pymupdf.csRGB, width, height, os.urandom(width * height * 3), False)
                page.insert_image(pymupdf.Rect(45, 120, 550, 650), pixmap=image)
                del image
        document.save(path)
    padding = size - path.stat().st_size
    assert 0 < padding < 32_768
    # Keep the real xref offsets and EOF intact. Under 32 KiB is a PDF comment;
    # virtually all fixture bytes are the referenced, renderable RGB image.
    with path.open("r+b") as stream:
        stream.seek(-256, 2)
        offset = stream.tell()
        tail = stream.read()
        start = tail.index(b"startxref")
        stream.seek(offset + start)
        stream.write(b"%" + b" " * (padding - 2) + b"\n" + tail[start:])
    assert path.stat().st_size == size


def verify(root):
    from buna.documents import extract_document, ExtractionError
    from buna.public_source_worker import validate_source
    started = time.monotonic()
    for mib in (36, 40):
        path = root / f"synthetic-{mib}.pdf"
        image_pdf(path, mib * 1024 * 1024)
        for profile in ("manuscript", "source"):
            document = extract_document(path, profile=profile)
            assert document["extraction"]["pages_extracted"] == 8
            assert not document["extraction"]["truncated"]
            for number, page in enumerate(document["pages"], 1):
                assert f"synthetic sensor study page {number}" in page["text"]
        work = root / f"save-{mib}"
        work.mkdir()
        shutil.copyfile(path, work / "source.pdf")
        validate_source(work)
        assert json.loads((work / "parsed.json").read_text())["extraction"]["pages_extracted"] == 8
        shutil.rmtree(work)
    over = root / "synthetic-40-plus-one.pdf"
    shutil.copyfile(root / "synthetic-40.pdf", over)
    with over.open("ab") as stream:
        stream.write(b"\n")
    for profile in ("manuscript", "source"):
        try:
            extract_document(over, profile=profile)
        except ExtractionError as exc:
            assert "40 MiB" in str(exc)
        else:
            raise AssertionError("One byte over was accepted.")
    return {"accepted_mib": [36, 40], "rejected_bytes": over.stat().st_size,
            "pages": 8, "profiles": ["manuscript", "source", "shared-source-validation"],
            "seconds": round(time.monotonic() - started, 2),
            "max_rss_platform_units": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "scope": "extraction only; no native gateway isolation or cloud persistence claim"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Retain only newly generated synthetic fixtures here.")
    args = parser.parse_args()
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=False)
        result = verify(args.output_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="capacity-synthetic-") as folder:
            result = verify(Path(folder))
    print(json.dumps(result, indent=2))
