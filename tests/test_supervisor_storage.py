from pathlib import Path
import threading

import pytest

from buna.hosted_runtime import atomic_json, job_storage_bytes


def test_production_scan_race_reproduces_and_atomic_safe_scan_survives(tmp_path, monkeypatch):
    (tmp_path / "target.txt").write_text("Synthetic target")
    temporary = tmp_path / "progress.new"
    temporary.write_text('{"stage":"compare"}')
    snapshot = list(tmp_path.rglob("*"))
    temporary.replace(tmp_path / "progress.json")
    original_glob = Path.rglob
    with monkeypatch.context() as patch:
        patch.setattr(Path, "rglob", lambda self, pattern: iter(snapshot) if self == tmp_path else original_glob(self, pattern))
        # Exact production expression before the fix: the first check returns
        # false for a vanished entry, then the second stat raises.
        with pytest.raises(FileNotFoundError):
            sum(p.lstat().st_size for p in tmp_path.rglob("*") if not p.is_symlink())
        assert job_storage_bytes(tmp_path) == (tmp_path / "target.txt").stat().st_size
    assert job_storage_bytes(tmp_path) == sum(p.stat().st_size for p in tmp_path.iterdir())


def test_live_atomic_progress_replacements_never_fail_storage_monitor(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("Original synthetic target")
    failures = []
    def writer():
        try:
            for i in range(1500):
                atomic_json(tmp_path / "progress.json", {"stage": "compare", "source_index": i % 44})
        except Exception as exc:
            failures.append(exc)
    thread = threading.Thread(target=writer)
    thread.start()
    samples = 0
    while thread.is_alive():
        assert job_storage_bytes(tmp_path) >= target.stat().st_size
        samples += 1
    thread.join()
    assert samples > 0 and not failures


def test_storage_monitor_does_not_follow_symlinks_or_hide_real_errors(tmp_path, monkeypatch):
    root = tmp_path / "job"
    root.mkdir()
    (tmp_path / "outside.txt").write_bytes(b"x" * 10000)
    (root / "link").symlink_to(tmp_path / "outside.txt")
    assert job_storage_bytes(root) == 0
    protected = root / "protected"
    protected.write_text("test")
    original_stat = Path.lstat
    def denied(path):
        if path == protected:
            raise PermissionError("Synthetic permission failure")
        return original_stat(path)
    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(PermissionError):
        job_storage_bytes(root)
