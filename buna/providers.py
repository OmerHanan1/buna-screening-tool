"""Real Crossref/OpenAlex discovery, conservative references, and deduplication."""
from __future__ import annotations

import html
import re
import unicodedata
import uuid
from difflib import SequenceMatcher
from urllib.parse import quote, unquote, urlencode, urlsplit

from .network import DiscoveryCancelled, NetworkError, safe_json

MAX_PAGES = 10
MAX_QUERIES = 10
PAGE_SIZE = 100
MAX_PDF_URLS = 3
OA_BATCH_SIZE = 50
MAX_OA_PAGES = 3


def normalize_doi(value):
    value = str(value or "").strip()
    if re.match(r"https?://(?:dx\.)?doi\.org/", value, re.I):
        value = urlsplit(value).path
    value = unquote(value)
    match = re.search(r"10\.\d{4,9}/[^\s<>\"]+", value, re.I)
    if not match:
        return ""
    doi = match.group(0).rstrip(".,;")
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    return doi.lower()


def normalize_title(title):
    title = html.unescape(re.sub(r"<[^>]+>", "", str(title or "")))
    title = unicodedata.normalize("NFKC", title).casefold()
    title = re.sub(r"\s*[\[(](?:preprint|accepted manuscript|version\s*\d+|v\d+)[\])]\s*", " ", title)
    return " ".join(re.findall(r"\w+", title, re.UNICODE))


def same_paper(left: dict, right: dict) -> bool:
    """Compare DOI OR normalized title/version, including retained aliases.

    This comparison never mutates either record, so callers can preserve an
    existing paper's ID, local files, extraction results, and other attributes.
    """
    for field, aliases, normalize in (
        ("doi", "alternate_dois", normalize_doi),
        ("title", "alternate_titles", normalize_title),
    ):
        left_keys = {
            normalize(value) for value in [left.get(field), *(left.get(aliases) or [])]
        } - {""}
        right_keys = {
            normalize(value) for value in [right.get(field), *(right.get(aliases) or [])]
        } - {""}
        if left_keys & right_keys:
            return True
    return False


def _merge(existing, incoming):
    for key in ("origins", "warnings", "alternate_dois", "alternate_titles", "alternate_ids", "pdf_urls"):
        existing[key] = list(dict.fromkeys(existing.get(key, []) + incoming.get(key, [])))
    existing["pdf_urls"] = existing["pdf_urls"][:MAX_PDF_URLS]
    if incoming.get("id") and incoming["id"] != existing.get("id"):
        if incoming["id"] not in existing["alternate_ids"]:
            existing["alternate_ids"].append(incoming["id"])
    for key, normalize in (("doi", normalize_doi), ("title", normalize_title)):
        value = normalize(incoming.get(key))
        if value and value != normalize(existing.get(key)):
            aliases = existing.setdefault(f"alternate_{key}s", [])
            if value not in aliases:
                aliases.append(value)
    for key in ("doi", "year", "url", "pdf_url", "authors"):
        if not existing.get(key) and incoming.get(key):
            existing[key] = incoming[key]
    if incoming.get("resolution_confidence") == "confirmed":
        existing["resolution_confidence"] = "confirmed"
    sources = existing.setdefault("provenance", [])
    for source in incoming.get("provenance", []):
        if source not in sources:
            sources.append(source)


