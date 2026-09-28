"""Owner-operated import: stop the app and back up its data directory first."""
import argparse
import fcntl
import json
import os
import tempfile
from contextlib import ExitStack
from pathlib import Path

from buna.collections import Collections
from buna.engine import ACTIVE, Engine
from buna.library import Library
from buna.storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--set-default", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate in a temporary library, without opening the production data directory.")
    args = parser.parse_args()
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="buna-collection-dry-run-"))) if args.dry_run else args.data_dir
        store = Store(root)
        lease = stack.enter_context((store.root / "library.lock").open("a"))
        os.chmod(lease.name, 0o600)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            parser.error("Stop the library server before importing; the data directory is in use.")
        if any(job["status"] in ACTIVE for job in store.list()):
            parser.error("An active comparison is recorded. Resolve it before importing.")
        engine = Engine(store, {})
        try:
            collections = Collections(Library(store, engine, {}))
            result = collections.import_manifest(json.loads(args.manifest.read_text()), args.staging_dir)
            if args.set_default:
                collections.set_default(result["id"])
            item = next(c for c in collections.list()["collections"] if c["id"] == result["id"])
            print(json.dumps({"dry_run": args.dry_run, **{key: item[key] for key in (
                "id", "name", "ready_unique", "ready_references", "total_references", "import_batch",
            )}}))
        finally:
            engine.close()


if __name__ == "__main__":
    main()
