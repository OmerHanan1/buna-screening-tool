# Optional shared comparison-paper library

**Keep in library for future comparisons** appears only beside additional PDF
comparison uploads and is unchecked by default. It never applies to the main
manuscript. The uploader must confirm authorization for shared hosted processing
and matching excerpts. Saved papers are available as comparison sources to
everyone with app access—including anyone who knows an allowed email, because
the email gate does not verify identity.

Manuscripts, reports and unselected comparison uploads remain temporary. Opted-in
sources persist privately across sessions, restarts and scale-down. There is no
original-source/full-text download route or public shared-library deletion API.
No public redistribution license or DOI identity is inferred from an upload.

## Readiness, deduplication and snapshots

Only a complete file accepted by the isolated PDF parser is eligible. Empty,
unreadable/scanned-only, identifiable abstract-only or inconsistent/truncated
extractions are rejected. Heading-less documents cannot be semantically proven
to contain a whole scientific article; extraction warnings and page limitations
remain visible. Exact manuscript-byte and normalized-text copies are not saved.

The original SHA256 is the source identity. Original PDF and canonical parsed
cache are immutable blobs. An identical upload returns “Already in the library”;
its title/version is not replaced. Curated sources remain in the separate,
immutable 44-file image and cannot be overwritten by shared uploads. No automatic
DOI merge is performed.

Successful saves affect **future comparison snapshots only**. The current job's
selected files, existing deselections, source IDs and reports do not change.
Starting a new comparison refreshes the library, keeps deselections for existing
members, and selects newly ready members. Another visitor gets the same durable
shared sources, not access to the uploader's private job or manuscript.

## Storage and limits

The service uses a private Azure StorageV2 / Hot LRS container. Blob public access
and shared-key access are disabled; the service's managed identity has
`Storage Blob Data Contributor` only on this container. No SAS URL is generated.
The gateway—not the unprivileged no-network document worker—talks to storage.

Configuration (all three values are required together):

* `BUNA_SHARED_ACCOUNT_URL`: exact Azure Blob HTTPS account endpoint.
* `BUNA_SHARED_CONTAINER`: private container name.
* `BUNA_SHARED_IDENTITY_CLIENT_ID`: existing user-assigned app identity client ID.

Initial shared limits are **50 files**, **512 MiB total original + parsed bytes**,
and **20 new files per rolling day**. Each additional original PDF remains limited
to 8 MiB; each parsed cache to 32 MiB. A comparison may materialize at most
192 MiB of shared parsed caches. Existing manuscript, parser, comparison and
worker time/memory limits still apply; a larger selected corpus can be partial.

`catalog-v1.json` uses conditional ETag writes. A counted pending reservation is
committed before blob uploads; only after both immutable objects exist is the
entry published as ready. Concurrent replicas cannot lose catalog updates or
exceed admitted quotas through last-writer-wins updates. Failed reservations
remain counted, bounding orphan storage; an identical upload or an authorized
retry resumes the save. Operator review is needed to reclaim abandoned
reservations or change immutable versions. There is no silent unbounded cleanup.

## Failure and cancellation behavior

The comparison report becomes available independently of durable saving.
“Saving”, “Saved”, “Already in the library”, “Not saved” and failure reasons are
shown separately. A failed/uncertain save never displays success. **Retry library
save** retries persistence only; it does not rerun the comparison.

Cancellation and entry into publication use the same lifecycle lock. If
cancellation wins, pending sharing is cancelled. If comparison has already
finished and publication started, deletion returns a clear “saving is finishing”
conflict rather than acknowledging a cancellation that cannot be honored.

If shared storage is unavailable, the UI explicitly says it is showing only the
curated library and disables new save selections. A save requested before an
outage reports its independent failure while leaving the comparison usable.
Missing/damaged selected shared cache files are recorded as unexamined sources,
not counted as fully screened.

## Cost and operations

East US General Block Blob v2 Hot LRS retail prices checked on 2026-09-29:
$0.0208/GB-month, $0.05/10,000 writes, and $0.004/10,000 reads. For example,
1 GB + 10,000 writes + 100,000 reads is about **$0.1108/month** for storage and
operations, excluding transfer and compute. This fits the existing $5 allowance
in the approximate $21-at-100-active-hours baseline; it is not a hard spending cap.
The existing $20/$25 budget alerts and min-zero/max-one compute profile remain.

Do not copy local SQLite data or visitor history into this store. Do not enable
anonymous blob access, issue browser SAS links, or put originals in Git/Pages.
Validate persistence by saving an original synthetic source, restarting the
service, and comparing from an independent visitor session. Clean up only the
explicit synthetic test object/catalog entry through operator-scoped maintenance;
never delete user sources as part of a test.
