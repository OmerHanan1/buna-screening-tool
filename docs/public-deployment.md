# Public preview deployment

The public preview is a **separate service**, not the localhost app behind a tunnel.
It supports a reviewed public corpus, personal PDF/TXT uploads and PDF/JSON reports.
It intentionally has no DOI importing or persistent personal history. Supplementary
PDFs can optionally be kept in the bounded private [shared library](shared-library.md);
the checkbox is unchecked by default and never applies to the manuscript.
The local application and its data are never mounted into the public service.

## Isolation and retention

The email-gated gateway issues 256-bit visitor bearer capabilities kept only in
page memory. All job/status/download/delete routes enforce ownership. No global
job listing or curation API is exposed. The corpus is immutable and separate.
Use the exact HTTPS frontend origin in `BUNA_PUBLIC_ORIGIN`; cross-origin credentials
and wildcard origins are not used.

Each job has its own unprivileged Linux UID, private files and a single-process
worker. UIDs are not recycled during a replica's lifetime. Seccomp prevents network
sockets, process creation/execution, namespace/session changes and privileged
introspection. Every replica tests those denials before starting HTTP; unsupported
kernels fail closed. The worker has 720 CPU seconds, 780 elapsed seconds, a 1.5 GiB
address-space limit and bounded file/process counts. The gateway admits one job
at a time and monitors temporary storage. Results are copied into gateway-owned
storage using no-follow, regular-file and size checks.

The comparison stage has its own 480-second bound so partial evidence can be
rendered before the outer worker limits. Source extraction caches are immutable,
content/parser-version pinned for the curated corpus and validated shared sources.

This reduces risk; it is not a promise that untrusted document parsing is perfectly
safe. Do not submit confidential or sensitive manuscripts. Visitor files/results
last **up to one hour** and may disappear sooner during scale-down or restart.
Different replicas/revisions have separate ephemeral stores; there is no shared
SQLite database or cross-revision UID/file namespace. Losing the session capability
means losing access. Download reports promptly.

Admission limits: 10 comparisons per rolling day per replica, three per session,
200 live sessions, one active worker, 32 MiB request envelope, 10 MiB/250,000-character
manuscripts and at most 50 personal 8 MiB sources, privately staged one at a time
within a 128 MiB batch budget (the multipart request cap remains 32 MiB).
Limits reset if a replica
restarts. Rate/quotas and one replica reduce abuse but are **not a billing cap**.
No external DOI, AI or discovery requests occur in the public worker.

## Reviewed corpus

The current 44-document private hosted corpus follows the user-attested permission
policy in [email-gate.md](email-gate.md), not a public redistribution license.
The following seed procedure applies only to separately reviewed public corpora.

`python -m buna.public_seed --audit REVIEWED_JSON --expected-sha256 REVIEW_SHA
--staging VERIFIED_FILES --destination release-context/public-corpus`

This command never opens a local jobs database. It accepts only the reviewed
allowlist and exact source hashes, rejects NC/ND or unapproved licenses, and
preserves full attribution/modification/copyright notices. Repeating the exact
seed is idempotent; replacing its contents requires a new release directory.
Public library UI, downloadable credit metadata, evidence JSON and PDF attribution
appendices include the required credits (including anthology chapter credits).
The source files and build context are ignored by Git. Do not commit the audit's
private paths, held files, user uploads or the local corpus.

## Infrastructure and cost

Use only the explicitly authorized personal subscription; every Azure command must
include `--subscription`. Never change the global active account.

The initial design uses East US Container Apps **Consumption**, 1 vCPU / 2 GiB,
minimum zero / maximum one replica, no Log Analytics workspace, no dedicated
profile/private endpoint, and one Basic private container registry. Curated files
persist in the immutable image; visitor data is ephemeral. ACR pull uses a
dedicated identity scoped to that registry. Optional shared saving additionally
grants that identity Blob Data Contributor on its single private source container,
not on the subscription or storage account.

Official retail rates checked for East US:

