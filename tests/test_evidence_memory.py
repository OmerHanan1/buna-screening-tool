import pytest

from buna.documents import _structure
from buna.improved_eng import _Evidence, _collect, _path_bytes, improved_report, SearchLimit


def test_replacements_release_disposed_paths_and_duplicates_do_not_charge():
    budget = _Evidence(10_000_000)
    paths = []
    for length in range(9, 350):
        path = tuple((i, i) for i in range(length))
        _collect(paths, path, budget, lambda: None)
        assert budget.bytes == _path_bytes(path)
        _collect(paths, path, budget, lambda: None)
        assert budget.bytes == _path_bytes(path)
    assert len(paths) == 1


def test_failed_replacement_preserves_previous_evidence_and_reservation():
    old = tuple((i, i) for i in range(9))
    new = tuple((i, i) for i in range(10))
    budget = _Evidence(_path_bytes(old) + _path_bytes(new) - 1)
    paths = []
    _collect(paths, old, budget, lambda: None)
    with pytest.raises(SearchLimit):
        _collect(paths, new, budget, lambda: None)
    assert paths == [old] and budget.bytes == _path_bytes(old)


def test_contained_exact_subrun_keeps_its_reservation():
    exact = tuple((i, i) for i in range(9))
    similar = exact + ((10, 11),)
    budget, paths = _Evidence(), []
    _collect(paths, exact, budget, lambda: None)
    _collect(paths, similar, budget, lambda: None)
    assert paths == [exact, similar]
    assert budget.bytes == _path_bytes(exact) + _path_bytes(similar)


def test_release_is_scoped_to_each_budget_and_never_negative():
    a, b = _Evidence(), _Evidence()
    path = tuple((i, i) for i in range(9))
    for budget in (a, b):
        _collect([], path, budget, lambda: None)
    a.release(_path_bytes(path))
    assert a.bytes == 0 and b.bytes == _path_bytes(path)
    with pytest.raises(RuntimeError):
        a.release(1)


def document(text):
    return _structure([{"number": 1, "text": text}], "Synthetic", [])


def test_audit_exhaustion_cannot_starve_later_scored_sources(monkeypatch):
    import buna.improved_eng as module
    monkeypatch.setattr(module, "MAX_EVIDENCE_BYTES", 2 * 1024 * 1024)
    phrase = "amber birds gather beside quiet rivers during winter mornings"
    bibliography = " ".join(f"bibliographyword{i}" for i in range(250))
    target = document("Abstract\n" + phrase + ". uniquetarget.\n\nReferences\n" + bibliography)
    sources = [{"id": str(i), "source_number": i + 1, "document": document("Introduction\n" + phrase + f". independentsource{i}.\n\nReferences\n" + bibliography)}
               for i in range(61)]
    result = improved_report(target, sources, total_time_limit_seconds=60)
    assert all(row["status"] == "compared" and row["scored_search_complete"] for row in result["source_coverage"])
    assert result["metrics"]["overlapping_words"] == 9
    assert result["metrics"]["all_sources_fully_checked"] is True
    assert result["improved_eng"]["audit_complete"] is False
    memory = result["improved_eng"]["evidence_memory"]
    assert memory["excluded_audit"]["exhausted"]
    assert memory["scored"]["limit_bytes"] + memory["excluded_audit"]["limit_bytes"] == 2 * 1024 * 1024
    assert memory["scored"]["retained_bytes"] + memory["excluded_audit"]["retained_bytes"] <= memory["combined_limit_bytes"]
    assert all(row["overlapping_words"] == 9 for row in result["source_coverage"])
    # No diagnostic output changes source-order-independent scored evidence.
    reversed_result = improved_report(target, list(reversed(sources)), total_time_limit_seconds=60)
    assert reversed_result["metrics"] == result["metrics"]
    assert reversed_result["matches"] == result["matches"]


def test_genuine_scored_budget_exhaustion_stays_partial(monkeypatch):
    import buna.improved_eng as module
    monkeypatch.setattr(module, "MAX_EVIDENCE_BYTES", 16_000)
    phrase = "amber birds gather beside quiet rivers during winter mornings"
    result = improved_report(document("Abstract\n" + phrase), [{"id": "s", "document": document("Source\n" + phrase + " ending")}])
    assert result["metrics"]["truncated"]
    assert result["source_coverage"][0]["status"] == "compared-with-limits"
    assert not result["source_coverage"][0]["scored_search_complete"]
    memory = result["improved_eng"]["evidence_memory"]
    assert memory["scored"]["retained_bytes"] == 0
