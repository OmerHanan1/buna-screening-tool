"""DOI-only library discovery. Never accepts manuscript text or arbitrary fetch URLs."""
from __future__ import annotations

import json
import re
import threading
import time
import xml.etree.ElementTree as ElementTree
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from buna.network import DiscoveryCancelled, NetworkError, safe_fetch
from buna.providers import _paper

MAX_DOIS = 200
DISCLOSURE_VERSION = 2
DISCLOSURE = (
    "Import sends only the listed public DOIs to Crossref, OpenAlex and Europe PMC "
    "(DataCite and arXiv for DOIs they register; Unpaywall if configured). It then downloads "
    "open-access full text from publisher/repository hosts, reading their open-access landing "
    "pages for the host's own PDF link. These services see your IP; a configured contact "
    "email/key goes to its named API. No manuscript or private full text is shared. "
    "Import does not start a comparison."
)
BASES = {
    "crossref": "https://api.crossref.org", "openalex": "https://api.openalex.org",
    "unpaywall": "https://api.unpaywall.org", "datacite": "https://api.datacite.org",
    "europepmc": "https://www.ebi.ac.uk/europepmc/webservices/rest",
}
ARXIV = re.compile(r"10\.48550/arxiv\.((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[a-z]{2})?/\d{7})(?:v\d+)?)")
TRANSIENT = re.compile(r"HTTP (?:429|5\d\d)|timed out|cooldown|Retry-After|DNS resolution", re.I)


def is_transient(reason: str) -> bool:
    return bool(TRANSIENT.search(reason))


def _site(host: str) -> str:
    # Do not guess registrable domains: sibling tenants on github.io, ac.uk, etc.
    # are not the same publisher. Only the exact hostname (or www alias) qualifies.
    return (host or "").lower().rstrip(".").removeprefix("www.")


class _PdfMeta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta" and values.get("name", "").lower() == "citation_pdf_url" and values.get("content"):
            self.links.append(values["content"].strip())


def landing_pdf_link(html: bytes, page_url: str) -> str:
    """The host's own Highwire citation_pdf_url; must be HTTPS on the landing page's site."""
    parser = _PdfMeta()
    parser.feed(html.decode("utf-8", "replace"))
    for link in parser.links[:3]:
        url = urljoin(page_url, link)
        parts = urlsplit(url)
        if (parts.scheme == "https" and not parts.username and not parts.password
                and parts.port in (None, 443)
                and _site(parts.hostname) == _site(urlsplit(page_url).hostname)):
            return url
    raise NetworkError("The open-access landing page offers no same-site HTTPS PDF link (citation_pdf_url).")


def jats_text(data: bytes) -> str:
    """Plain text from Europe PMC OA-subset JATS XML; rejects entity declarations and bodiless records."""
    if b"<!ENTITY" in data or b"\x00" in data or len(data) > 20 * 1024 * 1024:
        raise ValueError("Unsafe or oversized XML full text.")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        raise ValueError("Malformed XML full text.") from None
    body = root.find(".//body")
    if body is None:
        raise ValueError("The XML record has no article body (abstract/metadata only).")
    lines = [" ".join("".join(root.find(".//article-title").itertext()).split())] if root.find(".//article-title") is not None else []

    def walk(node):
        for child in node:
            if child.tag == "title":
                lines.append("\n" + " ".join("".join(child.itertext()).split()))
            elif child.tag == "p":
                lines.append(" ".join("".join(child.itertext()).split()))
            elif child.tag in {"sec", "boxed-text", "list", "list-item"}:
                walk(child)
    walk(body)
    text = "\n\n".join(line for line in lines if line.strip())
    if len(text.split()) < 200:
        raise ValueError("The XML article body is too short to be full text.")
    return text


def canonical_doi(value: str) -> str:
    value = value.strip()
    if re.match(r"https?://", value, re.I):
        parts = urlsplit(value)
        if (parts.netloc.lower() not in {"doi.org", "dx.doi.org"}
                or parts.query or parts.fragment):
            raise ValueError("Use a bare DOI or a doi.org URL without query/fragment.")
        value = unquote(parts.path[1:])
    elif value.lower().startswith("doi:"):
        value = value[4:].strip()
    if (len(value) > 300 or not re.fullmatch(r"10\.\d{4,9}/[!-~]+", value, re.I)
            or any(c in value for c in '<>"\\')
            or value.count("(") != value.count(")")):
        raise ValueError("Invalid DOI; use one identifier per line or space, without wrapping punctuation.")
    return value.lower()


