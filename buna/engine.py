import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import psutil

from buna.comparison import ALGORITHM_VERSION, compare_documents
from buna.network import NetworkError, safe_fetch
from buna.providers import DiscoveryCancelled, discover, same_paper
from buna.reports import render_report
from buna.storage import Store, now
from buna.result_state import result_state
from buna.local_alignment import DEFAULT_POLICY
from buna.presentation import evidence_presentation, manuscript_reader
from buna.documents import current_structure, MAX_BYTES, SOURCE_MAX_BYTES, SOURCE_MAX_CHARACTERS, PARSER_SECONDS, PARSER_MEMORY_BYTES

logger = logging.getLogger(__name__)
MAX_UPLOAD = MAX_BYTES
MAX_SOURCE_UPLOAD = SOURCE_MAX_BYTES
ACTIVE = {"queued", "running"}
_SOURCE_PARSERS = threading.BoundedSemaphore(2)


class ParseFailure(ValueError):
    pass


def _parse_source(path: Path, cancelled=None) -> dict:
    # Spool bounded parser output to disk rather than filling a pipe while the
    # parent monitors wall time, resident memory (including macOS), and cancellation.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        with subprocess.Popen([sys.executable, "-m", "buna.parse_worker", str(path), "source"],
                              stdout=output, stderr=errors) as process:
            started = time.monotonic()
            try:
                monitor = psutil.Process(process.pid)
                while process.poll() is None:
                    if cancelled and cancelled():
                        raise DiscoveryCancelled("Source parsing cancelled.")
                    if time.monotonic() - started > PARSER_SECONDS:
                        raise ParseFailure(f"Document parsing exceeded the {PARSER_SECONDS}-second safety limit.")
                    try:
                        memory = monitor.memory_info().rss
                    except psutil.NoSuchProcess:
                        memory = 0
                    except psutil.Error as exc:
                        raise ParseFailure("Cannot enforce the source parser memory limit.") from exc
                    if memory > PARSER_MEMORY_BYTES:
                        raise ParseFailure("Source parsing exceeded the 768 MiB memory safety limit.")
                    if output.tell() > SOURCE_MAX_CHARACTERS * 32 or errors.tell() > 1024 * 1024:
                        raise ParseFailure("Source parser output exceeded its safety limit.")
                    time.sleep(.05)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
        if output.tell() > SOURCE_MAX_CHARACTERS * 32:
            raise ParseFailure("Source parser output exceeded its safety limit.")
        output.seek(0)
        try:
            payload = json.load(output)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ParseFailure("Source parser stopped unexpectedly (resource limit or malformed file).") from exc
        if process.returncode or "error" in payload:
            raise ParseFailure(payload.get("error", "Source extraction failed."))
        return payload["document"]


def parse_file(path: Path, *, profile: str = "manuscript", cancelled=None) -> dict:
    if profile == "source":
        waiting = time.monotonic()
        while not _SOURCE_PARSERS.acquire(timeout=.05):
            if cancelled and cancelled():
                raise DiscoveryCancelled("Source parsing cancelled while waiting for a parser.")
            if time.monotonic() - waiting > PARSER_SECONDS:
                raise ParseFailure("Source parsers are busy; retry after current parsing finishes.")
        try:
            return _parse_source(path, cancelled)
        finally:
            _SOURCE_PARSERS.release()
    if profile != "manuscript":
        raise ParseFailure("Unknown extraction profile.")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "buna.parse_worker", str(path)],
            capture_output=True, timeout=35, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ParseFailure("Document parsing exceeded the 35-second safety limit.") from exc
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseFailure("Document parser stopped unexpectedly (resource limit or malformed file).") from exc
    if result.returncode or "error" in payload:
        raise ParseFailure(payload.get("error", "Document extraction failed."))
    return payload["document"]


