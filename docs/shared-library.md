# Optional shared comparison-paper library

**Keep in library for future comparisons** appears only beside additional PDF
comparison uploads and is unchecked by default. It never applies to the main
manuscript. The uploader must confirm authorization for shared hosted processing
and matching excerpts. Saved papers are available as comparison sources to
everyone with app access—including anyone who knows an allowed email, because
the email gate does not verify identity.

**Checking the box starts saving immediately. No manuscript or comparison is
required.** The file is uploaded, queued for the isolated parser, validated and
committed to private Blob storage and the catalog. The UI says **Saved to shared
library** only after the durable acknowledgement and an authoritative library
refresh confirm visibility. The source is then searchable in Review papers.
Unchecked files are not shared. Removing or unchecking an already saved upload
only affects the current workspace; it never deletes the shared source.

Manuscripts, reports and unselected comparison uploads remain temporary. Opted-in
sources persist privately across sessions, restarts and scale-down unless
explicitly removed from the shared library. There is no original-source/full-text
download route.
No public redistribution license or DOI identity is inferred from an upload.

## Removing a paper versus deselecting it

**Deselecting** changes only the current comparison. **Remove from library** in
Review papers is a separate, confirmed action affecting **all app users**. The
confirmation names the paper, explains global impact, and initially focuses
Cancel. Any visitor admitted through the existing email/capability gate can
confirm removal; this is not a verified-owner/admin restriction.

Both bundled and user-saved papers can be removed from future library lists and
selections. Bundled papers get a durable removal record; their private packaged
bytes remain in the backend image. Do not describe that as physical erasure.
User-saved papers are hidden immediately and their exact old-version blobs are
retired for at least two hours, then deleted when the service is running and
storage is reachable. Temporary comparison snapshots and existing PDF reports
are unchanged. Retired bytes still count against the storage quota until deletion
is confirmed; deleting never refunds the daily new-paper allowance.

Catalog changes use ETag compare-and-swap and immutable removal generations.
Already admitted old saves cannot republish a removed generation. A later
explicit upload can add the same content again under the new generation; retries
of an earlier removal cannot delete that newly added version. Stale comparison
selections are rejected for refresh rather than silently substituted.

Existing catalogs are read without a destructive migration. The additive format's
first removal creates one
private, immutable `catalog-before-removals-v1.json` metadata backup, not a backup
of erased source files. A bounded removal ledger stores source hash, generation,
time and kind, without email, capability or document text. Library reads fail
closed during catalog outages rather than resurrecting hidden bundled papers.
After the first removal, do not roll back to a release that ignores removal
generations: a rollback must preserve the removal-aware list, admission and save
guards even though older software can parse the underlying JSON.

## Readiness, deduplication and snapshots

Only a complete file accepted by the isolated PDF parser is eligible. Empty,
unreadable/scanned-only, identifiable abstract-only or inconsistent/truncated
extractions are rejected. Heading-less documents cannot be semantically proven
to contain a whole scientific article; extraction warnings and page limitations
remain visible. The browser rejects a byte-identical copy of its currently
selected manuscript. Standalone source saving has no manuscript to compare
against; it cannot determine whether an explicitly selected comparison PDF is
actually someone's manuscript. Legacy comparison-bound requests also reject
byte-identical and normalized-text-identical manuscript copies.

The original SHA256 is the source identity. Original PDF and canonical parsed
cache are immutable blobs. An identical upload returns “Already in the library”;
its title/version is not replaced. Curated sources remain in the separate,
immutable 44-file image and cannot be overwritten by shared uploads. No automatic
DOI merge is performed.

Successful saves refresh the setup library without resetting existing opt-outs.
Already saved manual files are selected by their immutable ID, not uploaded or
counted twice. Saves affect **future comparison snapshots only**. The current job's
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

Shared limits are **100 user-uploaded files** (curated papers are separate),
**512 MiB total original + parsed bytes**, and **50 new files per rolling day**.
`BUNA_SHARED_MAX_PAPERS` and `BUNA_SHARED_MAX_NEW_PER_DAY` can lower the count
and daily limits; startup rejects settings above the approved 100/50 ceilings.
The setup page shows remaining count, daily admissions and bytes; these are
informational snapshots, with authoritative conditional checks at publication.
Each additional original PDF remains limited
to 8 MiB; each parsed cache to 32 MiB. A comparison may materialize at most
192 MiB of shared parsed caches. Existing manuscript, parser, comparison and
worker time/memory limits still apply; a larger selected corpus can be partial.

Choose or drop **up to 50 additional files**, appending subsequent selections.
The private upload queue sends **one 8 MiB-maximum file per request**, with a
**128 MiB per-workspace original-byte budget** (in-flight uploads reserve 8 MiB).
This is not a 400 MiB multipart request: the 32 MiB request guard remains.
`POST /source-uploads` returns owner-bound, one-hour IDs. One comparison attaches
all selected IDs; server-owned originals are copied into an immutable job
snapshot and content hashes deduplicate sources, never filenames. Different
contents with the same filename remain distinct. Removing an upload deletes only
that visitor's temporary original; existing job snapshots and shared papers stay.
Private uploads are never published without a separate explicit save action.
Queued uploads can be removed, failed uploads retried, and comparison remains
disabled until every remaining upload is ready. Extraction failures and runtime
limits still appear in source coverage, rather than silently dropping sources.

