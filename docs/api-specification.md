# API specification — internal `service.py` contracts

This is not an HTTP API — the whole point of the modular-monolith
architecture (`CLAUDE.md`) is that modules talk to each other via
**direct in-process Python function calls**, not a wire protocol. This
document is the "API" in the sense that matters here: the exact,
typed function signatures each module's `service.py` exposes to its
callers, so Phase 2–7 implementation (`IMPLEMENTATION_PLAN.md`) has one
place to check the contract instead of piecing it together from prose.

If any of the seven modules is ever extracted into a real network
service later (`CLAUDE.md`'s "Known gaps" already anticipates this),
these signatures become the new HTTP contract more or less directly —
another reason to nail them down precisely now.

**Read `CLAUDE.md`'s "Applying without being a customer yet" section
first** — the account-on-approval model (most applicants aren't
customers yet; an account is the *outcome* of an approved loan, not a
precondition of filing one) is why several signatures below take
`applicant_identifier` instead of `customer_id`/`account_id`, and why
`customer_id`/`account_id` are nullable throughout.

Conventions used below:
- All functions are `async def` (matching `asyncpg`/`httpx`/`temporalio`
  all being async).
- IDs are `UUID` (Python `uuid.UUID`) except `workflow_id`, which is
  Temporal's own string id.
- Money is `Decimal`, never `float`.
- Every dataclass shown is a plain return type (Pydantic model or
  `@dataclass` — implementer's choice, not load-bearing) mirroring its
  owning table (PRD §9).

---

## `customer/service.py`

```python
async def find_by_identifier(applicant_identifier: str) -> Customer | None: ...
async def get_or_create(applicant_identifier: str) -> Customer: ...
async def get(customer_id: UUID) -> Customer: ...
```

```python
@dataclass
class Customer:
    customer_id: UUID
    applicant_identifier: str
    name: str | None
    email: str | None
    phone: str | None
    created_at: datetime
```

- **`find_by_identifier` is read-only** — no side effects, `None` if no
  match. Called by `application.service.create_application(...)` at
  submission time to link an application to an existing customer if
  one matches, and optionally by `bff_customer` (e.g. "welcome back"
  copy) without ever writing a row.
- **`get_or_create` is find-or-create, idempotent — but called from
  exactly one place in the whole codebase**:
  `application/activities.py`'s `persist_decision`, at the moment an
  application resolves to terminal `APPROVED` with no customer already
  linked. Nothing else should call it — in particular, `bff_customer`'s
  identify step does **not** call this (see its module section in
  `CLAUDE.md`); the session cookie is a pure client-side write.
- `get` raises (a module-local `CustomerNotFound`) if the id doesn't
  exist.

---

## `account/service.py`

```python
async def create_account(customer_id: UUID, product_type: str) -> Account: ...
async def has_active_account_of_type(customer_id: UUID, product_type: str) -> bool: ...
async def get(account_id: UUID) -> Account: ...
```

```python
@dataclass
class Account:
    account_id: UUID
    customer_id: UUID
    product_type: str
    opened_at: datetime
    status: Literal["ACTIVE", "CLOSED"]
```

- **`create_account` always creates a new row — no find-or-create.**
  Accounts are 1:1 with approved applications now, not 1:1 with
  customers; a customer can hold many. Called from exactly one place:
  `application/activities.py`'s `persist_decision`, exactly once per
  application that reaches terminal `APPROVED`.
  **`persist_decision` must check `applications.account_id IS NOT
  NULL` before calling this** — Temporal can retry an activity after a
  successful-but-unacknowledged execution, and this function has no
  idempotency guard of its own (calling it twice makes two accounts).
  Also **not conflict-safe on its own** — it will happily violate "one
  active account per product type" if called for a customer who
  already has one; `db/schema.sql`'s partial unique index is the only
  thing that would stop it at that point (see
  `has_active_account_of_type` below for the actual, earlier gate).
  See `CLAUDE.md`'s "Applying without being a customer yet" for the
  full sequence.
- **`has_active_account_of_type` is read-only** — the function that
  makes the active-account rule enforceable *before* a decision is
  signaled, not just inside `persist_decision`. Called by
  `application.service.check_decision_allowed(...)`, never directly by
  a BFF.

---

## `document/service.py`

```python
async def upload(
    applicant_identifier: str,
    application_id: UUID,
    category: str,
    file: UploadFile,
) -> DocumentRef: ...

async def list_documents(application_id: UUID) -> list[DocumentRef]: ...

async def check_completeness(
    application_id: UUID,
    product_type: str,
) -> list[str]: ...  # missing category names, [] if satisfied

async def preview(
    application_id: UUID,
    document_id: str,
) -> AsyncIterator[bytes]: ...  # streamed to the BFF's response

# Managed documents beyond the submission-gate categories above --
# all system-triggered, none uploaded by a customer through the
# application flow. See CLAUDE.md's "Applying without being a
# customer yet" and "Document hierarchy" for the full mechanics.

async def promote_government_id_to_customer_photo(
    application_id: UUID,
    customer_id: UUID,
) -> None: ...  # re-tags the existing Government ID document with
                # customer_id -- does NOT copy it. Called only from
                # application/activities.py's persist_decision.

async def generate_welcome_letter(
    account_id: UUID,
    customer_id: UUID,
    applicant_name: str,
    product_type: str,
    amount: Decimal,
) -> DocumentRef: ...  # renders a templated PDF and uploads it tagged
                       # to account_id. Called only from
                       # application/activities.py's persist_decision,
                       # immediately after account.service.create_account.

async def upload_consent(
    account_id: UUID,
    file: UploadFile,
) -> DocumentRef: ...  # true Mayan versioning -- uploads a new FILE
                       # VERSION of the account's one "consent"
                       # document, creating it first if none exists yet.
                       # Callable by either BFF once account_id exists.

async def list_customer_documents(customer_id: UUID) -> list[DocumentRef]: ...
async def list_account_documents(account_id: UUID) -> list[DocumentRef]: ...
```

```python
@dataclass
class DocumentRef:
    document_id: str       # Mayan's own id, not a UUID we mint
    category: str
    filename: str
    uploaded_at: datetime
```

- **`upload`/`list_documents`/`check_completeness`/`preview` take no
  `customer_id`/`account_id` params.** The submission-gate branch of the
  Mayan hierarchy is two levels — `<applicant_identifier> ->
  <application_id> -> category` (`CLAUDE.md`'s "Document hierarchy") —
  because there's no `account_id` to organize under at upload time
  (uploads happen and the completeness gate is checked *before*
  submission, before any account can exist) and `customer_id` may not
  exist yet either. `applicant_identifier` is required, not resolved
  internally — `document/` is a leaf module and never imports
  `application/`; the caller (`bff_customer`, which already has it from
  the session cookie) passes it straight through.
- `check_completeness`'s required-category list per `product_type` is
  owned here (PRD §6.4's table), not in `application/` — `application/`
  just calls this function and trusts the answer. **A category is
  satisfied by one or more documents** — `upload()` is safe to call
  repeatedly for the same `application_id`/`category`; each call
  creates a distinct Mayan document, never overwrites a prior one.
- **`check_completeness`, `list_documents`, `list_customer_documents`,
  and `list_account_documents` must all query Mayan's document/metadata
  search API directly (filtered on the relevant id + category
  metadata), never read the Index Template tree.** Metadata attachment
  is synchronous; the *index's* tree membership is async (Celery — see
  `CLAUDE.md`'s document-hierarchy gotcha #2). `create_application()`
  calls `check_completeness()` synchronously right after the customer's
  last upload — reading the index tree here would risk a false "still
  missing" result purely from reindex lag, not an actual missing
  document. See `CLAUDE.md`'s "Document hierarchy" for the fuller
  reasoning (this came out of evaluating Cabinets as an alternative to
  Index Templates and confirming the async-reindex gotcha's real blast
  radius).
- `promote_government_id_to_customer_photo` and
  `generate_welcome_letter` are the two extra steps
  `application/activities.py`'s `persist_decision` runs inside its
  APPROVE-provisioning block (`CLAUDE.md`'s numbered sequence, steps
  1-3) — both guarded by that same block's single idempotency check
  (`applications.account_id IS NOT NULL` before starting at all), not
  independently idempotent themselves.
- `generate_welcome_letter` takes only plain arguments (no `Application`/
  `Customer`/`Account` objects) — `document/` stays a leaf module,
  never importing `application/`, `customer/`, or `account/` to look
  anything up itself.
- **`upload_consent`'s caller isn't decided yet** — `PRD.md`'s open
  questions flags which BFF (or both) exposes a consent-upload screen
  as unresolved; the function itself only needs `account_id`, so either
  side can call it once that exists.

---

## `workflow/service.py`

```python
async def start_workflow(
    application_id: UUID,
    product_type: str,
    payload: dict[str, Any],
    amount: Decimal,
    applicant_identifier: str,
    customer_id: UUID | None,
) -> str: ...  # returns workflow_id

async def signal_decision(
    workflow_id: str,
    actor_role: Literal["underwriter", "manager", "customer"],
    decision: Literal["APPROVE", "REJECT", "REQUEST_MORE_INFO", "CANCELLED"],
    actor_name: str,
    comment: str | None,
) -> None: ...

async def signal_resubmit(
    workflow_id: str,
    payload: dict[str, Any],
) -> None: ...

async def bulk_signal_decision(
    workflow_ids: list[str],
    decision: Literal["APPROVE", "REJECT"],
    actor_name: str,
    comment: str | None,
) -> list[BulkActionResult]: ...
```

```python
@dataclass
class BulkActionResult:
    workflow_id: str
    ok: bool
    error: str | None  # populated only when ok is False
```

- `amount`, `applicant_identifier`, and `customer_id` all travel as
  their own arguments, never read out of `payload` — the workflow
  needs `amount` for the PRD §6.3 escalation-threshold check, and needs
  `applicant_identifier`/`customer_id` only to forward them, by name,
  to the `persist_application` activity. `payload` stays
  product-specific-fields-only and the workflow never inspects any of
  these values itself (see `CLAUDE.md`'s note on `workflow/`'s
  "generic" framing).
- `bulk_signal_decision` only accepts `APPROVE`/`REJECT` — bulk
  `REQUEST_MORE_INFO`/`CANCELLED` aren't UI-exposed actions (PRD §8.2's
  bulk toolbar is Approve/Reject only); reject any other value at the
  function boundary rather than silently allowing it.
- `_MAX_BULK_SIZE = 50` — `bulk_signal_decision` raises if
  `len(workflow_ids) > 50` rather than silently truncating.

---

## `application/service.py`

```python
async def create_application(
    applicant_identifier: str,
    product_type: str,
    payload: dict[str, Any],
    applicant_name: str,
    applicant_email: str,
    applicant_phone: str,
    amount: Decimal,
) -> CreateApplicationResult: ...

async def resubmit_application(
    application_id: UUID,
    payload: dict[str, Any],
) -> Application: ...

async def check_decision_allowed(
    application_id: UUID,
    decision: str,
) -> list[str]: ...  # blocking-reason strings, [] if OK. No-op unless
                     # decision == "APPROVE". Called by bff_backoffice
                     # BEFORE workflow.service.signal_decision(...).

async def get(application_id: UUID) -> Application: ...

async def list_for_applicant(
    applicant_identifier: str,
    page: int,
    page_size: int = 10,
    query_id: str | None = None,
) -> PagedResult[Application]: ...

async def list_by_status(
    status: str,
    page: int,
    page_size: int = 10,
    query_id: str | None = None,
) -> PagedResult[Application]: ...
```

```python
@dataclass
class CreateApplicationResult:
    application: Application | None       # None if documents were missing
    missing_categories: list[str]         # [] if application was created

@dataclass
class Application:
    application_id: UUID
    applicant_identifier: str
    customer_id: UUID | None       # None until an existing customer is matched, or until approval
    account_id: UUID | None        # None until this application reaches terminal APPROVED
    workflow_id: str | None
    product_type: str
    payload: dict[str, Any]
    applicant_name: str
    applicant_email: str
    applicant_phone: str
    amount: Decimal
    status: str
    underwriter_name: str | None
    underwriter_comment: str | None
    underwriter_decided_at: datetime | None
    manager_name: str | None
    manager_comment: str | None
    manager_decided_at: datetime | None
    created_at: datetime
    updated_at: datetime

@dataclass
class PagedResult(Generic[T]):
    items: list[T]
    query_id: str
    total_count: int
    page: int
    page_size: int
```

- **No `customer_id`/`account_id` params on `create_application`** —
  neither is guaranteed to exist yet. Internally: generates
  `application_id`, resolves `customer_id` via the **read-only**
  `customer.service.find_by_identifier(applicant_identifier)` (`None`
  for a new applicant), `account_id` is always `None` at this point
  regardless, validates `payload` against `product_type`'s schema,
  calls `document.service.check_completeness(...)`. Never calls
  `workflow.service` when documents are missing —
  `CreateApplicationResult.application` is `None` in that case, and the
  caller (a BFF) renders `missing_categories` directly.
- **`list_for_applicant` takes `applicant_identifier`, not
  `customer_id`** — this has to return an applicant's own applications
  even before any of them are approved and `customer_id` gets resolved.
  A `query_id` minted for one `applicant_identifier` must be
  rejected/ignored if reused with a different one — this is the actual
  mechanism enforcing "a customer only ever sees their own
  applications" (PRD §10 success criterion 2), not a UI-level filter.
- **`check_decision_allowed` resolves `customer_id`, then calls
  `account.service.has_active_account_of_type(customer_id,
  product_type)`** — both read-only. If the application's `customer_id`
  is still `NULL` (a brand-new applicant), it short-circuits to `[]`
  immediately — a customer with zero accounts can't conflict with
  anything. `bff_backoffice` calls this before signaling an Approve
  decision, for both single-item and bulk paths; see `CLAUDE.md`'s
  "Applying without being a customer yet" for the full active-account
  rule, including its accepted race-window gap.

### `application/activities.py` — the Temporal activity contract

Not called directly by any BFF — invoked by `workflow/workflows.py`
**by string name** (see `CLAUDE.md`'s "Breaking the cycle"). Listed
here because the string name + argument shape *is* a contract between
`workflow/` and `application/`, even though it's not a normal function
call. **This is also the one file in `application/` allowed to import
`customer/` and `account/`** — see `CLAUDE.md`'s "Applying without
being a customer yet".

```python
@activity.defn(name="persist_application")
async def persist_application(
    application_id: UUID,
    applicant_identifier: str,
    customer_id: UUID | None,
    workflow_id: str,
    product_type: str,
    payload: dict[str, Any],
    applicant_name: str,
    applicant_email: str,
    applicant_phone: str,
    amount: Decimal,
) -> None: ...  # INSERTs the applications row -- first step of run(). account_id is not a param; it's always NULL at insert time.

@activity.defn(name="persist_decision")
async def persist_decision(
    application_id: UUID,
    new_status: str,
    actor_role: Literal["underwriter", "manager", "customer", "temporal-admin"],
    decision: str,
    actor_name: str,
    comment: str | None,
) -> None: ...
```

`persist_decision` handles all four decision outcomes
(APPROVE/REJECT/REQUEST_MORE_INFO/CANCELLED) in one activity, writing
`underwriter_name`/`underwriter_comment`/`underwriter_decided_at` or
`manager_name`/`manager_comment`/`manager_decided_at` depending on
`actor_role` — same activity, column choice branches on the argument.

**When `new_status == "APPROVED"`** (a terminal approval — the
Underwriter's below-threshold approve, or the Manager's approve after
escalation; *not* the intermediate `PENDING_MANAGER_APPROVAL` step),
`persist_decision` additionally, in the same DB transaction as the rest
of its write:

1. Re-reads the current row. **If `account_id` is already set, this is
   a Temporal retry of an already-completed execution — skip steps 2–5
   entirely** and just (idempotently) re-apply the status/decision
   columns.
2. If `customer_id` is still `NULL`, calls
   `customer.service.get_or_create(applicant_identifier) -> Customer`.
3. Calls `account.service.create_account(customer_id, product_type) ->
   Account`. Not conflict-checked again here — `check_decision_allowed`
   already ran before this decision was ever signaled; this call
   trusts that, with `db/schema.sql`'s partial unique index as the only
   backstop against the (accepted, documented) race window.
4. Calls `document.service.promote_government_id_to_customer_photo(application_id,
   customer_id)` and `document.service.generate_welcome_letter(account_id,
   customer_id, applicant_name, product_type, amount)`.
5. Writes `status`, the decision columns, `customer_id` (if newly
   resolved), and `account_id` (newly created) together.

```python
@activity.defn(name="persist_resubmit")
async def persist_resubmit(
    application_id: UUID,
    payload: dict[str, Any],
) -> None: ...  # sets status back to PENDING_UNDERWRITING, updates payload
```

---

## Open items this spec surfaces (not yet decided — flag before Phase 6/4)

- **Exception contract.** None of the above signatures show what gets
  raised on a not-found id, a permission-shaped failure, or a Temporal
  RPC error. `bulk_signal_decision`'s `BulkActionResult.error` implies
  `signal_decision` raises a typed exception it catches — that
  exception type isn't named yet. Worth pinning down in Phase 4 (P4-4)
  rather than improvising per-call.
- **`decision` as `str` literal vs. enum.** Shown as `Literal[...]`
  above for readability; the real implementation should probably use a
  shared `enum.StrEnum` (defined where? `workflow/` is the natural
  owner since both `application/`'s activities and `bff_backoffice`
  need it) so a typo doesn't silently become a new unrecognized status.
- **`page_size` default of 10** matches PRD §8.2's "paginated list (10
  rows/page)" for staff screens — confirm this is also right for the
  customer's own "My Applications" list (PRD doesn't specify a page
  size there).
- **`persist_decision`'s re-read-then-branch (step 1 above) is a
  read-then-write across two statements**, not a single atomic
  `UPDATE ... WHERE account_id IS NULL`. Worth confirming in Phase 6
  whether that's tight enough for a single-worker POC or whether it
  should be a conditional `UPDATE` to close a theoretical race between
  two concurrent activity executions for the same application (which
  shouldn't happen under normal Temporal scheduling, but "shouldn't
  happen" is worth being deliberate about, not assumed).
- **`promote_government_id_to_customer_photo`'s one-document-two-leaves
  assumption is unverified** — see `CLAUDE.md`'s "Document hierarchy"
  note flagging this for empirical confirmation in Phase 5 before
  Phase 6 builds on top of it. If it doesn't hold, this function's
  contract changes from "re-tag" to "copy," which is a bigger Mayan
  operation (fetch the file, create a second document, upload it) —
  worth knowing before, not after, `persist_decision`'s tests are
  written against the re-tag assumption.
