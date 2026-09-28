# Public preview deployment

The public preview is a **separate service**, not the localhost app behind a tunnel.
It supports a reviewed public corpus, personal PDF/TXT uploads and PDF/JSON reports.
It intentionally has no DOI importing, shared visitor library or persistent personal history.
The local application and its data are never mounted into the public service.

## Isolation and retention

The gateway issues 256-bit anonymous bearer capabilities stored in the browser's
session storage. All job/status/download/delete routes enforce ownership. No global
job listing or curation API is exposed. The corpus is immutable and separate.
Use the exact HTTPS Pages origin in `BUNA_PUBLIC_ORIGIN`; cross-origin credentials
and wildcard origins are not used.

Each job has its own unprivileged Linux UID, private files and a single-process
worker. UIDs are not recycled during a replica's lifetime. Seccomp prevents network
sockets, process creation/execution, namespace/session changes and privileged
introspection. Every replica tests those denials before starting HTTP; unsupported
kernels fail closed. The worker has 180 CPU seconds, 240 elapsed seconds, a 1.5 GiB
address-space limit and bounded file/process counts. The gateway admits one job
at a time and monitors temporary storage. Results are copied into gateway-owned
storage using no-follow, regular-file and size checks.

This reduces risk; it is not a promise that untrusted document parsing is perfectly
safe. Do not submit confidential or sensitive manuscripts. Visitor files/results
last **up to one hour** and may disappear sooner during scale-down or restart.
Different replicas/revisions have separate ephemeral stores; there is no shared
SQLite database or cross-revision UID/file namespace. Losing the session capability
means losing access. Download reports promptly.

Admission limits: 10 comparisons per rolling day per replica, three per session,
200 live sessions, one active worker, 32 MiB request envelope, 10 MiB/250,000-character
manuscripts and at most five personal 8 MiB sources. Limits reset if a replica
restarts. Rate/quotas and one replica reduce abuse but are **not a billing cap**.
No external DOI, AI or discovery requests occur in the public worker.

## Reviewed corpus

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
dedicated identity scoped only to that registry.

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

`deploy/public.bicep` defaults to **internal ingress**. Deploy that first and verify
the actual Azure amd64 kernel and complete synthetic upload-to-PDF workflow.
Only after the security and corpus gates pass should `publicIngress` become true.
Keep revision mode Single, maximum one replica and all worker safeguards.

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
6. After approval, enable HTTPS public ingress. Set GitHub repository variable
   `PUBLIC_API_URL` and enable Pages with GitHub Actions. The workflow builds the
   `/buna-screening-tool/` frontend independently of local assets.
7. Repeat the smoke externally, then test the Pages browser flow.

To stop spending, delete **only** the dedicated public resource group after
confirming its identity; this removes the public app, registry and identity.
Disable the Pages workflow/site separately. No local data should be affected.
Do not weaken isolation when a runtime check fails—keep ingress private and fix
the deployment or report the blocker.
