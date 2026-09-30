import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from buna.shared_library import SharedLibrary, SharedLibraryError, LibraryChanged, SourceNotFound, RETIRE_SECONDS
from test_shared_library import MemoryBlobs, original, document


def test_curated_tombstone_persists_and_requires_new_explicit_save():
    store = MemoryBlobs()
    data = original()
    digest = hashlib.sha256(data).hexdigest()
    bundled = {"sha256": digest, "title": "Synthetic bundled source"}
    service = SharedLibrary(store, {digest})
    version = service.visible([bundled])[0]["library_version"]
    assert service.remove(digest, version)["storage_kind"] == "bundled"
    assert service.remove(digest, version)["already_removed"]
    assert SharedLibrary(store, {digest}).visible([bundled]) == []
    assert "catalog-before-removals-v1.json" in store.values
    with pytest.raises(LibraryChanged):
        service.save(data, document(), "old queued save", expected_version=version)
    service.save(data, document(), "explicit new save", expected_version=service.save_version(digest))
    restored = service.visible([bundled])
    assert len(restored) == 1 and restored[0]["title"] == bundled["title"]
    with pytest.raises(LibraryChanged):
        service.remove(digest, version)
    assert not any(path.startswith("papers/") for path in store.values)


def test_removed_shared_source_keeps_snapshot_then_gc_reclaims_only_old_objects(tmp_path, monkeypatch):
    store = MemoryBlobs()
    service = SharedLibrary(store, set())
    data, doc = original(), document()
    digest = service.save(data, doc, "First")["sha256"]
    paper = service.list()[0]
    snapshot = service.materialize(paper, tmp_path)
    captured = (tmp_path / ("shared-" + digest + ".json")).read_bytes()
    service.remove(digest, paper["library_version"])
    assert service.list() == []
    assert service.remove(digest, paper["library_version"])["already_removed"]
    with pytest.raises(LibraryChanged):
        service.assert_selected_current({digest: paper["library_version"]})
    assert (tmp_path / ("shared-" + digest + ".json")).read_bytes() == captured
    assert snapshot["cached_sha256"] == hashlib.sha256(captured).hexdigest()
    # New explicit upload uses a new object generation; old cleanup cannot delete it.
    service.save(data, doc, "Second", expected_version=service.save_version(digest))
    fresh = service.list()[0]
    assert fresh["object_version"] != paper["object_version"]
    old_paths = service._object_paths(paper)
    fresh_paths = service._object_paths(fresh)
    assert set(old_paths).isdisjoint(fresh_paths)
    before, _ = service._read()
    assert len(before["retired"]) == 1
    after_time = time.time() + RETIRE_SECONDS + 1
    monkeypatch.setattr("buna.shared_library.time.time", lambda: after_time)
    service.collect_retired()
    service.collect_retired()
    after, _ = service._read()
    assert after["retired"] == []
    assert all(path not in store.values for path in old_paths)
    assert all(path in store.values for path in fresh_paths)
    assert len(after["reservations"]) == 2  # Removing does not refund daily admission.
    assert service.list()[0]["title"] == "Second"


def test_late_duplicate_writer_cannot_publish_after_removal(monkeypatch):
    store = MemoryBlobs()
    service = SharedLibrary(store, set())
    data, doc = original(), document()
    entered, release = threading.Event(), threading.Event()
    writer = store.immutable
    def paused(name, content):
        if threading.current_thread().name.startswith("delayed") and name.endswith("original.pdf"):
            entered.set()
            assert release.wait(5)
        writer(name, content)
    store.immutable = paused
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="delayed") as pool:
        slow = pool.submit(service.save, data, doc, "Delayed", expected_version="0")
        assert entered.wait(5)
        service.save(data, doc, "First")
        paper = service.list()[0]
        service.remove(paper["sha256"], "0")
        release.set()
        with pytest.raises(LibraryChanged):
            slow.result()
    catalog, _ = service._read()
    assert service.list() == [] and len(catalog["retired"]) == 1
    later = time.time() + RETIRE_SECONDS + 1
    monkeypatch.setattr("buna.shared_library.time.time", lambda: later)
    service.collect_retired()
    assert not any(path.startswith("papers/") for path in store.values)