* CPU active: $0.000024/vCPU-second; memory active: $0.000003/GiB-second.
* 1 vCPU / 2 GiB: $0.108/active hour, or $10.80 for **100 active hours**.
* Basic ACR: $0.1666/day, approximately $5.17 in a 31-day month.
* Add a $5 storage/transfer/usage allowance: approximately **$20.97/month**
  for that workload assumption, without relying on credits/free grants.

Sources: [Container Apps pricing](https://azure.microsoft.com/pricing/details/container-apps/),
[Azure retail prices API](https://prices.azure.com/api/retail/prices),
[Container Registry pricing](https://azure.microsoft.com/pricing/details/container-registry/).
Images are built locally; no paid ACR build is required. Scale-to-zero consumes no
compute, but sustained traffic can exceed the estimate. Budget notifications at
$20/$25 are alerts, not enforcement. Ask before increasing the approved baseline.

The frontend uses **Azure Static Web Apps Free** (`paper-overlap-web`, East US 2)
at **https://kind-field-035b3910f.4.azurestaticapps.net/**, in the same dedicated
resource group. It adds $0 hosting cost within the Free quotas: 100 GB monthly
bandwidth per subscription, 250 MB per environment, 500 MB total storage per app,
and 15,000 files. Free has no SLA or paid bandwidth overage; quota exhaustion can
affect availability. No custom domain, Functions, linked backend, or Front Door
is needed. The browser calls the existing Container App directly; integrated
Container Apps backend linking would require paid Standard and must not be enabled.
See [plans](https://learn.microsoft.com/azure/static-web-apps/plans),
[quotas](https://learn.microsoft.com/azure/static-web-apps/quotas), and
[backend integration](https://learn.microsoft.com/azure/static-web-apps/apis-container-apps).

`deploy/public.bicep` defaults to **internal ingress**. Deploy that first and verify
the actual Azure amd64 kernel and complete synthetic upload-to-PDF workflow.
Only after the security and corpus gates pass should `publicIngress` become true.
Keep revision mode Single, maximum one replica and all worker safeguards.
For bounded release validation only, Multiple mode may pin all public traffic to
the proven revision while a zero-traffic candidate is tested internally. Restore
Single/minimum-zero operation after checking active work and completing rollout;
do not leave a warm validation replica running.

## Release and operations

1. Build a clean allowlisted image context (`.dockerignore`), never `.buna-data`.
2. Download Linux wheels over verified TLS for the offline container installation.
   Use `pip download --require-hashes -r deploy/requirements-linux.lock
   --only-binary=:all: --platform manylinux2014_x86_64 --python-version 313
   --implementation cp --abi cp313 --dest release-context/wheels`.
3. Build `deploy/Dockerfile` for linux/amd64 and pin the resulting image digest.
4. Provision the dedicated registry/environment/application; configure budget alerts.
5. Run `python deploy/smoke_public.py` inside the private app to validate ownership,
   all approved sources, real PDF output, credits and deletion.
6. After approval, enable HTTPS public ingress. Build and deploy only the frontend
   as described below. Keep local frontend assets separate.
7. Repeat the smoke externally, then test the Azure browser flow.

### Static frontend release

Use a reviewed source commit matching the API. From the repository root:

```sh
npm --prefix frontend ci
VITE_PUBLIC_MODE=true VITE_EMAIL_GATE=true VITE_BASE_PATH=/ \
VITE_PUBLIC_API_URL=https://paper-overlap-api.purpleflower-beedf5ce.eastus.azurecontainerapps.io \
npm --prefix frontend run build -- --outDir ../public-dist --emptyOutDir
cp LICENSE NOTICE public-dist/
```

`public-dist` must contain only `index.html`, `theme-init.js`,
`staticwebapp.config.json`, `LICENSE`, `NOTICE`, and hashed JS/CSS under `assets/`.
Inspect the complete file list and size before uploading. Never upload the
repository root, private corpus, extraction caches, release context, visitor data,
or container image. The source-code link and license/notice files must remain.

`frontend/public/staticwebapp.config.json` sets security headers, immutable caching
for hashed assets, and a SPA fallback excluding API/assets and missing file paths.
The synchronous external theme script avoids requiring inline script execution or
`unsafe-eval`. `connect-src` permits only the existing HTTPS API. Test native
blob-URL PDF tabs/downloads whenever changing CSP; do not weaken API headers.

Deploy with the supported SWA CLI, independently of GitHub Actions:

```sh
npm exec --yes --package @azure/static-web-apps-cli@2.0.10 -- swa deploy public-dist \
  --env production --subscription-id d7d06293-4bf0-48fd-9ae5-11bbe4566248 \
  --resource-group rg-paper-overlap-public --app-name paper-overlap-web \
  --no-use-keychain
```

Pass the deployment token privately through `SWA_CLI_DEPLOYMENT_TOKEN`, obtained
from the same explicitly scoped SWA resource. Never print it, place it in source,
pass it as a command-line argument, or use a `VITE_*` variable for any secret.
No GitHub workflow permission or Microsoft sign-in callback change is required.

### Origin cutover and rollback

Only one operator may deploy/update the backend at a time. Wait for running jobs
to finish and allow reports to be downloaded before changing the live revision.
First upload and verify static assets, then set `BUNA_PUBLIC_ORIGIN` to exactly
`https://kind-field-035b3910f.4.azurestaticapps.net` (no path or trailing slash).
Keep `BUNA_PUBLIC_HOSTS` restricted to API hosts, not the frontend hostname.
The single origin controls both CORS and the explicit Origin rejection middleware.
Keep credentials disabled, preserve the email gate and capability ownership, and
never use wildcard CORS. The existing Microsoft registration stays unused.

The frontend shell is static, but entry readiness still waits for the scale-to-zero
API to wake up. Connection failures must show a retry action. New hosts/revisions
do not migrate in-memory visitor capabilities or temporary jobs.

Completed reports are fetched and validated once into page memory before PDF
actions are enabled. Open/Download are then native blob links activated directly
by the user's click, not a network-dependent popup or detached download click.
PDF transfer has a 60-second timeout, byte progress, stop and retry controls;
retry never starts a new comparison. Payload type/signature and size are checked.
Browser PDF viewer support still varies; downloading and opening locally remains
available. A static frontend update does not replace JavaScript already loaded
in an existing tab: do not tell visitors to refresh an undownloaded result without
warning that their memory-only capability would be lost.

Run frontend unit and hosted browser checks, then the opt-in actual flow with an
authorized email supplied privately through `HOSTED_TEST_EMAIL`:

```sh
cd frontend
npm test
npx playwright test --config playwright.hosted.config.ts --grep-invert 'opt-in actual'
HOSTED_REALISTIC=true npx playwright test --config playwright.hosted.config.ts \
  --grep 'opt-in actual'
```

The live check generates an original 45-page synthetic PDF using the repository
`.venv` PyMuPDF dependency. It compares the 44 defaults plus one synthetic source,
checks deselection persistence, visitor isolation, origin/source-route denials,
real PDF opening/download, desktop/mobile layout, and deletion. It never needs
a real user manuscript. `HOSTED_SITE_URL` overrides the target URL when needed.

Keep old Pages unchanged until the new live flow passes. Afterwards, its
`gh-pages` branch can serve a moved notice linking to the Azure site (normal
fast-forward commit, never force-push). Old Pages is no longer an allowed API
origin. Rollback requires restoring the previous frontend artifact and exact
Pages origin in a coordinated backend update; it cannot restore ephemeral jobs.

To stop spending, delete **only** the dedicated public resource group after
confirming its identity; this removes the public app, registry and identity.
The group also contains the Free static frontend; disable the legacy Pages
notice/site separately. No local data should be affected.
Do not weaken isolation when a runtime check fails—keep ingress private and fix
the deployment or report the blocker.
