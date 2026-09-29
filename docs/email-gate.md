# Email access gate and private hosted corpus

This is deliberately **not identity authentication**. The user explicitly chose
an email allowlist without Microsoft sign-in, password, OTP or magic link.
Anyone who knows an allowed email can pass the gate. Do not describe it as
verified ownership, secure team sign-in or a substitute for paper-use permission.

## Visitor privacy

`BUNA_ACCESS_MODE=email-gate` requires one `BUNA_ALLOWED_EMAIL` server setting.
The gate trims/case-normalizes the submitted email, applies basic format/length
checks, and returns a generic rejection for entries not on the allowlist.
Attempts are rate-limited. Submitted emails are not saved with sessions/jobs.

Each accepted request mints a new cryptographically random visitor capability.
The browser keeps it only in page memory; the server stores its hash. The email
is **never the job-owner key**. Two visitors entering the same email get distinct
workspaces and cannot read/cancel/delete each other's jobs or reports.
Microsoft-issued tokens and old identity-owned jobs are not converted or adopted.
Refreshing/leaving the page loses its capability; download reports first.
Files/results remain temporary for up to an hour and may disappear sooner on
restart or scale-down. The existing Microsoft registration remains unused.

All library and job/report operations require the capability. Source originals,
full-source text, curation, import administration and global job lists have no
visitor endpoints. Full matched passages remain in reports; matching scores and
canonical evidence counts are unchanged. No arbitrary excerpt-length cutoff is
introduced. The same Linux worker isolation, input limits, one-worker admission,
memory/CPU/wall-time safeguards and scale-to-zero budget remain.

## User-attested hosted use

On 2026-09-29 the user explicitly confirmed authorization to store/process the
44 verified documents on Azure and show matching excerpts in reports.
`private-hosted-processing` manifests record this exact scope and the source
hashes, versions and provenance. Per-file `hosted_processing_basis` is
`user-attested-hosted-use`, **not a newly invented CC license**. Existing CC
attribution/copyright/conversion notices are retained. Other files retain their
actual known rights status; no public redistribution claim is added.

`BUNA_ATTESTED_CORPUS_SHA` pins the private manifest. The loader fails closed
unless the email-gate policy and explicit attestation are present. It checks
every file hash, a 50-file/160-MiB aggregate limit and existing per-file bounds.
This manifest is distinct from the identity-bound private-team permission model.
The attestation covers 44 ready files only, not missing papers or excluded
sources. The allowlist email itself is not a permission grant.

Private files are copied only into an ignored, explicit release build context
and the **private** Azure registry image. Never publish that image publicly,
enable registry admin access, put originals in source archives/Pages, or migrate
the local SQLite database and user histories. The service pull identity has only
registry-pull permissions. The static frontend receives metadata and reports,
not original comparison files.

## Verification and rollout

Run unit tests for normalization/rejection, attempt limits, token expiry, two
same-email sessions, cross-visitor 404s and admin/source-route denial.
Use `BUNA_TEST_EMAIL=... python deploy/smoke_public.py --email-gate --base URL`
for an authorized synthetic smoke; it never prints the email or capabilities.
The smoke selects all available corpus members, adds one synthetic matching
source, checks full candidate traversal, downloads a real PDF and deletes the job.
Ready means parsed—not a promise that every future manuscript can be fully
compared inside the unchanged processing budget.

Check active jobs before changing a live revision, preserve the local 44-source
library and prior preferences, and leave Azure at minimum zero / maximum one
replica. Budget notifications are not hard spending caps.
