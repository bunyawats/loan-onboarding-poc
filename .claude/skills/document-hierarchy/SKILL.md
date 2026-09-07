---
name: document-hierarchy
description: loan-onboarding-poc's three Mayan EDMS index templates (Customer/Account/Application Index, exclusive-placement model), the five index-template gotchas, and the document metadata assignment lifecycle -- exactly when applicant_identifier/application_id/account_id/customer_id get attached to a document, and why promote_government_id_to_customer_photo creates a genuine second Mayan document rather than re-tagging. Triggers on "index template", "Mayan index", "document hierarchy", "rebuild_index", "exclusive placement", "metadata assignment", "tag_application_documents", "promote_government_id_to_customer_photo", "customer index", "account index", "application index", "index node", "document type metadata association".
---

## Document hierarchy

**Three separate index templates**, each rooted at a different one of
the three entity ids a document can carry — three different entry
points into the same document set, confirmed with the user directly
rather than assumed (neither a single index nor
`applicant_identifier`-as-root was what staff actually wanted to browse
by). A document lives at exactly *one* leaf per index — the deepest
entity it's actually tied to, matching the real customer → account →
application hierarchy — a **strict "exclusive placement" model**,
requested directly by the user after an earlier multi-placement design
(the same document shown at every branch whose condition matched) read
as confusing to browse in practice:

```
Customer Index (customer_id)
└── <customer_id>
       ├── <account_id>
       │      ├── <application_id>
       │      │      └── <category>       (e.g. Bank Statements --
       │      │                            docs with all three ids set)
       │      └── <category>               (account-only docs, e.g.
       │                                    Welcome Letter -- account_id
       │                                    + customer_id, no application_id)
       ├── <application_id>                (docs with application_id +
       │      └── <category>                customer_id but NO account_id
       │                                    yet -- pre-approval upload
       │                                    from a returning customer)
       └── <category>                      (the customer-level
                                             Government ID copy --
                                             customer_id only, no
                                             account_id/application_id
                                             at all; see "Document
                                             metadata assignment
                                             lifecycle" below)

Account Index (account_id)
└── <account_id>
       ├── <application_id>
       │      └── <category>               (docs with account_id +
       │                                    application_id)
       └── <category>                      (account-only docs, no
                                             application_id)

Application Index (application_id)
└── <application_id>
       └── <category>                      (application is already the
                                             deepest owning entity for
                                             its own documents in the
                                             real hierarchy -- no further
                                             branching needed, whether or
                                             not the application has also
                                             gained account_id)
```

**No more cross-reference branches** (Account Index's old "customer"
sibling, Application Index's old "customer"/"account" siblings) — each
of those would have needed to either duplicate placement (the exact
thing this redesign removes) or dead-end with no documents under it, so
they're gone entirely rather than kept as inert navigation. A customer
looking to browse by account or application uses Customer Index (which
still nests both); Account Index and Application Index each answer only
"what does *this* account/application directly own." Every leaf
condition explicitly excludes the deeper case it doesn't own (e.g.
Customer Index's account-only leaf requires `account_id` present *and*
`application_id` absent) — Django's `{% if %}` supports `not` for this
(`{% if a and b and not c %}`), same tag used elsewhere in these
templates.

**The customer-level Government ID copy is exactly what makes Customer
Index's direct-category leaf unambiguous** (unlike the earlier
multi-placement design's version of this leaf, which matched *every*
document with `customer_id` — Proof of Income, Welcome Letters, all of
it): only the copy has `customer_id` with neither `account_id` nor
`application_id`, so it's the only thing that can ever land there. See
"Document metadata assignment lifecycle" below for why this document
exists as a genuine second Mayan document now, not a re-tagged original.