def deduplicate(papers: list[dict]) -> list[dict]:
    """Merge DOI/title-equivalent records, retaining the first stable paper ID."""
    output, doi_index, title_index = [], {}, {}
    for paper in papers:
        doi, title = normalize_doi(paper.get("doi")), normalize_title(paper.get("title"))
        dois = [value for value in [doi, *paper.get("alternate_dois", [])] if value]
        titles = [value for value in [title, *paper.get("alternate_titles", [])] if value]
        matches = []
        for index, keys in ((doi_index, dois), (title_index, titles)):
            for key in keys:
                existing = index.get(key)
                if existing is not None and all(existing is not match for match in matches):
                    matches.append(existing)
        existing = min(matches, key=lambda p: next(i for i, item in enumerate(output) if item is p)) if matches else None
        if existing is not None:
            for other in matches:
                if other is existing:
                    continue
                _merge(existing, other)
                output = [item for item in output if item is not other]
                for index in (doi_index, title_index):
                    for key, value in index.items():
                        if value is other:
                            index[key] = existing
            _merge(existing, paper)
            paper = existing
        else:
            paper = {
                **paper,
                "origins": list(paper.get("origins", [])),
                "warnings": list(paper.get("warnings", [])),
                "provenance": list(paper.get("provenance", [])),
                "alternate_dois": list(paper.get("alternate_dois", [])),
                "alternate_titles": list(paper.get("alternate_titles", [])),
                "alternate_ids": list(paper.get("alternate_ids", [])),
                "pdf_urls": list(paper.get("pdf_urls", [])),
            }
            output.append(paper)
        for value in dois:
            doi_index[value] = paper
        for value in titles:
            title_index[value] = paper
    return output


def _check(cancelled):
    if cancelled():
        raise DiscoveryCancelled("Discovery cancelled.")


def _https(value):
    return value if isinstance(value, str) and value.startswith("https://") else None


def _openalex_oa_locations(item):
    pdf_urls, oa_locations = [], []
    locations = [
        item.get("best_oa_location"), *(item.get("locations") or []),
        item.get("primary_location"),
    ]
    for location in locations:
        if not isinstance(location, dict) or location.get("is_oa") is not True:
            continue
        candidate_url = _https(location.get("pdf_url"))
        if not candidate_url or candidate_url in pdf_urls:
            continue
        pdf_urls.append(candidate_url)
        oa_locations.append({
            "id": location.get("id"), "is_oa": True,
            "license": location.get("license"), "version": location.get("version"),
            "landing_page_url": location.get("landing_page_url"),
            "pdf_url": candidate_url, "original_pdf_url": location.get("pdf_url"),
        })
        if len(pdf_urls) >= MAX_PDF_URLS:
            break
    return pdf_urls, oa_locations


def _paper(item, provider, origin):
    if not isinstance(item, dict):
        return None
    doi = normalize_doi(item.get("DOI") if provider == "crossref" else item.get("doi"))
    pdf_urls, oa_locations = [], []
    if provider == "crossref":
        titles = item.get("title") or []
        title = titles[0] if isinstance(titles, list) and titles else titles
        authors = [
            " ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name", "")
            for a in item.get("author", []) if isinstance(a, dict)
        ]
        year = None
        for field in ("published", "published-print", "published-online", "issued"):
            dates = (item.get(field) or {}).get("date-parts") or []
            if dates and dates[0]:
                year = dates[0][0]
                break
        url = _https(item.get("URL")) or (f"https://doi.org/{doi}" if doi else "")
        # Crossref TDM links/licenses alone do not establish public OA permission.
    else:
        title = item.get("title") or item.get("display_name")
        authors = [
            (a.get("author") or {}).get("display_name", "")
            for a in item.get("authorships", []) if isinstance(a, dict)
        ]
        year = item.get("publication_year")
        primary = item.get("primary_location") or {}
        url = _https(primary.get("landing_page_url")) or (
            f"https://doi.org/{doi}" if doi else _https(item.get("id")) or ""
        )
        pdf_urls, oa_locations = _openalex_oa_locations(item)
    if not isinstance(title, str) or not title.strip():
        return None
    return {
        "id": str(uuid.uuid4()), "title": title.strip(), "doi": doi or None,
        "year": year, "authors": [a for a in authors if a], "url": url,
        "pdf_url": pdf_urls[0] if pdf_urls else None, "pdf_urls": pdf_urls,
        "origins": [origin], "status": "discovered",
        "provider": provider,
        "provenance": [{
            "provider": provider, "record_id": item.get("id") or doi,
            "oa_locations": oa_locations,
        }],
        "warnings": [], "resolution_confidence": "confirmed",
        "oa_resolution": {
            "status": ("resolved" if pdf_urls else "unavailable") if provider == "openalex" else "not_requested",
            "reason": (
                "OpenAlex supplied an explicitly OA HTTPS PDF location." if pdf_urls
                else "No explicitly OA HTTPS PDF location was supplied."
            ) if provider == "openalex" else "Additional OpenAlex transmission was not authorized.",
        },
    }


