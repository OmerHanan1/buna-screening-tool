import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from buna.documents import _structure
from buna.shared_library import SharedLibrary, SharedLibraryError, Conflict, CATALOG


class MemoryBlobs:
    def __init__(self):
        self.values = {}
        self.version = 0
        self.lock = threading.Lock()
        self.fail_objects = False

    def read(self, name, limit):
        with self.lock:
            value = self.values.get(name)
            if value is None:
                return None
            if len(value[0]) > limit:
                raise SharedLibraryError("Read size exceeded")
            return value

    def write(self, name, data, expected):
        with self.lock:
            current = self.values.get(name)
            if (expected is None and current is not None) or (expected is not None and (not current or current[1] != expected)):
                raise Conflict()
            self.version += 1
            self.values[name] = (bytes(data), str(self.version))
            return str(self.version)

    def immutable(self, name, data):
        if self.fail_objects:
            raise OSError("Synthetic storage outage")
        try:
            self.write(name, data, None)
        except Conflict:
            assert self.read(name, len(data))[0] == data


def document(text="Introduction\nOriginal shared scientific observations are recorded in this synthetic full body."):
    return _structure([{"number": 1, "text": text}], "Synthetic", [])


def original(number=1):
    return f"%PDF-synthetic-source-{number}".encode()


def test_persist_restart_dedupe_and_curated_immutability(tmp_path):
    blobs = MemoryBlobs()
    first = SharedLibrary(blobs, set())
    result = first.save(original(), document(), "First title.pdf")
    assert result["state"] == "saved"
    second = SharedLibrary(blobs, set())
    assert len(second.list()) == 1
    assert second.save(original(), document(), "Changed title.pdf")["state"] == "already-present"
    assert second.list()[0]["title"] == "First title.pdf"
    materialized = second.materialize(second.list()[0], tmp_path)
    assert json.loads(open(materialized["cached_document"]).read())["text"] == document()["text"]
    before = copy.deepcopy(blobs.values)
    curated = SharedLibrary(blobs, {hashlib.sha256(original(2)).hexdigest()})
    assert curated.save(original(2), {}, "Do not replace")["state"] == "already-present"
    assert blobs.values == before


def test_concurrent_same_file_is_idempotent_and_unique_files_not_lost():
    blobs = MemoryBlobs()
    services = [SharedLibrary(blobs, set()) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda service: service.save(original(), document(), "same.pdf"), services))
    assert all(r["state"] in {"saved", "already-present"} for r in results)
    assert len(services[0].list()) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: services[i].save(original(i + 2), document(), f"source{i}.pdf"), range(4)))
    assert len(services[0].list()) == 5


def test_failed_upload_stays_nonready_and_reserved_until_identical_retry():
    blobs = MemoryBlobs()
    service = SharedLibrary(blobs, set())
    blobs.fail_objects = True
    with pytest.raises(OSError):
        service.save(original(), document(), "pending.pdf")
    assert service.list() == []
    catalog = json.loads(blobs.read(CATALOG, 512 * 1024)[0])
    assert len(catalog["papers"]) == 1
    assert next(iter(catalog["papers"].values()))["state"] == "pending"
    blobs.fail_objects = False
    assert SharedLibrary(blobs, set()).save(original(), document(), "retry.pdf")["state"] == "saved"
    assert len(service.list()) == 1


def test_abstract_only_bad_offsets_and_nonpdf_never_ready():
    service = SharedLibrary(MemoryBlobs(), set())
    with pytest.raises(SharedLibraryError, match="abstract"):
        service.save(original(), document("Abstract\nOnly a summary of an unavailable full article is included here."), "abstract.pdf")
    bad = document()
    bad["segments"][0]["start"] = -1
    with pytest.raises(SharedLibraryError, match="offset"):
        service.save(original(), bad, "bad.pdf")
    with pytest.raises(SharedLibraryError, match="PDF"):
        service.save(b"<html>not pdf</html>", document(), "web.pdf")
    assert service.list() == []


def test_count_byte_daily_limits_and_immutable_cache_corruption(tmp_path, monkeypatch):
    import buna.shared_library as module
    blobs = MemoryBlobs()
    service = SharedLibrary(blobs, set())
    service.save(original(), document(), "one.pdf")
    monkeypatch.setattr(module, "MAX_PAPERS", 1)
    with pytest.raises(SharedLibraryError, match="paper-count"):
        service.save(original(2), document(), "two.pdf")
    monkeypatch.setattr(module, "MAX_PAPERS", 50)
    monkeypatch.setattr(module, "MAX_NEW_PER_DAY", 1)
    with pytest.raises(SharedLibraryError, match="daily"):
        service.save(original(2), document(), "two.pdf")
    monkeypatch.setattr(module, "MAX_NEW_PER_DAY", 20)
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", sum(len(v[0]) for k, v in blobs.values.items() if k != CATALOG))
    with pytest.raises(SharedLibraryError, match="byte quota"):
        service.save(original(2), document(), "two.pdf")
    paper = service.list()[0]
    key = f"papers/{paper['sha256']}/{paper['parsed_sha256']}.json"
    blobs.values[key] = (b"corrupt", "bad")
    with pytest.raises(SharedLibraryError, match="damaged"):
        service.materialize(paper, tmp_path)


def test_shared_cache_preserves_matching_and_original_offsets(tmp_path):
    from buna.comparison import compare_documents
    phrase = "amber birds gather beside quiet rivers during winter mornings"
    source = document("Introduction\nSource observations show " + phrase + ".\nReferences\n1. A synthetic source.")
    target = document("Abstract\nIndependent synthetic observations describe " + phrase + ".")
    blobs = MemoryBlobs()
    service = SharedLibrary(blobs, set())
    service.save(original(), source, "source.pdf")
    materialized = service.materialize(service.list()[0], tmp_path)
    restored = json.loads(open(materialized["cached_document"]).read())
    assert restored["text"] == source["text"] and restored["pages"] == source["pages"]
    direct = compare_documents(target, [{"id": "s", "document": source}])
    cached = compare_documents(target, [{"id": "s", "document": restored}])
    assert direct["metrics"] == cached["metrics"]
    assert direct["matches"] == cached["matches"]
