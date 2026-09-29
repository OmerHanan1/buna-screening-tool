"""Preparse immutable approved sources once per private release, never visitor files."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

from buna.documents import STRUCTURE_VERSION


def build(source: Path, destination: Path):
    from buna.engine import parse_file
    import pypdf
    manifest = json.loads((source / "manifest.json").read_text())
    if destination.exists():
        raise ValueError("Use a new cache release directory; existing artifacts are immutable.")
    destination.mkdir(parents=True)
    for paper in manifest["papers"]:
        path = source / paper["filename"]
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != paper["sha256"]:
            raise ValueError("Source fingerprint mismatch.")
        document = parse_file(path, profile="source")
        output = json.dumps(document, ensure_ascii=False).encode()
        parsed_name = paper["sha256"] + ".json"
        (destination / parsed_name).write_bytes(output)
        shutil.copyfile(path, destination / paper["filename"])
        paper["parsed_cache"] = {"filename": parsed_name, "sha256": hashlib.sha256(output).hexdigest(),
                                 "source_sha256": paper["sha256"], "structure_version": STRUCTURE_VERSION,
                                 "profile": "source-v1", "parser": "pypdf", "parser_version": pypdf.__version__}
    raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    (destination / "manifest.json").write_bytes(raw)
    return {"papers": len(manifest["papers"]), "manifest_sha256": hashlib.sha256(raw).hexdigest()}


def validate_cache(root: Path, paper: dict):
    import pypdf
    cache = paper.get("parsed_cache")
    if cache is None:
        return
    if (cache.get("filename") != paper["sha256"] + ".json"
            or cache.get("source_sha256") != paper["sha256"]
            or cache.get("structure_version") != STRUCTURE_VERSION or cache.get("profile") != "source-v1"
            or cache.get("parser") != "pypdf" or cache.get("parser_version") != pypdf.__version__):
        raise ValueError("Invalid source extraction cache identity.")
    path = root / cache["filename"]
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Invalid source extraction cache file.")
    if hashlib.sha256(path.read_bytes()).hexdigest() != cache["sha256"]:
        raise ValueError("Source extraction cache fingerprint mismatch.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.destination)))