def preview_dois(text: str) -> dict:
    tokens = text.split()
    if len(tokens) > MAX_DOIS or len(text) > MAX_DOIS * 400:
        raise ValueError(f"Import at most {MAX_DOIS} DOI entries at a time.")
    dois, invalid, duplicates = [], [], []
    for index, token in enumerate(tokens):
        try:
            doi = canonical_doi(token)
        except ValueError as exc:
            invalid.append({"position": index + 1, "value": token[:400], "reason": str(exc)})
            continue
        if doi in dois:
            duplicates.append(doi)
        else:
            dois.append(doi)
    return {"dois": dois, "invalid": invalid, "duplicates": duplicates,
            "disclosure": DISCLOSURE, "disclosure_version": DISCLOSURE_VERSION}


def public_url(value: str) -> str:
    """Keep public DOI/content selectors, never signed query credentials."""
    try:
        parts = urlsplit(value)
        if parts.username is not None or parts.password is not None:
            return "(invalid source URL)"
        query = [(key, item) for key, item in parse_qsl(parts.query)
                 if (key in {"id", "doi"} and re.fullmatch(r"10\.\d{4,9}/[^\s]+", item))
                 or (key == "type" and item in {"printable", "pdf"})]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except ValueError:
        return "(invalid source URL)"


