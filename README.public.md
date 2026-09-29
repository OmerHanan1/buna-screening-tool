# Paper Overlap Detector

A scientific-paper **text-overlap screening tool for human review**, not a
plagiarism verdict or a reproduction of Crossref/Turnitin's proprietary service.

The hosted site now requires the **explicitly approved owner's Microsoft sign-in**.
The static login page is public; library, uploads, comparisons and reports are
authorized by the backend. See [`docs/team-deployment.md`](docs/team-deployment.md).
Sign-in does not expand paper-use rights: the initial protected release retains
12 reviewed sources, not all files in the separate local library.

The hosted service uses a reviewed paper library and optional
personal comparison uploads. It produces real PDFs with original manuscript
pages, source-numbered highlights and source-passage comments, plus JSON evidence.
New comparisons begin at a recognized Abstract heading; otherwise a visible
warning explains the whole-manuscript fallback.

Hosted uploads are isolated by validated Microsoft identity, expire within one
hour, and can disappear sooner on restart/scale-down. Download results promptly.
Do not upload sensitive or confidential manuscripts. This preview does **not**
offer DOI import, a persistent visitor library or permanent comparison history.
Comparison source files have no download/full-text endpoint; reports preserve
complete matched passages without adding the full source documents.

## Code and deployment

* `buna/public_app.py`: isolated anonymous public API; it never serves the local store.
* `buna/public_worker.py`: per-job Linux isolation and comparison/PDF generation.
* `buna/public_seed.py`: explicit reviewed-hash corpus build, separate from private data.
* `frontend/src/PublicApp.tsx`: public frontend; deployed separately on GitHub Pages.
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
Do not expose that local API to the internet. The public service requires its
Linux container, a reviewed public corpus and an exact HTTPS frontend origin.

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
