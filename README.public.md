# Paper Overlap Detector

A scientific-paper **text-overlap screening tool for human review**, not a
plagiarism verdict or a reproduction of Crossref/Turnitin's proprietary service.

The hosted site uses a **simple email allowlist gate**, as explicitly requested.
Open it at **https://kind-field-035b3910f.4.azurestaticapps.net/**.
Email ownership is not verified: anyone who knows an allowed email can enter.
The backend checks the entry and issues a separate random visitor capability;
typing the same email does not grant access to another visitor's jobs or reports.
See [`docs/email-gate.md`](docs/email-gate.md).

All 44 verified source documents are available for server-side comparison on the
user's explicit hosted-use authorization. This is **not** a claim that all files
are cleared for public redistribution. Originals remain in private backend
storage, never in public frontend assets or source-file download routes.

The hosted service uses a reviewed paper library and optional
personal comparison uploads. It produces real PDFs with original manuscript
pages, source-numbered highlights and source-passage comments, plus JSON evidence.
New comparisons begin at a recognized Abstract heading; otherwise a visible
warning explains the whole-manuscript fallback.

Hosted uploads are isolated by independent random visitor capabilities, expire within one
hour, and can disappear sooner on restart/scale-down. Download results promptly.
Do not upload sensitive or confidential manuscripts. This preview does **not**
offer DOI import or permanent private comparison history.
Comparison source files have no download/full-text endpoint; reports preserve
complete matched passages without adding the full source documents.

Additional comparison PDFs can optionally be kept in the durable shared library.
The **Keep in library for future comparisons** checkbox is off by default and
never applies to the manuscript. Saved papers are available to everyone with app
access; this is not a private personal library. See [shared storage and limits](docs/shared-library.md).

The hosted workspace starts with all available default papers selected. Use
**Review papers** to search, deselect or inspect credits, then upload a manuscript
and choose **Compare papers**. Optional comparison uploads stay separate from the
default library. Results offer **Open PDF** and **Download PDF**; methodology,
privacy details and source credits are kept in secondary disclosures. File
validation, incomplete results and expired-session errors remain visible.

An optional **Advanced → Exact + similar wording (experimental)** model separates
contiguous exact wording from bounded edits/reordering. Experimental PDFs use
rose **E** and amber **S** markers with source-numbered annotation titles; color
is not the only cue. The standard model remains the default. Scores and
normalization can differ, and no superiority or vendor-equivalence claim is made.
See [the model and limitations](docs/experimental-wording.md).

## Code and deployment

* `buna/public_app.py`: isolated anonymous public API; it never serves the local store.
* `buna/public_worker.py`: per-job Linux isolation and comparison/PDF generation.
* `buna/public_seed.py`: explicit reviewed-hash corpus build, separate from private data.
* `frontend/src/PublicApp.tsx`: public frontend; deployed separately on Azure Static Web Apps Free.
* [`docs/public-deployment.md`](docs/public-deployment.md): boundaries, budgets,
  operation, retention, build and deployment instructions.

Python 3.11+ and Node 22+:

```sh
python -m venv .venv
.venv/bin/pip install -e '.[test]'
cd frontend && npm ci && npm test && cd ..
.venv/bin/python -m pytest tests/test_public_app.py
```

The local single-user app can be started with `.venv/bin/python -m buna`.
Do not expose that local API to the internet. The hosted service requires its Linux container, an exact-hash reviewed corpus
permission manifest and an exact HTTPS frontend origin.

## Licensing

This application's source is provided under **GNU AGPL version 3 or later**;
see [`LICENSE`](LICENSE). PyMuPDF is used under its AGPL option, not a purchased
commercial license. Corresponding application source, build recipe and dependency
manifests are available in this repository; the public interface links here.
Dependencies retain their own licenses. No license is granted here for held
private papers, user uploads or third-party material.

The curated papers are **separately licensed**. Their exact-file license,
attribution, source/version and conversion notices are shown in the public library,
downloadable attribution metadata and relevant PDF-report credits. They are not
relicensed under the application's AGPL. No publisher/author endorsement is implied.
