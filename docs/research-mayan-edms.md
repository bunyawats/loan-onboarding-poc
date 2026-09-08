# Research note: Mayan EDMS

Like `research-temporal-io.md`, this isn't speculative background —
Mayan EDMS is already this project's `document/` module
(`mayan_client.py` + `service.py`), running as its own `mayan`/
`mayan-db`/`mayan-redis` containers per `docker-compose.yml`
(`mayanedms/mayanedms:latest`). This note is a grounding reference,
condensed from `mayan-edms.com`/`docs.mayan-edms.com` (the docs site
403s automated fetches directly, so this is built from search-indexed
excerpts of the same pages plus the GitHub mirror), closing with where
each concept already shows up in this codebase.

## What it is

Mayan EDMS is a free, open-source, Django-based document management
system — storing, introspecting, and categorizing files with an
emphasis on preserving each document's contextual and business
metadata, not just the file itself. Built on PostgreSQL (storage),
Redis (caching/locking), and Celery (async task processing — OCR,
index rebuilds, thumbnail generation all run as background jobs, not
inline with the request).

## Core pieces

- **Document / Document Type** — a Document Type is a category
  ("Invoice", "Contract") that determines which Metadata Types a
  document of that type is allowed to carry, and can trigger its own
  automation (OCR, workflow) on upload.
- **Metadata Types** — user-defined key/value fields attachable to
  documents, matched to a Document Type as needed (Dublin Core, ISO
  23081, or fully custom). A document can only be attached a Metadata
  Type its Document Type has been explicitly associated with — attach
  one it isn't associated with and Mayan rejects the call outright.
- **Tags & Cabinets** — two lighter-weight organizing mechanisms:
  color-coded Tags (many-to-many, no hierarchy) and Cabinets
  (user-curated folder trees), independent of the Index system below.
- **Indexes / Index Templates** — the automatic, hierarchical
  organizing mechanism: an administrator defines a *template* (a tree
  of branches, each branch's placement rule evaluated as a Python
  expression against a document's metadata/properties), and Mayan
  auto-populates an *instance* of that tree with links to matching
  documents. Indexes update automatically as metadata/tags/document
  type change; a *structural* change to the template itself needs an
  explicit "Rebuild Index" action (async, via Celery).
- **Workflows** — Mayan's own feature, and worth being careful not to
  confuse with Temporal Workflows (`research-temporal-io.md`) despite
  the identical name: a Mayan Workflow is a finite state machine
  attached to a Document Type — a fixed set of named states, one
  initial state, and Transitions between them (manual, user-triggered,
  or automatically triggered by a system event). A State can fire an
  action on entry/exit — including an HTTP POST to an external system.
  This is a document-lifecycle feature *internal to Mayan*, unrelated
  to a real Temporal Workflow orchestrating a business process outside
  it.
- **ACLs** — per-object role-based permissions (who can view, edit,
  attach metadata to, or transition a specific document/cabinet/etc.).
- **OCR** — automatic text extraction (Tesseract by default, pluggable
  backend), distributable across workers, feeding full-text search.
- **REST API** — the entire product surface (upload, metadata, search,
  workflows) is REST-accessible; this is the *only* interface this
  project's `document/` module talks to (`mayan_client.py` — no direct
  DB or Celery access).

## Core pieces this project doesn't use

- **Mayan's own Workflow engine** — this project's loan-approval
  lifecycle is entirely a Temporal Workflow (`workflow/`); Mayan is
  used purely for document storage/metadata/indexing, never for
  document-state automation.
- **Tags and Cabinets** — the three Index Templates (below) are this
  project's only organizing mechanism; Tags/Cabinets aren't used.
- **Digital signatures**, smart links — not part of this project's
  scope.

## How this project uses it

- **Three Index Templates, exclusive placement**: Customer Index,
  Account Index, Application Index — each rooted at a different entity
  id a document can carry (`applicant_identifier`/`application_id`/
  `account_id`/`customer_id`), with a document living at exactly one
  leaf per index, the deepest entity it's actually tied to. Built by
  `scripts/setup_document_hierarchy.sh` (one-time, not idempotent). See
  the `document-hierarchy` skill for the full tree diagrams and five
  real index-template gotchas hit building this (e.g. a leaf condition
  doesn't inherit an ancestor branch's match — every descendant
  document needs the ancestor's metadata field on itself too, or it
  falls into a top-level "None" bucket instead of its real branch).
- **Two Document Types**: `DOCUMENT_TYPE_APPLICATION` ("Application
  Document") and `DOCUMENT_TYPE_ACCOUNT` ("Account Document") —
  `document/mayan_client.py`'s constants, each associated with the
  five Metadata Types below so `attach_metadata` calls don't hit
  Mayan's "not associated with this document type" rejection.
- **Five Metadata Types**, named as constants in `mayan_client.py`
  (`METADATA_FIELD_APPLICANT_IDENTIFIER`/`_APPLICATION_ID`/
  `_ACCOUNT_ID`/`_CUSTOMER_ID`/`_CATEGORY`) — attached at upload time
  (`applicant_identifier`/`application_id`/`category` always;
  `customer_id` once resolvable), then `tag_application_documents`
  attaches `account_id`+`customer_id` to every document under an
  application once it's `APPROVED` (CLAUDE.md's "Document metadata
  assignment lifecycle").
- **Celery-driven, deliberately async index rebuilds**: `document/`
  never reads the Index Template tree from application code
  (`check_completeness`/`list_*_documents` query Mayan's metadata
  search API directly instead) — the tree exists purely for staff to
  browse visually, and is async by nature. `mayan_client.rebuild_index()`
  is called after every metadata attach; overlapping calls can race
  Mayan's own reset-then-rebuild sequence (an operating rule the
  `document-hierarchy` skill documents).
- **Service-account auth, not per-user** — one shared Mayan service
  account (`MAYAN_SERVICE_ACCOUNT_USERNAME`/`_PASSWORD`), token
  obtained lazily and refreshed on a 401; neither BFF has its own Mayan
  credentials, both go through `document.service`.

## Links

- Product site: https://www.mayan-edms.com/
- Docs (blocks automated fetches directly — search-indexed excerpts
  only): https://docs.mayan-edms.com/
- Features overview: https://docs.mayan-edms.com/chapters/features.html
- Indexes: https://docs.mayan-edms.com/ (search "index templates")
- Workflows (Mayan's own, not Temporal's):
  https://docs.mayan-edms.com/apps/document_states/index.html
- Source: https://gitlab.com/mayan-edms/mayan-edms (canonical; the
  `github.com/mayan-edms/Mayan-EDMS` mirror is noted as outdated)