def write_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def coverage(papers: list[dict]) -> dict:
    return {
        "discovered": len(papers),
        **{stage: sum(bool(p.get(stage)) for p in papers)
           for stage in ("resolved", "downloaded", "parsed", "compared")},
        "unavailable": sum(p["status"] == "unavailable" for p in papers),
        "excluded": sum(p["status"] == "excluded" for p in papers),
        "partially_compared": sum(bool(p.get("partial_comparison")) for p in papers),
    }


def summarize_report(report: dict) -> dict:
    result = result_state(report)
    report["result"] = result
    report["evidence_presentation"] = evidence_presentation(report)
    report["reader"] = manuscript_reader(report)
    report["score_available"] = result["score_available"]
    if not report.get("algorithm_version"):
        report["algorithm_version"] = "1.0 (saved legacy results; not recalculated)"
    report["screening_summary"] = (
        f"{result['heading']}. {result['complete_sources']} fully compared; "
        f"{result['partial_sources']} partially examined; {result['unchecked_sources']} not checked; "
        f"{result['excluded_sources']} excluded. {result['explanation']}"
    )
    return report


def public_job(job: dict, detail: bool = True) -> dict:
    public = {key: value for key, value in job.items()
              if key not in {"consent", "manuscript_file", "document_file", "revision"}}
    public["papers"] = [
        {key: value for key, value in paper.items() if key not in {"file", "document_file"}}
        for paper in job["papers"]
    ]
    public["coverage"] = coverage(job["papers"])
    if not detail:
        public.pop("document", None)
    return public