class _Provider:
    def __init__(self, config, cancelled):
        self.name = config.get("provider", "crossref")
        if self.name not in {"crossref", "openalex"}:
            raise ValueError("Provider must be crossref or openalex.")
        self.cancelled = cancelled
        self.params = {}
        if config.get("contact_email"):
            self.params["mailto"] = config["contact_email"]
        self.headers = {"Accept": "application/json"}
        if self.name == "openalex" and config.get("openalex_api_key"):
            self.headers["Authorization"] = f"Bearer {config['openalex_api_key']}"

    def get(self, path, params):
        _check(self.cancelled)
        base = "https://api.crossref.org" if self.name == "crossref" else "https://api.openalex.org"
        data = safe_json(
            base + path + "?" + urlencode({**self.params, **params}),
            headers=self.headers, cancelled=self.cancelled,
        )
        _check(self.cancelled)
        if not isinstance(data, dict):
            raise NetworkError("Provider returned malformed metadata.")
        return data

    def search(self, query, cursor="*", rows=PAGE_SIZE):
        if self.name == "crossref":
            data = self.get("/works", {"query.bibliographic": query, "rows": rows, "cursor": cursor})
            message = data.get("message") or {}
            items, next_cursor = message.get("items"), message.get("next-cursor")
        else:
            data = self.get("/works", {"search": query, "per_page": rows, "cursor": cursor})
            items, next_cursor = data.get("results"), (data.get("meta") or {}).get("next_cursor")
        if not isinstance(items, list):
            raise NetworkError("Provider returned malformed search results.")
        return items, next_cursor

    def doi(self, doi):
        path = "/works/" + quote(doi if self.name == "crossref" else "https://doi.org/" + doi, safe="")
        data = self.get(path, {})
        return data.get("message") if self.name == "crossref" else data


