"""Original synthetic removal checks confined to a unique private Blob prefix."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4

import pymupdf

from buna.documents import extract_document
from buna.shared_library import AzureBlobStore, SharedLibrary, LibraryChanged


class PrefixStore:
    def __init__(self, store, prefix):
        if not re.fullmatch(r"validation/removal-[0-9a-f]{32}/", prefix):
            raise ValueError("Only a unique synthetic validation namespace is permitted.")
        self.store, self.prefix = store, prefix

    def read(self, name, limit):
        return self.store.read(self.prefix + name, limit)

    def write(self, name, data, expected):
        return self.store.write(self.prefix + name, data, expected)

    def immutable(self, name, data):
        return self.store.immutable(self.prefix + name, data)

    def delete(self, name):
        return self.store.delete(self.prefix + name)


def verify(prefix=None, cleanup=False):
    store = AzureBlobStore(*[os.environ[key] for key in (
        "BUNA_SHARED_ACCOUNT_URL", "BUNA_SHARED_CONTAINER", "BUNA_SHARED_IDENTITY_CLIENT_ID")])
    namespace = prefix or "validation/removal-" + uuid4().hex + "/"
    isolated = PrefixStore(store, namespace)
    def fixture(text):
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_textbox(pymupdf.Rect(40, 40, 550, 750), text, fontsize=10)
            return document.tobytes(no_new_id=True)
    bundled_bytes = fixture("Introduction\nOriginal synthetic bundled-removal fixture. It is not an actual paper.")
    digest = hashlib.sha256(bundled_bytes).hexdigest()
    curated = [{"sha256": digest, "title": "Synthetic bundled removal fixture"}]
    library = SharedLibrary(isolated, {digest})
    if prefix:
        assert not any(p["sha256"] == digest for p in library.visible(curated))
        assert len(library.list()) == 1
        result = {"namespace": namespace, "restart_bundled_still_removed": True,
                  "restart_explicit_shared_reupload_present": True}
    else:
        assert len(library.visible(curated)) == 1
        library.remove(digest, "0")
        assert library.visible(curated) == []
        assert library.remove(digest, "0")["already_removed"]
        source = fixture("Introduction\nOriginal synthetic source text validates exact private storage operations.")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.pdf"
            path.write_bytes(source)
            doc = extract_document(path, profile="source")
        shared_digest = library.save(source, doc, "Synthetic shared fixture")["sha256"]
        paper = library.list()[0]
        with tempfile.TemporaryDirectory() as folder:
            snapshot = library.materialize(paper, Path(folder))
            captured = Path(snapshot["cached_document"]).read_bytes()
            library.remove(shared_digest, "0")
            assert Path(snapshot["cached_document"]).read_bytes() == captured
        assert library.list() == []
        assert library.remove(shared_digest, "0")["already_removed"]
        try:
            library.save(source, doc, "Old queued fixture", expected_version="0")
        except LibraryChanged:
            pass
        else:
            raise AssertionError("An old save restored a removed generation.")
        library.save(source, doc, "Explicit synthetic reupload", expected_version=library.save_version(shared_digest))
        fresh = library.list()[0]
        assert fresh["object_version"] != paper["object_version"]
        catalog, etag = library._read()
        # Only this disposable namespace's retention clock is advanced.
        for entry in catalog["retired"]:
            entry["delete_after"] = 0
        library._cas(catalog, etag)
        library.collect_retired()
        assert not library._read()[0]["retired"]
        assert all(isolated.read(name, 32 * 1024 * 1024) is None for name in library._object_paths(paper))
        assert all(isolated.read(name, 32 * 1024 * 1024) is not None for name in library._object_paths(fresh))
        result = {"namespace": namespace, "synthetic_bundled_hidden": True, "shared_hidden": True,
                  "repeat_remove_idempotent": True, "old_save_cannot_resurrect": True,
                  "explicit_reupload_new_generation": True, "snapshot_unchanged": True,
                  "actual_msi_old_blobs_deleted": True, "new_blobs_preserved": True,
                  "retired_quota_reclaimed": True, "production_catalog_untouched": True}
    if cleanup:
        names = [item.name for item in store.client.list_blobs(name_starts_with=namespace)]
        assert all(name.startswith(namespace) for name in names)
        for name in names:
            store.delete(name)
        result["only_validation_namespace_cleaned"] = True
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.namespace, args.cleanup)))