class Engine:
    def __init__(self, store: Store, config: dict):
        self.store = store
        self.config = config
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="buna")
        self.events: dict[str, threading.Event] = {}
        self.lock = threading.Lock()
        for job in store.list():
            if job["status"] in ACTIVE:
                store.update(job["id"], lambda current: current.update(
                    status="partial", stage="interrupted",
                    message="Server restarted during screening. Local files are preserved; review and run again.",
                    warnings=current["warnings"] + ["Interrupted run; no automatic network restart."],
                ))

    def close(self) -> None:
        with self.lock:
            events = list(self.events.values())
        for event in events:
            event.set()
        self.pool.shutdown(wait=True, cancel_futures=True)

    def start(self, job_id: str, mode: str, provider: str) -> None:
        event = threading.Event()
        self.events[job_id] = event
        self.pool.submit(self.run, job_id, mode, provider, event)

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            event = self.events.get(job_id)
            if event:
                event.set()
            return self.store.update(job_id, lambda job: job.update(
                cancel_requested=True, message="Cancellation requested; finishing the current bounded operation."
            ))

    def run(self, job_id: str, mode: str, provider: str, event: threading.Event) -> None:
        folder = self.store.folder(job_id)

        def update(**values: object) -> dict:
            return self.store.update(job_id, lambda job: job.update(**values))

        def check() -> None:
            if event.is_set():
                raise DiscoveryCancelled("Screening cancelled")

        def emit(message: str) -> None:
            update(message=message)

        try:
            check()
            job = update(status="running", stage="discovery" if mode == "online" else "acquisition", progress=5)
            manuscript = current_structure(job["document"])
            if mode == "online":
                discovered, references, warnings = discover(
                    job["queries"], job["references"], job["target"],
                    {**self.config, "provider": provider, "allow_oa_resolution": provider == "crossref"}, event.is_set, emit,
                )
                check()
                # Keep existing parsed uploads/cache on repeated runs. DOI/title
                # identity handles provider-assigned IDs changing between searches.
                papers = list(job["papers"])
                for paper in discovered:
                    previous = next((p for p in papers if same_paper(p, paper)), None)
                    if previous:
                        previous["origins"] = sorted(set(previous["origins"] + paper["origins"]))
                        for key in ("doi", "url", "pdf_url", "pdf_urls", "year", "authors", "resolution_confidence", "oa_resolution"):
                            if not previous.get(key) and paper.get(key):
                                previous[key] = paper[key]
                        if paper.get("resolution_confidence") == "confirmed":
                            previous.update(resolution_confidence="confirmed", resolved=True)
                        if paper.get("pdf_url"):
                            previous["pdf_url"] = paper["pdf_url"]
                            previous["pdf_urls"] = paper.get("pdf_urls") or [paper["pdf_url"]]
                            previous.pop("oa_resolution", None)
                        if paper.get("oa_resolution") is not None:
                            previous["oa_resolution"] = paper["oa_resolution"]
                        provenance = {"provider": paper.get("provider"), "provenance": paper.get("provenance")}
                        previous.setdefault("discovery_provenance", [])
                        if provenance not in previous["discovery_provenance"]:
                            previous["discovery_provenance"].append(provenance)
                        for reference in references:
                            if reference.get("paper_id") == paper["id"]:
                                reference["paper_id"] = previous["id"]
                    else:
                        paper.setdefault("warnings", [])
                        paper["resolved"] = paper.get("resolution_confidence") != "uncertain"
                        papers.append(paper)
                job = update(papers=papers, references=references, warnings=job["warnings"] + warnings)
            papers = job["papers"]
            update(stage="acquisition", progress=30)
            sources = []
            for index, paper in enumerate(papers):
                check()
                paper["source_number"] = index + 1
                if paper.get("excluded"):
                    sources.append({**paper})
                    continue
                if paper.get("upload_failed"):
                    continue
                emit(f"Preparing source {index + 1} of {len(papers)}: {paper['title'][:100]}")
                try:
                    document_path = folder / paper["document_file"] if paper.get("document_file") else None
                    if document_path and document_path.exists():
                        document = json.loads(document_path.read_text(encoding="utf-8"))
                    else:
                        if mode == "offline":
                            raise ParseFailure("Offline mode: no local full text attached.")
                        if paper.get("resolution_confidence") == "uncertain":
                            raise ParseFailure("Uncertain reference match: correct the reference or attach verified full text.")
                        if not paper.get("pdf_url"):
                            explanation = "No authorized open-access PDF location supplied by provider. Attach a lawful local copy."
                            oa_resolution = paper.get("oa_resolution")
                            if isinstance(oa_resolution, dict) and oa_resolution.get("reason"):
                                explanation += f" OA resolution: {oa_resolution['reason']}"
                            raise ParseFailure(explanation)
                        filename = f"source-{paper['id']}.pdf"
                        path = folder / filename
                        urls = list(dict.fromkeys([paper["pdf_url"], *paper.get("pdf_urls", [])]))[:3]
                        attempts = []
                        paper["acquisition_attempts"] = attempts
                        for url in urls:
                            check()
                            try:
                                data, content_type, final_url = safe_fetch(
                                    url, max_bytes=MAX_SOURCE_UPLOAD, timeout=20, cancelled=event.is_set,
                                )
                                check()
                                if not data.startswith(b"%PDF-") or "html" in content_type.lower():
                                    raise ParseFailure("Open-access location did not return a PDF; abstract/landing pages are not full text.")
                                path.write_bytes(data)
                                os.chmod(path, 0o600)
                                paper.update(
                                    file=filename, downloaded=True, status="downloaded", acquired_from=final_url,
                                    sha256=hashlib.sha256(data).hexdigest(),
                                )
                                update(papers=papers)
                                document = parse_file(path, profile="source", cancelled=event.is_set)
                            except (NetworkError, ParseFailure, TimeoutError) as exc:
                                attempts.append({"url": url, "status": "failed", "reason": str(exc)})
                                update(papers=papers)
                            else:
                                attempts.append({"url": url, "final_url": final_url, "status": "parsed"})
                                break
                        else:
                            reasons = "; ".join(f"Attempt {index + 1}: {attempt['reason']}" for index, attempt in enumerate(attempts))
                            raise ParseFailure(f"No usable full text from {len(attempts)} authorized OA location(s). {reasons}")
                        document_path = folder / f"source-{paper['id']}.json"
                        write_json(document_path, document)
                        paper["document_file"] = document_path.name
                    paper.update(parsed=True, status="parsed", error=None)
                    sources.append({**paper, **({"document": current_structure(document)} if job.get("workflow") != "manual" else {})})
                except DiscoveryCancelled:
                    raise
                except Exception as exc:
                    if event.is_set():
                        raise DiscoveryCancelled("Screening cancelled") from exc
                    logger.warning("Source preparation failed for job %s paper %s (%s)", job_id, paper["id"], type(exc).__name__)
                    paper.update(status="unavailable", parsed=False, compared=False, error=str(exc))
                update(papers=papers, progress=30 + round(35 * (index + 1) / max(1, len(papers))))
            check()
            update(stage="comparison", progress=70, message="Comparing local full text and aggregating unique manuscript spans.")
            model = job.get("comparison_settings", {}).get("comparison_model", "validated-lexical")
            algorithm = ALGORITHM_VERSION
            if model == "improvedEng":
                from buna.improved_eng import VERSION as IMPROVED_VERSION
                algorithm = IMPROVED_VERSION
            checkpoint_key = consent_digest({
                "algorithm": algorithm, "score_policy": "eligible-manuscript-v1", "manuscript": job.get("manuscript_sha256"),
                "source_capacity_profile": "source-v1", "source_character_limit": SOURCE_MAX_CHARACTERS,
                "settings": job.get("comparison_settings", {}),
                "sources": [(p["id"], p.get("sha256"), p.get("excluded"), p.get("doi")) for p in papers],
            })
            completed_sources = 0

            def load_document(source: dict) -> dict:
                return current_structure(json.loads((folder / source["document_file"]).read_text(encoding="utf-8")))

            def load_checkpoint(source: dict) -> dict | None:
                path = folder / f"comparison-{source['id']}.json"
                if not path.exists():
                    return None
                try:
                    cached = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    logger.warning("Unreadable comparison checkpoint for source %s; comparing again.", source["id"])
                    return None
                return cached if cached.get("key") == checkpoint_key else None

            def source_done(row: dict, evidence: list[dict]) -> None:
                nonlocal completed_sources
                write_json(folder / f"comparison-{row['source_id']}.json",
                           {"key": checkpoint_key, "coverage": row, "matches": evidence})
                completed_sources += 1
                for paper in papers:
                    if paper["id"] == row["source_id"]:
                        paper["comparison_progress"] = row
                update(papers=papers, progress=70 + round(24 * completed_sources / max(len(sources), 1)),
                       message=f"Processed {completed_sources} of {len(sources)} source papers.")

            if model == "improvedEng":
                from buna.improved_eng import improved_report
                report = improved_report(
                    manuscript, sources, cancelled=event.is_set, progress=emit,
                    exclude_quotes=job.get("comparison_settings", {}).get("exclude_quotes", True),
                    load_document=load_document if job.get("workflow") == "manual" else None,
                    source_progress=lambda row: source_done(row, []),
                    total_time_limit_seconds=480,
                )
            else:
                report = compare_documents(
                    manuscript, sources, cancelled=event.is_set,
                    **({"exclude_quotes": job.get("comparison_settings", {}).get("exclude_quotes", True), "progress": emit,
                        "match_policy": DEFAULT_POLICY if model == "experimental-ordered" else None,
                        "load_document": load_document, "source_done": source_done, "load_checkpoint": load_checkpoint}
                       if job.get("workflow") == "manual" else {}),
                )
            check()
            excluded_ids = set(report.get("excluded_source_ids", []))
            comparison_coverage = {row["source_id"]: row for row in report.get("source_coverage", [])}
            for paper in papers:
                if paper.get("excluded"):
                    paper.update(status="excluded", compared=False, error=paper.get("exclusion_reason"))
                elif paper.get("parsed"):
                    result = comparison_coverage.get(paper["id"], {})
                    paper["comparison_status"] = result.get("status", "missing")
                    if paper["id"] in excluded_ids or result.get("status") == "excluded":
                        paper.update(status="excluded", compared=False)
                    elif result.get("status") == "compared":
                        paper.update(status="compared", compared=True, partial_comparison=False)
                    elif result.get("status") == "compared-with-limits":
                        paper.update(
                            status="parsed", compared=False, partial_comparison=True,
                            error=result.get("reason") or "Partially compared: a declared resource limit was reached. Retained evidence is a lower bound.",
                        )
                    else:
                        reasons = {
                            "skipped-evidence-limit": "Not compared: the global evidence limit was reached before this article.",
                            "skipped-size-limit": "Not compared: extracted text exceeds the comparison size limit.",
                            "unavailable": "Not compared: the extracted document is missing or empty.",
                        }
                        paper.update(
                            status="unavailable", compared=False,
                            error=result.get("reason") or result.get("error") or
                            reasons.get(result.get("status")) or
                            "This source was not fully compared; inspect comparison coverage and limits.",
                        )
            warnings = list(dict.fromkeys(job["warnings"] + report.get("warnings", [])))
            if mode == "offline":
                warnings.append("Only the manually uploaded comparison papers were checked. No external search or metadata requests were made.")
            compared_count = sum(bool(p.get("compared")) for p in papers)
            if not compared_count:
                if any(p.get("partial_comparison") for p in papers):
                    warnings.append("No article was fully compared: comparison limits left only partial evidence. Retained overlap is a lower bound.")
                else:
                    warnings.append("No full-text source could be compared. Zero overlap is not evidence of originality.")
            unavailable = any(p["status"] == "unavailable" for p in papers)
            incomplete_refs = mode == "online" and any(r.get("status") != "resolved" for r in job["references"])
            topical_count = sum("topical" in p["origins"] for p in papers)
            partial = (
                unavailable or incomplete_refs or not compared_count or report["metrics"].get("unscorable", False)
                or bool(report.get("metrics", {}).get("truncated"))
                or (mode == "online" and topical_count < job["target"])
            )
            update(stage="report", progress=95)
            report.update(
                schema_version="2.0", generated_at=now(),
                job={
                    "id": job_id, "title": job["title"], "mode": mode, "target": job["target"],
                    "provider": provider if mode == "online" else None,
                    "provider_pipeline": (["crossref", "openalex"] if provider == "crossref" else ["openalex"]) if mode == "online" else [],
                    "manuscript_sha256": job.get("manuscript_sha256"),
                },
                papers=public_job({**job, "papers": papers})["papers"],
                references=job["references"], coverage=coverage(papers), warnings=warnings,
                status="partial" if partial else "completed",
                scope="Only the manually uploaded comparison papers. No literature-wide search or originality assessment."
                if mode == "offline" else "Limited to the listed sources.",
            )
            summarize_report(report)
            if report.get("comparison_model") == "improvedEng":
                from buna.match_diagnostics import diagnostics_csv
                report["improved_eng"]["similar_diagnostics_csv"] = diagnostics_csv(report)
                (folder / "similar-diagnostics.csv").write_text(report["improved_eng"]["similar_diagnostics_csv"], encoding="utf-8")
            write_json(folder / "report.json", report)
            html_temp = folder / "report.html.tmp"
            html_temp.write_text(render_report(report), encoding="utf-8")
            os.chmod(html_temp, 0o600)
            html_temp.replace(folder / "report.html")
            update(
                papers=papers, warnings=warnings, status=report["status"], stage="done", progress=100,
                message="Report ready for human review." if not partial else "Partial report ready. Review corpus coverage and warnings.",
                report_available=True,
            )
        except DiscoveryCancelled:
            update(status="cancelled", stage="cancelled", message="Screening cancelled. Prepared files remain local; run again when ready.")
        except Exception as exc:
            if event.is_set():
                update(status="cancelled", stage="cancelled", message="Screening cancelled. Prepared files remain local; run again when ready.")
            else:
                logger.exception("Screening failed for job %s", job_id)
                update(status="failed", stage="failed", message=f"{type(exc).__name__}: {exc}")
        finally:
            with self.lock:
                if self.events.get(job_id) is event:
                    self.events.pop(job_id, None)


def consent_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