Shared saves also run in a sequential browser queue. **Keep all selected PDFs in
shared library** is an explicit sharing action; individual checkboxes remain
unchecked by default. Wait for each durable, library-visible acknowledgement.
Daily and storage limits may reject part of a batch with per-file reasons.

`catalog-v1.json` uses conditional ETag writes. A counted pending reservation is
committed before blob uploads; only after both immutable objects exist is the
entry published as ready. Concurrent replicas cannot lose catalog updates or
exceed admitted quotas through last-writer-wins updates. Failed reservations
remain counted, bounding orphan storage; an identical upload or an authorized
retry resumes the save. Operator review is needed to reclaim abandoned
reservations or change immutable versions. There is no silent unbounded cleanup.

Pending object writes have an exclusive durable writer owner. Duplicate writers
cannot take over on a timer: that could allow a paused writer to recreate old
blobs after cleanup. Normal failures release ownership for retry. If a process is
terminated mid-write, an operator must verify that writer has permanently stopped
before releasing its claim; unfinished bytes remain quota-accounted meanwhile.

## Failure and cancellation behavior

Saving uses the same single resource slot as comparison; up to five source saves
can wait without launching competing parsers. Source parsing has a 35-second
timer, 40 CPU seconds (45 hard), 45-second supervisor deadline and 96 MiB temporary
file bound, with the same UID/seccomp/address-space isolation. Admission/retries
are bounded to **150 per rolling day per replica**, with at most three attempts
per receipt. All standalone parsing shares a **2,250-second daily wall-time
budget**: reserve 45 seconds before each isolated parse, then return unused time.
Duplicate catalog content skips parsing and does not use new-paper quota.
Persistent catalog quotas still apply across replicas/restarts. Browser rate-limit
retries honor `Retry-After`, retry at most twice, and do not automatically retry
daily limits. The service still admits ten comparisons/day, three/visitor.

The 768 MiB runtime and 256 MiB worker-directory guards remain. Working originals
are limited to 144 MiB including the manuscript and uncached curated sources.
The worker keeps at most 32 MiB of newly parsed private caches, reparsing later
sources when necessary rather than accumulating every parsed document. Existing
legacy comparison-bound save caches retain their previous behavior. Shared
parsed snapshots remain limited to 192 MiB and admission reserves working space.
The engine remains bounded to 480 comparison seconds, 720 CPU seconds and
780 wall seconds; full completion is workload-dependent, not guaranteed for
every 50-file batch.

The comparison report becomes available independently of durable saving.
“Saving”, “Saved”, “Already in the library”, “Not saved” and failure reasons are
shown per file with a random save reference. A failed/uncertain save never displays
success. **Retry save** retries only source validation/storage, never comparison.
The older **Retry library save** action remains supported for old comparison-bound
requests; it retries their storage only. The new UI no longer submits those flags.

Pending uploads and owner-scoped retry receipts expire after one hour and may be
lost on restart; only acknowledged saved papers are durable. The UI warns before
leaving with pending or failed saves. After a lost receipt, check the library and
explicitly retry with the original file if it is absent. Operators can inspect
up to 100 seven-day durable diagnostic entries in the catalog, containing only
random save ID, timestamp, state and coarse error code—no email, capability,
filename or document text. Historical failures predating these receipts cannot
be reconstructed once their temporary job records expire.

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
No resource or warm-replica change accompanies bulk uploads. The source-parser
ceiling is 37.5 minutes/day (18.75 hours/30 days), versus the previous
20-admission worst case of 15 minutes/day. This bounds additional parser work to
11.25 hours/30 days at maximum sustained use; queue, gateway, comparison and
storage operations also cost money. The approximate budget is not a hard spend
guarantee, and measured low-cost synthetic papers do not predict every PDF.

Offline resource verification: `deploy/verify_bulk_local.py` creates only original
synthetic documents. Run it inside the candidate Linux image with `--network none
--cpus 1 --memory 2g` and `PYTHONPATH=/app`. A 45-page manuscript against
44 cached plus 50 independently uploaded eight-page sources checked all 94 and
produced a PDF in 48.21 seconds, with 118,521,856 bytes worker peak RSS in the
initial candidate run. These are synthetic workload observations, not live-user
or worst-case performance claims.

Do not copy local SQLite data or visitor history into this store. Do not enable
anonymous blob access, issue browser SAS links, or put originals in Git/Pages.
Validate persistence by saving an original synthetic source, restarting the
service, and comparing from an independent visitor session. Clean up only the
explicit synthetic test object/catalog entry through operator-scoped maintenance;
never delete user sources as part of a test.