def _enrich_crossref_oa(papers, config, cancelled, emit, warnings):
    """Resolve DOI batches only after the caller explicitly authorizes OpenAlex."""
    if config.get("allow_oa_resolution") is not True:
        return
    by_doi = {}
    for paper in papers:
        _check(cancelled)
        if paper.get("resolution_confidence") == "uncertain" and "topical" not in paper.get("origins", []):
            paper["oa_resolution"] = {
                "status": "skipped", "reason": "Confirm this uncertain reference before OA resolution.",
            }
            continue
        doi = normalize_doi(paper.get("doi"))
        if not doi or any(character in doi for character in "|,+"):
            paper["oa_resolution"] = {
                "status": "skipped",
                "reason": "No DOI suitable for an exact batch lookup; title-only OA matching was not attempted.",
            }
            continue
        paper["oa_resolution"] = {
            "status": "unavailable", "reason": "No matching OpenAlex DOI record was returned.",
        }
        by_doi.setdefault(doi, []).append(paper)
    resolver = _Provider({**config, "provider": "openalex"}, cancelled)
    dois = list(by_doi)
    total_batches = (len(dois) + OA_BATCH_SIZE - 1) // OA_BATCH_SIZE
    for offset in range(0, len(dois), OA_BATCH_SIZE):
        batch = dois[offset:offset + OA_BATCH_SIZE]
        cursor, seen_cursors, matched = "*", set(), set()
        failure = None
        for page in range(MAX_OA_PAGES):
            _check(cancelled)
            try:
                data = resolver.get("/works", {
                    "filter": "doi:" + "|".join(f"https://doi.org/{doi}" for doi in batch),
                    "per_page": PAGE_SIZE, "cursor": cursor,
                })
                items = data.get("results")
                if not isinstance(items, list):
                    raise NetworkError("OpenAlex returned malformed OA lookup results.")
                for item in items:
                    _check(cancelled)
                    if not isinstance(item, dict):
                        continue
                    doi = normalize_doi(item.get("doi"))
                    if doi not in batch:
                        continue
                    pdf_urls, locations = _openalex_oa_locations(item)
                    for paper in by_doi[doi]:
                        paper["pdf_urls"] = list(dict.fromkeys(
                            paper.get("pdf_urls", []) + pdf_urls
                        ))[:MAX_PDF_URLS]
                        paper["pdf_url"] = paper["pdf_urls"][0] if paper["pdf_urls"] else None
                        source = {
                            "provider": "openalex", "record_id": item.get("id"),
                            "purpose": "oa_resolution", "match": "exact_doi",
                            "oa_locations": locations,
                        }
                        if source not in paper["provenance"]:
                            paper["provenance"].append(source)
                        paper["oa_resolution"] = {
                            "status": "resolved" if paper["pdf_url"] else "unavailable",
                            "provider": "openalex", "match": "exact_doi",
                            "reason": (
                                "Exact DOI match supplied an explicitly OA HTTPS PDF location."
                                if paper["pdf_url"] else
                                "Exact DOI matched, but no explicitly OA HTTPS PDF location was supplied."
                            ),
                        }
                    matched.add(doi)
                next_cursor = (data.get("meta") or {}).get("next_cursor")
                if len(matched) == len(batch) or len(items) < PAGE_SIZE or not next_cursor:
                    break
                if next_cursor in seen_cursors or next_cursor == cursor or page == MAX_OA_PAGES - 1:
                    failure = "OpenAlex OA lookup reached its bounded pagination limit."
                    break
                seen_cursors.add(cursor)
                cursor = next_cursor
            except (NetworkError, ValueError, TypeError, AttributeError):
                failure = "OpenAlex OA lookup failed; retry after checking provider availability."
                break
        if failure:
            warnings.append(failure)
            for doi in batch:
                if doi not in matched:
                    for paper in by_doi[doi]:
                        paper["oa_resolution"] = {
                            "status": "failed", "provider": "openalex", "reason": failure,
                        }
        resolved = sum(p.get("oa_resolution", {}).get("status") == "resolved" for p in papers)
        emit(
            f"Resolved authorized OA PDF locations for {resolved} papers "
            f"(OpenAlex batch {offset // OA_BATCH_SIZE + 1} of {total_batches})."
        )
    _check(cancelled)