class Resolver:
    def __init__(self, config: dict):
        self.config = config
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.fetch = safe_fetch

    def get(self, provider, path, params, cancelled, errors):
        with self.lock:
            while time.monotonic() < self.next_request:
                if cancelled():
                    raise DiscoveryCancelled("Import cancelled.")
                time.sleep(min(.05, max(0, self.next_request - time.monotonic())))
            self.next_request = time.monotonic() + .25
            headers = {"Accept": "application/json"}
            params = dict(params)
            if provider == "openalex" and self.config.get("openalex_api_key"):
                headers["Authorization"] = f"Bearer {self.config['openalex_api_key']}"
            if provider == "crossref" and self.config.get("contact_email"):
                params["mailto"] = self.config["contact_email"]
            if provider == "unpaywall":
                params["email"] = self.config["unpaywall_email"]
            data, content_type, _ = self.fetch(
                f"{BASES[provider]}{path}?{urlencode(params)}",
                max_bytes=4_000_000, timeout=12, headers=headers, cancelled=cancelled,
                strict_retry_after=True,
                on_retry=lambda reason: errors.append({"provider": provider, "stage": "retry", "reason": reason}),
            )
        if "json" not in content_type.lower():
            raise NetworkError("Provider returned a non-JSON response.")
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError):
            raise NetworkError("Provider returned invalid JSON.") from None
        if not isinstance(result, dict):
            raise NetworkError("Provider returned malformed metadata.")
        return result

    def openalex_batch(self, dois, cancelled):
        """At most 50 exact identifiers per request; punctuation uses singleton lookup."""
        results = {doi: {"item": None, "errors": []} for doi in dois}
        ordinary = [doi for doi in dois if not any(c in doi for c in "|,+")]
        groups = [ordinary[i:i + 50] for i in range(0, len(ordinary), 50)]
        groups += [[doi] for doi in dois if doi not in ordinary]
        for group in groups:
            errors = []
            try:
                singleton = len(group) == 1
                payload = self.get(
                    "openalex",
                    "/works/" + quote("https://doi.org/" + group[0], safe="") if singleton else "/works",
                    {} if singleton else {"filter": "doi:" + "|".join(group), "per_page": 100,
                                           "select": "id,doi,title,publication_year,authorships,open_access,locations,best_oa_location,primary_location"},
                    cancelled, errors,
                )
                items = [payload] if singleton else payload.get("results")
                if not isinstance(items, list):
                    raise NetworkError("OpenAlex returned malformed results.")
                for item in items:
                    if not isinstance(item, dict):
                        raise NetworkError("OpenAlex returned a malformed record.")
                    try:
                        doi = canonical_doi(item.get("doi") or "")
                    except ValueError:
                        continue
                    if doi in group:
                        results[doi]["item"] = item
            except NetworkError as exc:
                if singleton and "HTTP 404" in str(exc):
                    continue  # OpenAlex has no record; not a provider failure.
                errors.append({"provider": "openalex", "stage": "metadata", "reason": str(exc)})
            for doi in group:
                results[doi]["errors"] = list(errors)
        return results

    def resolve(self, doi, oa, cancelled):
        errors = list(oa["errors"])
        metadata, provenance, locations, landings = {}, [], [], []
        summary = {"openalex_record": oa["item"] is not None, "checked": []}
        registries = ["datacite"] if doi.startswith("10.48550/") else ["crossref"]
        for provider in (*registries, "openalex", "unpaywall", "europepmc"):
            if cancelled():
                raise DiscoveryCancelled("Import cancelled.")
            if provider == "unpaywall" and not self.config.get("unpaywall_email"):
                continue
            summary["checked"].append(provider)
            try:
                if provider == "crossref":
                    try:
                        item = self.get(provider, "/works/" + quote(doi, safe=""), {}, cancelled, errors).get("message")
                    except NetworkError as exc:
                        if "HTTP 404" in str(exc):
                            # Not a Crossref DOI (e.g. DataCite-registered); ask the other public registry.
                            errors.append({"provider": provider, "stage": "registry",
                                           "reason": "Not registered with Crossref (HTTP 404); checked DataCite."})
                            registries.append("datacite")
                            summary["checked"].append("datacite")
                            self._datacite(doi, metadata, provenance, locations, cancelled, errors)
                            continue
                        raise
                elif provider == "datacite":
                    self._datacite(doi, metadata, provenance, locations, cancelled, errors)
                    continue
                elif provider == "openalex":
                    item = oa["item"]
                    if item is None:
                        continue
                    access = item.get("open_access") or {}
                    summary.update(openalex_is_oa=access.get("is_oa"), openalex_oa_status=access.get("oa_status"))
                    for location in item.get("locations") or []:
                        if (isinstance(location, dict) and location.get("is_oa") is True
                                and not str(location.get("pdf_url") or "").startswith("https://")
                                and str(location.get("landing_page_url") or "").startswith("https://")):
                            landings.append({"provider": provider, "kind": "landing",
                                             "landing_page_url": location["landing_page_url"],
                                             "host": ((location.get("source") or {}).get("display_name") or "")[:200],
                                             "license": location.get("license"), "version": location.get("version")})
                elif provider == "europepmc":
                    self._europepmc(doi, summary, locations, provenance, cancelled, errors)
                    continue
                else:
                    item = self.get(provider, "/v2/" + quote(doi, safe=""), {}, cancelled, errors)
                    if canonical_doi(item.get("doi") or "") != doi:
                        raise NetworkError("Unpaywall returned a different DOI.")
                    provenance.append({"provider": provider, "record_id": doi})
                    for location in item.get("oa_locations") or []:
                        if not isinstance(location, dict):
                            continue
                        if str(location.get("url_for_pdf") or "").startswith("https://"):
                            locations.append({
                                "provider": provider, "pdf_url": location["url_for_pdf"],
                                "license": location.get("license"), "version": location.get("version"),
                                "landing_page_url": location.get("url_for_landing_page"),
                            })
                        elif str(location.get("url_for_landing_page") or "").startswith("https://"):
                            landings.append({"provider": provider, "kind": "landing",
                                             "landing_page_url": location["url_for_landing_page"],
                                             "license": location.get("license"), "version": location.get("version")})
                    if not metadata.get("title") and item.get("title"):
                        metadata["title"] = str(item["title"])[:1000]
                    continue
                if not isinstance(item, dict):
                    raise NetworkError(f"{provider} returned malformed metadata.")
                if canonical_doi(item.get("DOI") if provider == "crossref" else item.get("doi") or "") != doi:
                    raise NetworkError(f"{provider} returned a different DOI.")
                paper = _paper(item, provider, "library-doi")
                if not paper:
                    raise NetworkError(f"{provider} returned no usable title.")
                for key in ("title", "authors", "year"):
                    if not metadata.get(key):
                        metadata[key] = paper.get(key)
                provenance.append({"provider": provider, "record_id": item.get("id") or doi,
                                   "url": public_url(paper.get("url") or "")})
                for record in paper["provenance"]:
                    for location in record.get("oa_locations", []):
                        locations.append({**location, "provider": provider})
            except (NetworkError, ValueError, TypeError, AttributeError, KeyError) as exc:
                reason = str(exc) if isinstance(exc, NetworkError) else f"{provider} returned malformed metadata."
                errors.append({"provider": provider, "stage": "metadata", "reason": reason})
        unique = {}
        for location in [*locations, *landings]:
            unique.setdefault(location.get("pdf_url") or location["landing_page_url"], location)
        return {"metadata": metadata, "provenance": provenance, "locations": list(unique.values())[:12],
                "errors": errors, "oa": summary}

    def _datacite(self, doi, metadata, provenance, locations, cancelled, errors):
        data = self.get("datacite", "/dois/" + quote(doi, safe=""), {}, cancelled, errors).get("data") or {}
        attributes = data.get("attributes") or {}
        if canonical_doi(attributes.get("doi") or "") != doi:
            raise NetworkError("DataCite returned a different DOI.")
        titles = [t.get("title") for t in attributes.get("titles") or [] if isinstance(t, dict) and t.get("title")]
        if titles and not metadata.get("title"):
            metadata["title"] = str(titles[0])[:1000]
        if not metadata.get("authors"):
            metadata["authors"] = [str(c.get("name"))[:300] for c in attributes.get("creators") or [] if isinstance(c, dict) and c.get("name")][:50]
        if not metadata.get("year") and str(attributes.get("publicationYear") or "").isdigit():
            metadata["year"] = int(attributes["publicationYear"])
        rights = [r.get("rightsUri") for r in attributes.get("rightsList") or [] if isinstance(r, dict) and r.get("rightsUri")]
        provenance.append({"provider": "datacite", "record_id": doi, "url": public_url(str(attributes.get("url") or ""))})
        match = ARXIV.fullmatch(doi)
        if match:
            locations.append({"provider": "arxiv", "pdf_url": f"https://export.arxiv.org/pdf/{match.group(1)}",
                              "landing_page_url": f"https://arxiv.org/abs/{match.group(1)}",
                              "version": "submittedVersion", "license": rights[0] if rights else None})

    def _europepmc(self, doi, summary, locations, provenance, cancelled, errors):
        payload = self.get("europepmc", "/search", {"query": f'DOI:"{doi}"', "resultType": "core",
                                                    "format": "json", "pageSize": 5}, cancelled, errors)
        results = (payload.get("resultList") or {}).get("result") or []
        record = next((r for r in results if isinstance(r, dict) and str(r.get("doi") or "").lower() == doi), None)
        if not record:
            summary["europepmc"] = {"record": False}
            return
        pmcid = record.get("pmcid") if re.fullmatch(r"PMC\d{1,10}", str(record.get("pmcid") or "")) else None
        codes = {f.get("availabilityCode") for f in (record.get("fullTextUrlList") or {}).get("fullTextUrl") or []
                 if isinstance(f, dict) and f.get("site") in {"Europe_PMC", "PubMedCentral"}}
        summary["europepmc"] = {"record": True, "pmcid": pmcid, "open_access_subset": record.get("isOpenAccess") == "Y",
                                "free_fulltext": bool(codes & {"OA", "F"}), "license": record.get("license")}
        provenance.append({"provider": "europepmc", "record_id": record.get("id"), "pmcid": pmcid})
        if pmcid and record.get("isOpenAccess") == "Y":
            locations.append({"provider": "europepmc", "kind": "jats",
                              "pdf_url": f"{BASES['europepmc']}/{pmcid}/fullTextXML",
                              "landing_page_url": f"https://europepmc.org/article/PMC/{pmcid}",
                              "license": record.get("license"), "version": "PMC open-access subset"})