Live-verified end to end against a real instance (a customer with two
approved applications plus a rejected third, each landing in exactly
one place across the three indexes) — full sweep moved to
`IMPLEMENTATION_PLAN.md`'s Session Log, 2026-09-04 docs-consolidation
entry. `applicant_identifier` plays no role in any of the three trees —
it's still attached to every document (see `document/service.py`'s
`upload`) and still what `document.service.py`'s own queries filter on
(see the gotcha #2 consequence below), just never an index-tree
grouping key.

**A real, mid-build reliability wrinkle, not a template bug — hit twice,
in two different sessions, both from the same root cause: overlapping
`rebuild/` calls fired in quick succession race Mayan's own
reset-then-rebuild sequence and can leave a tree briefly at
`depth=0`/`node_count=0`, even when the template definitions are
correct.** Full repro moved to `IMPLEMENTATION_PLAN.md`'s Session Log
(2026-09-04 docs-consolidation entry). **The operating rule this
confirms, and the reason this paragraph stays in full here rather than
moving with the rest**: never fire a `rebuild/` call — for any index —
while a previous `rebuild/` call against *any* index might still be in
flight, and always confirm `node_count` stable across several polls
before trusting a rebuilt tree.

**A real, load-bearing bug found while deleting the old single index**:
`document/mayan_client.py`'s `rebuild_index()` used to look up a single
hardcoded slug, `INDEX_TEMPLATE_SLUG = "loan-onboarding-archive"` —
deleting that index without updating this constant would have made
every document upload in the whole application start failing. Fixed by
replacing the single slug with `INDEX_TEMPLATE_SLUGS = ("customer-index",
"account-index", "application-index")` and a new
`index_template_ids() -> list[int]` that `rebuild_index()` now loops
over, rebuilding all three. Deliberately still excludes "Creation
date" — nothing in this codebase rebuilt that index before this fix
either. Live-verified after the fix — see the Session Log entry above.

**Multi-leaf placement is real Mayan behavior, no longer exploited on
purpose — historical context, not current design.** Two earlier
drafts of this file relied on it (source-confirmed via
`mayan/apps/document_indexing/models/index_instance_models.py`'s
`_document_add()`, which walks *every* child branch at each tree level
and links a document into *all* branches whose conditions independently
evaluate true, not just the first match — full verification narrative
in the Session Log entry above). **Both uses are gone now** — the
exclusive-placement redesign above replaced them specifically because
multi-placement read as confusing when browsing (the user's own direct
feedback), and the customer-level Government ID copy (a genuine second
document, not a re-tagged original — see "Document metadata assignment
lifecycle" below) means no document needs to satisfy two leaves
simultaneously anymore. The mechanism itself is still true of Mayan and
worth knowing if a future design ever wants it back. **Cabinets were
evaluated as an alternative and rejected as the hierarchy's backbone**
— they also support true multi-membership and are synchronous (no
Celery, unlike Index Templates), but the project's actual usage pattern
is automatic, upload-time classification via API, which is Index
Templates' idiomatic niche, not Cabinets' (a third-party source
describes Cabinets as manual, file-manager-style curation).

**A sharper, previously-implicit consequence of gotcha #2 (async
reindex)**: `document.service.check_completeness()` and
`list_documents()`/`list_customer_documents()`/`list_account_documents()`
**must query Mayan's document/metadata search API directly, filtering
on the relevant id + category metadata — never read the Index Template
tree.** Metadata attachment itself is synchronous; only the *index's*
recomputed tree membership is async (Celery-driven, per gotcha #2). If
`check_completeness` walked the index tree instead, a customer who
uploads their last required document and immediately hits Submit could
get a false "still missing" result purely from index lag — a real
correctness bug, not a hypothetical, since `create_application()` calls
`check_completeness()` synchronously right after the customer's last
upload (PRD §6.4). **This principle is exactly why swapping the index
templates out entirely (this section's redesign) required zero changes
to any of `document/service.py`'s query functions** — none of them ever
read the Index Template tree in the first place; the tree exists purely
for staff to browse the archive visually in Mayan's own UI, never as a
data source for this application's own logic.

The same **five gotchas** documented in `mayan-edms-customer-archive`'s
`docs/document-hierarchy-setup.md` still apply — read that file before
touching any index template or `document/`'s setup script (they're
about index-template mechanics, not any particular tree shape):

1. Empty index-node expressions don't prune the branch — every leaf
   condition must repeat the full ancestor requirement set.
2. Index updates are async (Celery) — always rebuild the index after
   attaching all metadata, wait ~10-15s before reading the tree.
3. `action_name` on file upload is a string ID (`replace`); an invalid
   value fails silently (HTTP 200, broken async task).
4. A file that passes magic-byte sniffing may still have zero
   extractable pages — verify real uploads actually render.
5. `GET /index_templates/<id>/nodes/` doesn't return a wrapped root —
   `results` *is* the children array.

`DELETE /api/v4/documents/{id}/` moves to Mayan's trash, not a hard
delete — confirmed via the endpoint's own OPTIONS description in the
reference project.

## Document metadata assignment lifecycle

**Five rules, confirmed with the user, that together describe exactly
which of `applicant_identifier`/`application_id`/`account_id`/
`customer_id` a document carries at every point in its life** — the
"Document hierarchy" section above describes the resulting tree shape;
this section describes *when* each metadata field actually gets
attached to make that shape happen.

1. **At upload time, `application_id` (and `applicant_identifier`,
   `category`) are always attached** — true since Phase 6, unchanged
   here. `document.service.upload(...)`'s first three metadata fields
   are never optional.
2. **At upload time, `customer_id` is attached too, but only when the
   applicant already resolves to an existing customer.** A returning
   applicant's `bff_customer` wizard already resolves `customer_id` via
   the read-only `customer.service.find_by_identifier(...)` lookup
   (Phase 14's prefill step) and holds it in the session draft —
   `new_application_upload` now passes it straight into
   `document.service.upload(...)`'s new `customer_id` parameter. The
   resubmit path (`upload_more_info_document`) passes the application
   row's own already-resolved `customer_id` column the same way. A
   brand-new applicant has no `customer_id` to pass — `None`, same as
   every upload before this existed — so their documents stay
   `customer_id`-less until approval, same as today.
3. **On approval, every document under the application — not just the
   Government ID one — gets `account_id` and `customer_id` attached.**
   `document.service.tag_application_documents(application_id,
   account_id, customer_id)` (new — see the `document/` module section
   above) does this in one pass across every category, in place, on
   the documents themselves. This runs alongside, not instead of,
   `promote_government_id_to_customer_photo` and
   `generate_welcome_letter` (whose own new document gets `customer_id`
   too, for consistency). All three calls sit inside `persist_decision`'s
   existing `account_id IS NOT NULL` idempotency guard — a Temporal
   retry that finds the account already provisioned skips all three,
   permanently, same accepted smaller-than-a-duplicated-account gap this
   file already documents for the other two.
4. **`promote_government_id_to_customer_photo` creates a genuine second
   Mayan document — a customer-level copy — rather than re-tagging the
   original.** An earlier design attached `customer_id` directly to the
   just-approved
   application's own Government ID document, making one Mayan document
   satisfy two index leaves at once (CLAUDE.md's old "multi-leaf
   placement"). Changed after a direct design request: the application's
   Government ID document is now left completely untouched (still owned
   only by its application, consistent with "Document hierarchy"'s
   exclusive-placement rule); a *new* document is created instead, with
   the same file content (`mayan_client.download_file`, a full in-memory
   read — POC-scale documents only, no streaming needed for the copy)
   but tagged with only `customer_id`/`applicant_identifier`/`category`
   — deliberately no `application_id`/`account_id` at all, so it lives
   purely at the customer level (Customer Index's own direct
   `Government ID` leaf). If the customer already had a previous copy
   (a fresh Government ID on a *later* approved application, the
   "Returning-customer profile refresh and ID reuse" refresh case), that
   old copy is trashed first (`DELETE /documents/{id}/`, Mayan's own
   soft-delete) — still never more than one copy per customer at a
   time, just via delete-then-create instead of strip-then-retag. The
   reuse path (no fresh Government ID under the just-approved
   application) is still a no-op, unchanged — the existing copy is
   already the customer's current photo.
5. **A rejected, cancelled, or still-pending application's documents
   never get `account_id` — this was already true by construction, not
   new behavior.** `account_id` is only ever attached inside the
   terminal-`APPROVED` branch of `persist_decision`'s provisioning
   block; no other decision outcome creates an account or calls
   `document/` for account-tagging at all. Stated explicitly here
   because it was asked about directly, not because anything had to
   change to make it true.

**Two real bugs found against the real stack (P16-4), neither caught by
the unit suite — both only surfacing against genuine Mayan behavior,
full repro/verification narrative moved to `IMPLEMENTATION_PLAN.md`'s
Session Log, 2026-09-04 docs-consolidation entry**:

1. **A document type can only carry metadata types it's been explicitly
   associated with** — `account_id` had never been associated with
   "Application Document", nor `customer_id` with "Account Document",
   so the new attaches above were rejected outright with a 400.
   `scripts/setup_document_hierarchy.sh` now attaches both associations
   (`required=false`, since neither exists at upload/create time).
2. **Mayan rejects a second `POST` for a metadata type a document
   already carries** with another 400 — `tag_application_documents` and
   `promote_government_id_to_customer_photo` used to both attach
   `customer_id` to the same Government ID document when a fresh upload
   was promoted; this file's own earlier draft wrongly called that
   second attach "a harmless idempotent no-op." Fixed with a
   `document/service.py`-internal `_set_metadata` helper (update-in-place
   via `update_metadata_entry` if the field already exists, plain create
   otherwise). **Superseded, not reverted, by the exclusive-placement
   redesign below** — `promote_government_id_to_customer_photo` no
   longer touches the same document `tag_application_documents` does at
   all (it creates a brand-new Mayan document instead), so this
   double-attach can't recur structurally, not just because
   `_set_metadata` guards it. `_set_metadata` stays in use by
   `tag_application_documents`' own multi-category attach loop, where
   the original conflict-on-retry concern is still real.

**The exclusive-placement redesign (see "Document hierarchy" above and
rule 4 above) also exposed a real correctness bug in `reconcile.py`,
fixed alongside it**: `scan()` used to treat *every* stale `customer_id`
as a strippable secondary tag — true when `customer_id` only ever rode
alongside `application_id`, no longer true now that the customer-level
copy carries `customer_id` as its *only* metadata. `scan()` now checks
whether a document has `application_id`/`account_id` at all before
deciding orphaned-vs-stale (see rule 5 above and `scan()`'s own
docstring) — without this fix, a customer-level copy whose owning
customer row was deleted would have had its one identifying tag
stripped instead of the whole document being trashed, leaving a
permanently untethered, un-taggable document invisible to every future
reconciliation run.

**Deliberately out of scope**: no backfill of documents belonging to
applications approved *before* this lifecycle existed — same "forward-
looking only" scope boundary Phase 14 already accepted for not
backfilling existing customer profiles. An application approved before
this shipped keeps whatever metadata its documents already had; only
approvals from this point forward get the full `account_id`/
`customer_id` tagging on every document.