def discover(queries, references, target, config, cancelled, emit):
    """Return (papers, reference_results, warnings); never synthesize missing papers.

    target is an exact upper bound on unique topical papers (100–200).
    References are additive and all are attempted, even after provider outages.
    Uncertain title matches have no downloadable PDF until separately confirmed.
    Crossref results are enriched via exact-DOI OpenAlex batches only when
    ``allow_oa_resolution is True``. The caller must first obtain fresh consent
    covering both destinations and these additional DOI transmissions.
    """
    if isinstance(target, bool) or not isinstance(target, int) or not 100 <= target <= 200:
        raise ValueError("Topical target must be an integer between 100 and 200.")
    provider = _Provider(config, cancelled)
    papers, reference_results, warnings = [], [], []
    query_list = list(dict.fromkeys(q.strip() for q in queries if isinstance(q, str) and q.strip()))
    if len(query_list) > MAX_QUERIES:
        warnings.append(f"Only the first {MAX_QUERIES} search queries were used.")
    for query in query_list[:MAX_QUERIES]:
        cursor, seen_cursors = "*", set()
        for _ in range(MAX_PAGES):
            _check(cancelled)
            if len(papers) >= target:
                break
            try:
                items, next_cursor = provider.search(query, cursor)
                candidates = [_paper(item, provider.name, "topical") for item in items]
                papers = deduplicate(papers + [p for p in candidates if p])[:target]
                emit(f"Discovered {len(papers)} of {target} unique topical papers.")
            except (NetworkError, ValueError, TypeError, AttributeError):
                warnings.append(f"{provider.name} topical search failed; reference resolution will still be attempted.")
                break
            if not items or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            cursor = next_cursor
        if len(papers) >= target:
            break
    if len(papers) < target:
        warnings.append(f"Found only {len(papers)} of {target} requested unique topical papers; no results were fabricated.")
    for index, reference in enumerate(references):
        _check(cancelled)
        result = {**reference, "status": "unresolved", "paper_id": None, "message": ""}
        try:
            doi = normalize_doi(reference.get("doi")) or normalize_doi(reference.get("raw"))
            candidate, confidence = None, "uncertain"
            if doi:
                candidate = _paper(provider.doi(doi), provider.name, "referenced")
                if candidate and candidate.get("doi") == doi:
                    confidence = "confirmed"
                else:
                    candidate = None
            else:
                title = reference.get("title") or reference.get("raw") or ""
                if title.strip():
                    items, _ = provider.search(title, rows=3)
                    candidates = deduplicate([
                        paper for item in items
                        if (paper := _paper(item, provider.name, "referenced"))
                    ])
                    normalized = normalize_title(title)
                    ranked = sorted(candidates, key=lambda p: SequenceMatcher(
                        None, normalized, normalize_title(p["title"])
                    ).ratio(), reverse=True)
                    if ranked:
                        candidate = ranked[0]
                        exact = [p for p in ranked if normalize_title(p["title"]) == normalized]
                        if reference.get("title") and len(exact) == 1:
                            confidence = "confirmed"
                        elif SequenceMatcher(None, normalized, normalize_title(candidate["title"])).ratio() < 0.45:
                            candidate = None
            if candidate:
                candidate["resolution_confidence"] = confidence
                if confidence == "uncertain":
                    candidate["pdf_url"] = None
                    candidate["pdf_urls"] = []
                    candidate["oa_resolution"] = {
                        "status": "skipped", "reason": "Confirm this uncertain reference before OA resolution.",
                    }
                    candidate["warnings"].append("Uncertain reference match: confirm before downloading.")
                papers = deduplicate(papers + [candidate])
                canonical = next(
                    p for p in papers
                    if (candidate.get("doi") and p.get("doi") == candidate["doi"])
                    or candidate.get("doi") in p.get("alternate_dois", [])
                    or normalize_title(p["title"]) == normalize_title(candidate["title"])
                    or normalize_title(candidate["title"]) in p.get("alternate_titles", [])
                )
                result.update(
                    status="resolved" if confidence == "confirmed" else "uncertain",
                    paper_id=canonical["id"],
                    message="DOI/title matched." if confidence == "confirmed" else "Possible title match; confirmation required.",
                )
            else:
                result["message"] = "No reliable matching record was found."
        except (NetworkError, ValueError, TypeError, AttributeError):
            result["message"] = f"{provider.name} lookup failed; retry this reference later."
        reference_results.append(result)
        emit(f"Attempted reference {index + 1} of {len(references)}: {result['status']}.")
    _check(cancelled)
    canonical_ids = {
        alias: paper["id"] for paper in papers
        for alias in paper.get("alternate_ids", [])
    }
    for result in reference_results:
        result["paper_id"] = canonical_ids.get(result["paper_id"], result["paper_id"])
    if provider.name == "crossref":
        _enrich_crossref_oa(papers, config, cancelled, emit, warnings)
    return papers, reference_results, list(dict.fromkeys(warnings))
