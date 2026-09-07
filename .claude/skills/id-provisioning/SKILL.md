---
name: id-provisioning
description: How and when loan-onboarding-poc creates a customer/account record on loan approval (not at application time), the persist_decision idempotency sequence, the active-account-per-product-type rule, and Phase 14's returning-customer profile refresh / Government-ID reuse design. Triggers on "create_application", "persist_decision", "get_or_create", "customer_id", "account provisioning", "applying without being a customer", "reuse_existing_id_photo", "has_active_account_of_type", "check_decision_allowed", "get_available_product_types", "id reuse", "promote_government_id_to_customer_photo", "returning customer prefill".
---

### Applying without being a customer yet

The original design had `bff_customer` eagerly create a `customer` row
(and an `account` under it) the moment someone typed an identifier and
started an application — modeling a bank's *existing* customer applying
for another product. The actual product intent is closer to real loan
origination: **most applicants aren't customers yet, and an account is
the *outcome* of an approved loan, not something that pre-exists it.**

- **`applications.applicant_identifier`** (new column, `NOT NULL`) is
  the durable key for "which human is this," always known at
  submission regardless of whether they're a recognized customer — the
  same value `bff_customer`'s session cookie already holds (PRD §7.1).
  This is what the customer-facing visibility filter (PRD §10 success
  criterion 2) is keyed on now, **not** `customer_id` — it has to work
  identically for a first-time applicant (no `customer_id` yet) and a
  returning one.
- **`applications.customer_id` is nullable.** `application.service.create_application(...)`
  resolves it via a **read-only** lookup —
  `customer.service.find_by_identifier(applicant_identifier) ->
  Customer | None` — never a create. If the identifier matches an
  existing customer, the application is linked immediately; if not,
  `customer_id` stays `NULL` until (and unless) the application is
  approved.
- **`accounts.application_id` is `NOT NULL` and `UNIQUE`** — the
  account points at the application that produced it, not the other
  way around (not `applications.account_id`), because (a) that reverse
  pointer would give no way, given an account, to find which
  application produced it, and (b) this direction lets the `UNIQUE`
  constraint on `accounts.application_id` serve as `persist_decision`'s
  idempotency guard directly (see step 2 below), instead of a
  separately-written, easy-to-get-wrong nullable column on
  `applications`. There is still no "auto-opened account" —
  `account.service.find_or_create_for_customer(...)` is gone, and
  `account/` still doesn't enforce one-account-per-customer (see
  `account/`'s module section).
