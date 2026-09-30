"""One public comparison in an OS-isolated, disposable job directory."""
import ctypes
import errno
import hashlib
import html
import json
import os
from pathlib import Path
import resource
import sys
import signal
import time
import re
import shutil
from buna.hosted_runtime import CPU_SECONDS, WALL_SECONDS, COMPARISON_SECONDS, atomic_json


class CpuBudgetExceeded(RuntimeError):
    pass


class ParseTimeout(RuntimeError):
    pass


def isolate(uid: int) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Public workers require the Linux container isolation boundary.")
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS + 10))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (128,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)
    resource.setrlimit(resource.RLIMIT_NPROC, (24, 24))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise RuntimeError("Cannot enforce no-new-privileges.")
    seccomp = ctypes.CDLL("libseccomp.so.2")
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    context = seccomp.seccomp_init(0x7FFF0000)  # default allow; deny networking and privileged introspection
    if not context:
        raise RuntimeError("Cannot create syscall filter.")
    try:
        for name in ("socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
                     "ptrace", "process_vm_readv", "process_vm_writev", "mount", "umount2",
                     "unshare", "setns", "bpf", "keyctl", "perf_event_open", "io_uring_setup",
                     "fork", "vfork", "clone", "clone3", "setsid", "setpgid", "execve", "execveat"):
            number = seccomp.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and seccomp.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0) != 0:
                raise RuntimeError("Cannot install syscall restriction.")
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("Cannot enforce syscall filter.")
    finally:
        seccomp.seccomp_release(context)


def source_excerpts_only(report: dict) -> None:
    """Keep complete matched passages, excluding unrelated source context only."""
    for match in report["matches"]:
        source = match.get("source") or {}
        text = source.get("text", "")
        original_start = source.get("start", 0)
        start = max(0, source.get("match_start", original_start) - original_start)
        end = min(len(text), source.get("match_end", original_start + len(text)) - original_start)
        if not 0 <= start <= end <= len(text):
            raise ValueError("Saved source excerpt bounds are invalid.")
        excerpt = text[start:end]
        for field in ("highlights", "scored_highlights"):
            if field in source:
                source[field] = [[max(a, start) - start, min(b, end) - start]
                                 for a, b in source[field] if max(a, start) < min(b, end)]
        source.update(text=excerpt, start=original_start + start,
                      end=original_start + start + len(excerpt),
                      match_start=original_start + start,
                      match_end=original_start + start + len(excerpt))
    report["evidence_export"] = {
        "policy": "team-reports-only-v1",
        "notice": "Reports include complete saved matched source passages, not a full comparison-source appendix. Matching scores and counts are unchanged.",
    }
    report["warnings"].append(report["evidence_export"]["notice"])


