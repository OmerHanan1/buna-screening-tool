# Owner-restricted deployment

The hosted app can require Microsoft sign-in instead of anonymous capabilities.
This does not grant rights to additional papers. The initial authenticated release
keeps the same 12 reviewed sources; the other local files remain local until a
file/version-specific hosted-use basis is confirmed.

## Identity configuration

Supply **all three** server settings:

* `BUNA_TEAM_TENANT`: the personal Microsoft Entra directory UUID.
* `BUNA_TEAM_CLIENT`: the secretless SPA application/client UUID.
* `BUNA_TEAM_OWNER_OID`: the exact approved owner's object UUID **in that directory**.

The app refuses partial configuration. The registration is single-tenant
`AzureADMyOrg`, requests v2 access tokens, and exposes delegated
`api://CLIENT/access_as_user`. Register only the exact HTTPS Pages URL as a SPA
redirect. Implicit grants are disabled; there is no client secret.

The frontend uses Microsoft's MSAL authorization-code/PKCE implementation. OAuth
state/nonce/PKCE handling belongs to MSAL, not hand-written URL parsing. Its token
cache is tab-scoped session storage. Logout clears the app's local job references
and invokes the Microsoft logout flow. Already issued bearer access tokens remain
valid until expiry; they are not placed in URLs or shared links.

The backend checks signature, RS256, fixed-tenant signing keys, exact issuer,
audience, tenant, expiry/not-before, version, delegated scope and authorized client.
It then permits **only the configured object ID**. Email strings, frontend account
selection, membership in the directory, and caller-supplied identity headers do
not authorize access. Work accounts and other personal accounts are denied even
if they present a similarly named email claim. Jobs are owned by validated
issuer/object ID, not by session-chosen IDs.

All API routes except non-sensitive `/api/auth/config` require identity. `/health`
remains public for platform probes. Anonymous session issuance is disabled.
Library metadata, create/status/cancel/delete, PDF and JSON are protected. There
are no source-file, full-source-text or curation/admin download routes.

## Directory operations

ARM `--subscription` is **not** a reliable Microsoft Graph directory selector.
Verify the personal subscription's tenant, then use explicit `--tenant` for
Graph token acquisition; Azure CLI does not accept both flags together on that
command. Check the returned token's tenant/audience locally before making a
Graph request, then confirm `/me` matches the approved personal identity.
Never log the token or fall back to an implicitly selected work directory.
If interactive authentication or admin consent is needed, stop the affected
operation and report that requirement rather than guessing an identity.

## Corpus and report boundary

The curated files are in the **private Azure container registry image**, read-only
inside the worker environment. Only the service's narrowly scoped pull identity
can pull that image. They are not in Git, Pages assets, public SAS URLs or API
file routes. Do not enable registry admin access or publish a private-corpus image
to a public registry. Approved future private files must use a new reviewed
exact-hash manifest and a separate private build context; never copy the local
database, comparison history or user upload tree.

The private loader is opt-in via `BUNA_TEAM_CORPUS_SHA`, a reviewed manifest SHA256.
It refuses startup without complete team authentication, exact identity binding
and a `private-team-processing` manifest. Every file requires a hash plus a
`hosted_processing_permission` record containing `approved: true`, scope
`owner-hosted-research-reports-only`, basis, evidence, reviewer and review date.
Pending rights do not satisfy this record. Limits are 50 files / 160 MiB total,
with the existing per-source limits. No permission manifest is generated from a
sign-in, checkbox or local Ready status. This path is prepared but not enabled
for the 32 held local files. Private rights-review evidence is not included in
library responses or report exports.

Team reports include complete saved matched source passages with original text,
not a full comparison-source appendix. Unrelated surrounding source text is
removed from exported evidence; matching scores, counts and manuscript markings
are unchanged. Attribution obligations still apply. There is no blanket claim
that sign-in, private storage, lack of fees, or an export setting supplies a
copyright license or makes every internal use lawful.

## Safe rollout and verification

1. Validate the exact identity and registration without global account switches.
2. Build with `VITE_PUBLIC_MODE=true` and `VITE_TEAM_MODE=true`.
3. Deploy server settings together; never put held files in an anonymous revision.
4. Before switching the live route, check for active comparisons. Do not cancel
   them or silently rewrite existing results for this migration.
5. Verify anonymous/invalid/nonallowlisted requests are denied and source routes
   do not exist; check the real Microsoft login redirect uses tenant, client,
   exact redirect URI, response type `code` and PKCE S256.
6. The approved owner must complete Microsoft sign-in/consent to verify the real
   allowed-user flow. A synthetic signed-token unit test is not proof that their
   browser completed that flow. Do not simulate or claim it.

The same temporary workspace retention, one-worker resource bounds and
scale-to-zero budget apply. The public static page is only a login shell before
authentication. An identity-provider client ID is public configuration, not a
secret. No new paid authentication tier is required by this configuration.