def test_concurrent_removals_preserve_all_tombstones_and_quota():
    store = MemoryBlobs()
    service = SharedLibrary(store, set())
    for index in range(4):
        service.save(original(index), document(), str(index))
    papers = service.list()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda p: service.remove(p["sha256"], p["library_version"]), papers))
    catalog, _ = service._read()
    assert len(catalog["removals"]) == len(catalog["retired"]) == 4
    assert len(catalog["reservations"]) == 4 and not catalog["papers"]
    # Repeated requests cannot double-charge retired storage.
    for paper in papers:
        assert service.remove(paper["sha256"], "0")["already_removed"]
    assert len(service._read()[0]["retired"]) == 4


def test_unknown_or_pathlike_identifiers_cannot_remove_blobs():
    store = MemoryBlobs()
    service = SharedLibrary(store, set())
    for digest in ["f" * 64, "../catalog-v1.json", "papers/" + "f" * 64]:
        with pytest.raises(SourceNotFound):
            service.remove(digest, "0")
    assert not store.values


def test_failed_blob_delete_keeps_retired_quota_for_retry(monkeypatch):
    store = MemoryBlobs()
    service = SharedLibrary(store, set())
    digest = service.save(original(), document(), "Synthetic")["sha256"]
    service.remove(digest, "0")
    later = time.time() + RETIRE_SECONDS + 1
    monkeypatch.setattr("buna.shared_library.time.time", lambda: later)
    delete = store.delete
    store.delete = lambda _: (_ for _ in ()).throw(OSError("Synthetic storage outage"))
    with pytest.raises(OSError):
        service.collect_retired()
    assert service._read()[0]["retired"]
    store.delete = delete
    service.collect_retired()
    assert service._read()[0]["retired"] == []


def test_retired_bytes_are_charged_until_confirmed_cleanup(monkeypatch):
    service = SharedLibrary(MemoryBlobs(), set())
    digest = service.save(original(), document(), "Synthetic")["sha256"]
    paper = service.list()[0]
    monkeypatch.setattr("buna.shared_library.MAX_TOTAL_BYTES", paper["original_bytes"] + paper["parsed_bytes"])
    service.remove(digest, "0")
    with pytest.raises(SharedLibraryError, match="byte quota"):
        service.save(original(), document(), "New explicit upload", expected_version=service.save_version(digest))
    later = time.time() + RETIRE_SECONDS + 1
    monkeypatch.setattr("buna.shared_library.time.time", lambda: later)
    service.collect_retired()
    assert service.save(original(), document(), "New explicit upload")["state"] == "saved"


def test_cloud_verifier_never_mutates_production_catalog(monkeypatch):
    from types import SimpleNamespace
    from deploy import verify_library_removal
    store = MemoryBlobs()
    store.write("catalog-v1.json", b"untouched synthetic production marker", None)
    store.client = SimpleNamespace(list_blobs=lambda name_starts_with: [
        SimpleNamespace(name=name) for name in store.values if name.startswith(name_starts_with)])
    monkeypatch.setattr(verify_library_removal, "AzureBlobStore", lambda *_: store)
    for key in ("BUNA_SHARED_ACCOUNT_URL", "BUNA_SHARED_CONTAINER", "BUNA_SHARED_IDENTITY_CLIENT_ID"):
        monkeypatch.setenv(key, "synthetic")
    proof = verify_library_removal.verify()
    assert proof["actual_msi_old_blobs_deleted"]
    restarted = verify_library_removal.verify(proof["namespace"], cleanup=True)
    assert restarted["restart_bundled_still_removed"]
    assert restarted["only_validation_namespace_cleaned"]
    assert list(store.values) == ["catalog-v1.json"]
    assert store.values["catalog-v1.json"][0] == b"untouched synthetic production marker"
