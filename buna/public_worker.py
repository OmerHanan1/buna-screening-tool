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


def isolate(uid: int) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Public workers require the Linux container isolation boundary.")
    resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (180,) * 2)
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


def main():
    folder = Path(sys.argv[1])
    isolate(int(sys.argv[2]))
    os.chdir(folder)
    from buna.documents import extract_document
    from buna.comparison import compare_documents
    from buna.pdf_reports import generate_pdf, _typeset
    from buna.result_state import result_state
    import pymupdf
    request = json.loads(Path("request.json").read_text())
    target = extract_document(Path(request["target"]))
    if len(target["text"]) > 250_000:
        raise ValueError("Public manuscript limit is 250,000 extracted characters.")
    sources = []
    for index, entry in enumerate(request["sources"]):
        document = extract_document(Path(entry["path"]), profile="source")
        sources.append({"id": str(index + 1), "title": entry["title"], "filename": entry["title"],
                        "document": document, "status": "parsed", "parsed": True})
    if not sources:
        raise ValueError("At least one readable comparison source is required.")
    report = compare_documents(target, sources, exclude_quotes=True)
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
    Path("report.json").write_text(json.dumps(report, ensure_ascii=False))
    generate_pdf({"job": {"filename": request["title"], "document": target,
                          "manuscript_sha256": hashlib.sha256(Path(request["target"]).read_bytes()).hexdigest()},
                  "report": report, "original": request["target"]}, Path("report.pdf"))
    if request["attributions"]:
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
    outcome = result_state(report)
    Path("complete.json").write_text(json.dumps({
        "overlap_percent": report["metrics"]["overlap_percent"],
        "checked": sum(row.get("status") == "compared" for row in checked.values()),
        "total": len(sources), "warnings": report["warnings"],
        "partial": any(row.get("status") != "compared" for row in checked.values()),
        "algorithm_version": report["algorithm_version"],
        "score_available": outcome["score_available"],
    }))


if __name__ == "__main__":
    main()