def main():
    folder = Path(sys.argv[1])
    isolate(int(sys.argv[2]))
    os.chdir(folder)
    from buna.documents import extract_document, ExtractionError
    from buna.comparison import compare_documents, text_fingerprint
    from buna.pdf_reports import generate_pdf, _typeset
    from buna.result_state import result_state
    import pymupdf
    signal.signal(signal.SIGXCPU, lambda *_: (_ for _ in ()).throw(CpuBudgetExceeded()))
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(ParseTimeout()))
    state = {"stage": "starting", "processed_sources": 0, "checked_sources": 0}
    def progress(stage, **counts):
        state.update(stage=stage, **counts)
        atomic_json(Path("progress.json"), state)
    def extract(path, profile="manuscript"):
        signal.setitimer(signal.ITIMER_REAL, 35)
        try:
            return extract_document(path, profile=profile)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    request = json.loads(Path("request.json").read_text())
    progress("parse-manuscript", source_count=len(request["sources"]))
    target = extract(Path(request["target"]))
    if len(target["text"]) > 250_000:
        raise ValueError("Public manuscript limit is 250,000 extracted characters.")
    target_fingerprint = text_fingerprint(target["text"])
    share_results = {}
    sources = []
    parsed_cache_bytes = 0
    for index, entry in enumerate(request["sources"]):
        sources.append({"id": str(index + 1), "title": entry["title"], "filename": entry["title"],
                        "entry": entry, "status": "parsed", "parsed": True})
    if not sources:
        raise ValueError("At least one readable comparison source is required.")
    def load_document(source):
        nonlocal parsed_cache_bytes
        entry = source["entry"]
        progress("load-source", source_index=int(source["id"]))
        if entry.get("unavailable_reason"):
            raise ValueError(entry["unavailable_reason"])
        if entry.get("cached_document"):
            try:
                raw = Path(entry["cached_document"]).read_bytes()
            except OSError:
                raise ValueError("Immutable source extraction cache is unavailable.") from None
            if hashlib.sha256(raw).hexdigest() != entry["cached_sha256"]:
                raise ValueError("Immutable source extraction cache failed validation.")
            return json.loads(raw)
        parsed = Path("parsed-source-" + source["id"] + ".json")
        if parsed.exists():
            return json.loads(parsed.read_text())
        canonical = None
        try:
            parse_path = Path(entry["path"])
            if entry.get("keep_in_library"):
                digest = hashlib.sha256(parse_path.read_bytes()).hexdigest()
                canonical = Path("shared-validate-" + digest + ".pdf")
                shutil.copyfile(parse_path, canonical)
                parse_path = canonical
            document = extract(parse_path, profile="source")
        except (ExtractionError, ParseTimeout) as exc:
            # Report source-local extraction failures without returning paths or content.
            raise ValueError("Comparison source extraction failed (" + type(exc).__name__ + ").") from None
        finally:
            if canonical:
                canonical.unlink(missing_ok=True)
        if entry.get("keep_in_library"):
            from buna.shared_library import checked_document, SharedLibraryError
            try:
                if Path(entry["path"]).suffix.lower() != ".pdf":
                    raise SharedLibraryError("Only supplementary PDFs can be saved.")
                if text_fingerprint(document["text"]) == target_fingerprint:
                    raise SharedLibraryError("This source is identical to your manuscript and was not shared.")
                checked_document(document)
                share_results[source["id"]] = {"validated": True}
            except SharedLibraryError as exc:
                share_results[source["id"]] = {"validated": False, "reason": str(exc)}
            atomic_json(Path("share-validation.json"), share_results)
        encoded = json.dumps(document, ensure_ascii=False).encode()
        if entry.get("keep_in_library") or parsed_cache_bytes + len(encoded) <= 32 * 1024 * 1024:
            parsed.write_bytes(encoded)
            parsed_cache_bytes += len(encoded)
        return document
    last_progress = 0
    def comparison_progress(message):
        nonlocal last_progress
        if time.monotonic() - last_progress < .5:
            return
        source_numbers = re.search(r"source (\d+)/(\d+)", message)
        if source_numbers:
            index, total = map(int, source_numbers.groups())
            windows = re.search(r"window (\d+)/(\d+)", message)
            counts = dict(zip(("source_windows_visited", "source_windows_total"), map(int, windows.groups()))) if windows else {}
            progress("compare", source_index=index, source_count=total, **counts)
            last_progress = time.monotonic()
    def source_done(row, matches):
        state["processed_sources"] += 1
        state["checked_sources"] += int(row["status"] == "compared")
        progress("compare")
    model = request.get("comparison_model", "validated-lexical")
    if model == "improvedEng":
        from buna.improved_eng import improved_report
        report = improved_report(target, sources, exclude_quotes=True, load_document=load_document,
                                 progress=comparison_progress, source_progress=lambda row: source_done(row, []),
                                 total_time_limit_seconds=COMPARISON_SECONDS)
    elif model == "classified-v1.1":
        from buna.classified import classify_report
        report = classify_report(target, sources, exclude_quotes=True, load_document=load_document,
                                 progress=comparison_progress, source_progress=lambda row: source_done(row, []),
                                 total_time_limit_seconds=COMPARISON_SECONDS)
    elif model == "validated-lexical":
        report = compare_documents(target, sources, exclude_quotes=True, load_document=load_document,
                                   progress=comparison_progress, source_done=source_done,
                                   total_time_limit_seconds=COMPARISON_SECONDS)
    else:
        raise ValueError("Unsupported comparison model.")
    if request.get("reports_only"):
        source_excerpts_only(report)
    checked = {row["source_id"]: row for row in report["source_coverage"]}
    papers = []
    for source in sources:
        row = checked.get(source["id"], {})
        papers.append({"id": source["id"], "title": source["title"], "filename": source["filename"],
                       "source_number": int(source["id"]),
                       "status": "compared" if row.get("status") == "compared" else "parsed",
                       "partial_comparison": row.get("status") == "compared-with-limits",
                       "overlap_percent": row.get("overlap_percent")})
    report["papers"] = papers
    report["attributions"] = request["attributions"]
    report["hosted_runtime"] = {"profile": "cached-corpus-v1", "cpu_seconds": CPU_SECONDS,
                                "wall_seconds": WALL_SECONDS, "comparison_seconds": COMPARISON_SECONDS,
                                "cached_sources": sum(bool(source["entry"].get("cached_document")) for source in sources)}
    outcome = result_state(report)
    summary = {
        "overlap_percent": report["metrics"]["overlap_percent"],
        "checked": sum(row.get("status") == "compared" for row in checked.values()),
        "total": len(sources), "warnings": report["warnings"],
        "partial": outcome["state"] == "partial" or outcome["score_kind"] == "lower_bound",
        "algorithm_version": report["algorithm_version"], "score_available": outcome["score_available"],
        "comparison_model": model,
        "score_basis": report["metrics"].get("score_basis"),
        "score_policy_version": report["metrics"].get("score_policy_version"),
        "word_accounting": {key: report["metrics"].get(key) for key in (
            "total_words", "scoped_words", "eligible_words", "score_denominator_words", "overlapping_words",
            "front_matter_words", "excluded_bibliography_words", "excluded_quotation_words", "other_excluded_manuscript_words")},
    }
    if report.get("classification"):
        summary["classification_counts"] = {key: report["classification"]["metrics"][key] for key in
            ("exact_words", "similar_only_words", "unmatched_words", "not_fully_checked_words")}
    progress("write-evidence")
    if report.get("comparison_model") == "improvedEng":
        from buna.match_diagnostics import diagnostics_csv
        report["improved_eng"]["similar_diagnostics_csv"] = diagnostics_csv(report)
        Path("similar-diagnostics.csv").write_text(report["improved_eng"]["similar_diagnostics_csv"], encoding="utf-8")
    Path("report.json").write_text(json.dumps(report, ensure_ascii=False))
    atomic_json(Path("comparison-complete.json"), summary)
    progress("render-pdf")
    mapping = generate_pdf({"job": {"filename": request["title"], "document": target,
                          "manuscript_sha256": hashlib.sha256(Path(request["target"]).read_bytes()).hexdigest()},
                  "report": report, "original": request["target"]}, Path("report.pdf"))
    report["pdf_mapping"] = mapping
    atomic_json(Path("report.json"), report)
    if request["attributions"]:
        progress("attribution")
        body = "<h1>Public source attribution</h1>"
        for paper in request["attributions"]:
            body += "<h2>" + html.escape(paper["title"]) + "</h2>"
            for field in ("attribution", "version", "license", "license_url", "source_url"):
                body += "<p>" + html.escape(paper[field]) + "</p>"
        appendix = _typeset(body)
        with pymupdf.open("report.pdf") as pdf:
            pdf.insert_pdf(appendix)
            pdf.save("attributed-report.pdf", garbage=4, deflate=True)
        appendix.close()
        os.replace("attributed-report.pdf", "report.pdf")
    atomic_json(Path("complete.json"), summary)
    progress("complete")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        from buna.hosted_runtime import safe_progress
        folder = Path(sys.argv[1])
        state = safe_progress(folder)
        code = "worker-error"
        if isinstance(exc, CpuBudgetExceeded):
            code = "cpu-limit"
        elif isinstance(exc, MemoryError):
            code = "memory-limit"
        elif isinstance(exc, ParseTimeout):
            code = "parse-timeout"
        elif state.get("stage") in {"render-pdf", "attribution"}:
            code = "pdf-error"
        elif state.get("stage") == "parse-manuscript":
            message = str(exc).lower()
            code = ("parse-manuscript-empty" if "no extractable text" in message else
                    "parse-manuscript-encrypted" if "encrypted" in message else
                    "parse-manuscript-limit" if "limit" in message else "parse-manuscript-invalid")
        try:
            atomic_json(folder / "failure.json", {"code": code, "exception_type": type(exc).__name__})
        except OSError:
            pass
        sys.exit(1)