- **Provisioning happens inside `application/activities.py`'s
  `persist_decision`, only on the transition to terminal `APPROVED`**
  (either the Underwriter's below-threshold approve, or the Manager's
  approve after escalation — *not* the intermediate
  `PENDING_MANAGER_APPROVAL` step, which isn't a terminal approval).
  **Idempotency check first, before anything else**: call
  `account.service.get_by_application_id(application_id)`. A non-`None`
  result means this activity execution is a Temporal retry of an
  already-provisioned application (successful-but-unacknowledged, or a
  genuine partial failure partway through a prior attempt) — skip
  straight to the final decision-column write below, using
  `existing_account.customer_id` in place of step 1. Otherwise:
  1. If `applications.customer_id` is still `NULL`, call
     `customer.service.get_or_create(applicant_identifier) ->
     Customer` (idempotent find-or-create — this is the *only* caller
     of this function left; `bff_customer`'s identify step no longer
     calls it, see `customer/`'s module section below).
  2. Call `account.service.create_account(customer_id, product_type,
     application_id) -> Account` — **always creates a new row**, no
     find-or-create semantics, since accounts are 1:1 with approved
     applications now, not 1:1 with customers (a customer can hold
     many accounts, one per approved loan — a plain, non-unique index
     on `accounts.customer_id`). `product_type` is a required column
     too — see the active-account rule immediately below. **This
     INSERT, once committed, is itself the durable idempotency
     marker** — no separate write back onto `applications` is needed
     the way the old `account_id`-on-`applications` design required
     (that write's entire reason to exist was giving a retry something
     to check; `accounts.application_id`'s own `UNIQUE` constraint does
     that job now, one step earlier and with nothing to get out of
     order).
  3. Call `document.service.tag_application_documents(application_id,
     account_id, customer_id)`,
     `document.service.promote_government_id_to_customer_photo(application_id,
     customer_id)`, and `document.service.generate_welcome_letter(applicant_identifier,
     account_id, customer_id, applicant_name, product_type, amount)` —
     see `document/`'s module section and "Document metadata assignment
     lifecycle" below for what each does and why all three, not just
     one. **A retry that finds an account already provisioned (the check
     above) skips all three of these calls entirely, permanently** — a
     smaller, manually-recoverable gap (some documents short a few
     metadata fields, no Welcome Letter) than a duplicated account, and
     consistent with this project's existing rare-enough-to-accept-for-
     a-POC stance elsewhere in this section. (This is the same tradeoff
     an earlier draft of this file already accepted; only the mechanism
     that makes the retry recognize "already provisioned" has moved,
     from a column on `applications` to the `accounts` row itself.)
     **Built (Phase 19)**: a fourth call in this same block,
     `notifications.service.send_welcome_letter_email(applicant_identifier,
     account_id, product_type, amount)`, right alongside
     `generate_welcome_letter` — the second of PRD §4's two narrow
     exceptions to the no-proactive-notification non-goal (the first,
     already built, is the account-closure-decision email — see
     "Account closure" below). Deliberately placed inside this same
     `existing_account is None` guard, not a separate check: same
     "a retry skips it, permanently, rather than resending" tradeoff
     this bullet already accepts for the other three calls, not a new
     idempotency mechanism — **confirmed live, not just in theory**,
     during P19-3's own verification sweep: an unrelated local-environment
     mistake (a worker process missing its Mayan env vars) made a real
     retry take exactly this skip path, proving the accepted tradeoff
     holds for the new call too, not only the original three. Requires a
     new import edge, `application/activities.py` → `notifications/` —
     `application/`
     doesn't have this exception yet (only `account/` does, from Phase
     18); no `.importlinter`/`pyproject.toml` layers restructuring
     needed to add it, unlike `account/`'s own case, since
     `application/` already sits above the bottom `idgen | notifications`
     tier in the existing layers contract.
  4. Write `status` and the underwriter/manager decision columns on
     `applications` — `customer_id` travels along in this same
     `UPDATE` (harmless if it's already set: `COALESCE` preserves it
     either way). There is no `account_id` column on `applications`
     to write here anymore.
  - **This makes provisioning the one place in the whole codebase where
    a Temporal activity's idempotency actually matters in a way that
    can silently misbehave**: activities can be retried by Temporal
    after a successful-but-unacknowledged execution, *or* after a
    genuine partial failure partway through. Creating a new `account`
    row (or a second Welcome Letter, or re-promoting an
    already-promoted `id_photo`) unconditionally on every call would
    duplicate state on a retry — the `get_by_application_id` check
    above, backed by `accounts.application_id`'s `UNIQUE` constraint,
    is what makes the whole activity safe to run twice.
- **One customer, one active account per product type — enforced
  *before* the decision is signaled, not just inside provisioning.**
  `accounts.product_type` (new column) plus a partial unique index
  (`db/schema.sql`'s `ux_accounts_customer_active_product_type`, on
  `(customer_id, product_type) WHERE status = 'ACTIVE'`) is the
  authoritative enforcement — a customer can hold any number of
  `CLOSED` accounts of the same type, just never two `ACTIVE` ones at
  once. But by the time `persist_decision` runs, the decision has
  already been accepted by the workflow — there's no clean way to
  surface an error back to whoever clicked Approve from that deep
  inside activity execution. So the real gate is earlier:
  `application.service.check_decision_allowed(application_id, decision)
  -> list[str]` (empty = OK, same shape as `check_completeness`) —
  called by `bff_backoffice` **before** it calls
  `workflow.service.signal_decision(...)`, for both the single-item and
  bulk-approve paths (bulk approve pre-filters each selected
  application this way *before* collecting `workflow_ids` to hand to
  `bulk_signal_decision`; anything blocked is reported as a per-item
  failure, same shape as any other bulk partial-failure). Only relevant
  for `decision == "APPROVE"` — Reject/RequestMoreInfo/Cancel never
  create an account, so never conflict. This is what actually justifies
  `application/service.py`'s new read-only call into
  `account.service.has_active_account_of_type(customer_id,
  product_type)` (see the corrected module-boundary rule above). **A
  real, accepted gap in the window itself, not fully closed**: two
  different staff members approving two different applications for the
  same customer+product_type within the small-but-nonzero window
  between this pre-check passing and `persist_decision` actually
  writing the account can still both pass the check — the partial
  unique index is the backstop that stops the bad state from ever
  being written. What's no longer a gap is what happens to the loser
  when that's hit: `persist_decision` now converts it into a clean
  `REJECTED` outcome instead of a stuck application and a `FAILED`
  Temporal workflow — see "Known gaps" below for the full mechanism,
  the live repro, and why the window itself was left open on purpose.
- **The same rule is now also enforced proactively, at intake, not just
  at approval — a UX addition, not a second source of truth.**
  `application.service.get_available_product_types(applicant_identifier)
  -> list[str]` resolves the customer via the existing read-only
  `find_by_identifier`, then filters `workflow.task_queues.KNOWN_PRODUCT_TYPES`
  down to the ones `account.service.has_active_account_of_type(customer_id,
  product_type)` returns `False` for — the exact same read
  `check_decision_allowed` already relies on, so the two can never
  disagree. A first-time applicant (no resolvable customer yet) gets
  every product type back, since there's nothing yet to conflict with.
  `bff_customer`'s product picker (`GET /apply/new`) calls this to
  render only the eligible types — a **hard elimination, confirmed with
  the user as deliberate: no "apply anyway" override.** A customer
  already active in every product type sees an explanatory message
  instead of an empty list. `POST /apply/new/start` re-runs the same
  check server-side before accepting the submitted `product_type` — the
  picker only hides the option, it doesn't stop a direct POST past it.
  **`check_decision_allowed`'s approval-time gate is deliberately left
  completely unchanged and un-simplified by this** — it remains the
  sole authoritative enforcement; this new function only ever narrows
  what the customer is *offered*, it never replaces the backstop that
  catches a bypass. See `PRD.md` §8.1/§9.2 for the product framing and
  §11 for the real gap this surfaced: there is still no way to ever
  transition an `accounts.status` row from `ACTIVE` back to `CLOSED` in
  this codebase, so combined with this hard elimination, a customer
  approved once for a product type can now never apply for that type
  again through the UI — raised directly by the user as a "close
  account" feature worth designing, not designed or built here.
- **Document upload/completeness-check at submission time never needs
  an `account_id`**, since no account can possibly exist before
  submission — `document.service.upload(...)` attaches
  `applicant_identifier`/`application_id`/`category` (plus `customer_id`
  when the applicant already resolves to a customer) regardless. Post-
  approval, every document under the application gains `account_id` too
  (`tag_application_documents`, "Document metadata assignment
  lifecycle" below) — the account-level metadata isn't gone, it's just
  attached later, by a different code path (provisioning, not
  submission). See "Document hierarchy" below for how staff actually
  browse these fields — corrected there from an
  `applicant_identifier`-rooted single index to three separate
  entity-rooted indexes; that redesign changed nothing about *when*
  metadata gets attached, only how it's organized for browsing.

### Returning-customer profile refresh and ID reuse (built — Phase 14)

Built and live-verified. This section describes the design behind
`IMPLEMENTATION_PLAN.md`'s Phase 14, written first per this project's
own convention (architecture doc before implementation). Confirmed with
the user as a deliberate enhancement to "Applying without being a
customer yet" above, not a correction of it — everything in that
section still holds; this adds two things on top: (1) a customer's
profile actually gets populated and kept current, instead of staying
permanently `NULL`, and (2) a *returning* customer gets a materially
better experience — a prefilled form and the option to skip
re-uploading a Government ID they already have on file.

- **Customer profile used to be write-once-never-filled, a real gap
  found while designing this**: `customer.service.get_or_create(applicant_identifier)`
  used to take *only* the identifier — `customers.name`/`email`/`phone`
  stayed `NULL` forever, for every customer. Phase 14 fixed this two
  ways:
  1. `customer.service.get_or_create` gains three new parameters —
     `get_or_create(applicant_identifier, name, email, phone) ->
     Customer` — so the *first* approval that creates a customer row
     seeds it from that application's own denormalized
     `applicant_name`/`applicant_email`/`applicant_phone` instead of
     leaving the profile blank.
  2. A new `customer.service.update_profile(customer_id, name, email,
     phone) -> Customer` write path, called instead of `get_or_create`
     when `persist_decision` finds `applications.customer_id` already
     set (an existing customer's *later* application being approved).
     **Policy: unconditional overwrite, not fill-blanks-only** — the
     most recently *approved* application's submitted details always
     win. This is the direct, consistent reading of "Denormalized
     applicant fields, on purpose" above: `application/` is what was
     submitted at the time, `customer/` is the *current* profile, and
     an approved application is exactly the trust signal that makes
     "current" worth updating.
  3. `application/activities.py`'s `persist_decision` branches on
     `record["customer_id"]` to pick which of the two to call — both
     already sit in the one file in `application/` allowed to import
     `customer/`, so this is a same-file branch, not a new import.
- **Returning-customer form prefill**: `bff_customer`'s new-application
  wizard start step calls the existing **read-only**
  `customer.service.find_by_identifier(applicant_identifier)` (already
  used elsewhere for "welcome back" copy) and, if it resolves,
  prefills `applicant_name`/`applicant_email`/`applicant_phone` —
  still editable, and a correction made here is exactly what feeds
  back into `update_profile` above on this application's own eventual
  approval. No new `service.py` function needed; this is purely a
  `bff_customer/routes.py` + template change.
- **ID reuse can't be "re-tag the old document into the new
  application" — a real Mayan constraint rules that out, confirmed
  against `mayan_client.py`'s own `attach_metadata`/
  `update_metadata_entry`**: Mayan holds exactly **one value per
  (document, metadata_type)** — a document's `application_id` metadata
  entry can be created once and later *updated* in place, never
  duplicated. Re-pointing an existing `id_photo` document's
  `application_id` to a brand-new application would silently unfile it
  from the *old* application's own `<application_id> -> Government ID`
  index leaf — a real correctness break, not a cosmetic one. So reuse
  works the other way: **the new application's document gate is told
  to skip Government ID, not that some other document already
  satisfies it.**
  1. `document.service.check_completeness` gains an optional
     `exclude_categories: list[str] | None = None` parameter —
     `required = [c for c in REQUIRED_CATEGORIES[product_type] if c
     not in (exclude_categories or [])]`. A small, general parameter
     rather than a Government-ID-specific special case, even though
     Government ID is the only category this call site excludes today.
  2. A new **read-only** `document.service.has_id_photo(customer_id)
     -> bool` (a thin wrapper over the existing
     `list_customer_documents(customer_id)` — any result *is* the
     `id_photo`, per the one-per-customer invariant below).
  3. `application.service.create_application(...)` gains a new
     `reuse_existing_id_photo: bool = False` parameter. When `True`
     *and* the applicant resolves to an existing customer (via the
     read-only `find_by_identifier` lookup this function already does)
     *and* `document.service.has_id_photo(customer_id)` is `True`, it
     calls `check_completeness(application_id, product_type,
     exclude_categories=[document_service.CATEGORY_GOVERNMENT_ID])`
     instead of the bare call. **Reuse is a customer choice surfaced in
     the UI, not an automatic silent skip** — the wizard shows "We
     already have a Government ID on file for you" with an explicit
     "Upload a new one instead" override once `find_by_identifier` +
     `has_id_photo` both resolve true; nothing is skipped unless the
     customer actually leaves reuse selected.
  4. `resubmit_application` does **not** get this parameter in Phase
     14 — deliberately deferred (see `PRD.md` §11's open questions);
     a customer resubmitting from `MORE_INFO_REQUESTED` who never
     uploaded a Government ID for *this* application still has to
     upload one, even if they're a known returning customer. A smaller
     gap than leaving reuse unbuilt entirely, and resubmit's document
     gate re-check already only fires when the customer touches
     documents at all (see `application/`'s module section below).
- **`id_photo` is refreshed by a later approved application's fresh
  upload, not fixed forever — corrects a real, previously-undocumented
  gap found while designing this feature, not something this feature
  introduces.** `PRD.md` §6.5 used to state "the first one stands," but
  `document.service.promote_government_id_to_customer_photo` never
  actually enforced that — it unconditionally re-tagged whatever
  Government ID document existed under the just-approved application,
  with no check for a prior `id_photo`. This was invisible before Phase
  14 because nothing exercised a *second* approval, for an
  already-a-customer applicant, with a fresh Government ID upload —
  exactly the case this phase makes reachable. Phase 14 resolved the
  tension in favor of the new intent (refreshable, not frozen) and made
  the enforcement real:
  1. If no Government ID document exists under the just-approved
     `application_id` (the reuse path — nothing was uploaded), `promote_government_id_to_customer_photo`
     returns early, a no-op — **changed from this function's original
     behavior, which `raise`d `DocumentNotFound`** in this case; that
     exception was written under the old assumption that every approved
     application always has its own Government ID document, no longer
     true once reuse exists.
  2. If one *does* exist (a fresh upload — either a first-time
     applicant, or a returning customer who chose "upload a new one
     instead"), the function first finds any *other* document
     currently carrying this customer's `customer_id` metadata
     (excluding the one about to be tagged) and strips that metadata
     entry via a new `mayan_client.delete_metadata_entry(document_id,
     metadata_entry_id)` (a plain wrapper over the already-generic
     `self.delete(...)` — Mayan's create/update metadata calls already
     exist in `mayan_client.py`, delete was simply never needed until
     now), *then* tags the new one. **Exactly one current `id_photo`
     per customer, enforced for real** — a reused-ID application
     leaves the existing one untouched (step 1); a fresh-upload
     application supersedes it (step 2).
  3. `persist_decision` itself doesn't grow a new branch — it still
     calls `promote_government_id_to_customer_photo(application_id,
     customer_id)` unconditionally on every terminal `APPROVED`
     transition, same as today; the no-op-vs-supersede behavior above
     lives entirely inside `document/service.py`.
