---
name: document-reconciliation
description: loan-onboarding-poc's reconcile.py composition root -- detecting Mayan documents whose Postgres owner row (customer/account/application) no longer exists, the orphaned-vs-stale-tag distinction, and --report/--fix modes. Also covers why cascade-on-delete is deliberately not built. Triggers on "reconcile.py", "reconciliation", "orphaned document", "stale customer_id tag", "list_all_documents", "cascade on delete", "--report --fix", "drift detection".
---

## Document/database reconciliation

**A real, live-observed gap, not a hypothetical one**: `loan_onboarding`
(Postgres) and Mayan are two completely independent systems with no
foreign key, no cascade, and no transaction spanning them — the only
link is a plain string (`applicant_identifier`/`application_id`/
`account_id`/`customer_id`) attached to a Mayan document as metadata
(`document/service.py`'s `upload`/`generate_welcome_letter`/etc., see
"Document hierarchy" above). Nothing enforces that string actually
still resolves to a Postgres row. Confirmed live: `loan_onboarding`'s
three domain tables were cleared (by something outside this app
entirely — a script or process with direct database access, not any
code path this codebase owns) while Mayan's documents were completely
unaffected, leaving real orphaned documents (`application_id`/
`account_id` values pointing at rows that no longer existed) with
nothing in the codebase able to detect, let alone fix, that on its own.

**Two related but genuinely different problems, addressed separately —
don't conflate them**:

1. **Drift detection / reconciliation** (this section, built): Postgres
   and Mayan can each be modified independently of the other, by
   anything with direct access to either — not just this app. The only
   way to catch that is to periodically (or on-demand) walk every Mayan
   document and check whether the Postgres row it claims to belong to
   still exists. Nothing about *how* the row disappeared matters — a
   direct `DELETE`/`TRUNCATE`, a bug, an operator mistake, all look
   identical from Mayan's side: metadata pointing at nothing.
2. **Cascade-on-delete** (planned, not built yet — see Known Gaps):
   when *this app itself* deletes a `customer`/`account`/`application`
   row through its own service layer, the documents that belonged to it
   should go too. This only ever fires for deletes that go through
   `service.py` — it does nothing for the kind of external, direct-DB
   modification that reconciliation (above) exists to catch. Also
   presently blocked on a real, unresolved product question: there is
   no delete operation for any of these three entities in this codebase
   today, and whether a loan-onboarding system should ever hard-delete
   an approved customer/account/application (audit-trail implications)
   versus something like a status change is an open question, not yet
   decided.

**Reconciliation mechanism**: `loan_onboarding/reconcile.py`, a third
composition root alongside `app.py`/`worker_main.py` (see "Repo
layout") — the only files in this codebase allowed to import from every
domain module, because this is fundamentally a cross-cutting concern no
single module's own leaf-purity should absorb. `customer/`/`account/`
stay pure leaves; `reconcile.py` reaches into `customer/`, `account/`,
`application/`, and `document/` all at once, same as `app.py` already
does for the two BFFs.

For every document `document.service.list_all_documents()` returns
(a new, unfiltered public wrapper over the existing private
`_documents_matching({})` — an empty filter dict already matches every
document, that path just wasn't exposed before):

- **A document's primary owner** is whichever id its document type
  actually keys on — `application_id` for an Application Document
  (Government ID, Proof of Income, Bank Statements, Credit Report,
  Property Appraisal, Vehicle Title/Invoice), `account_id` for an
  Account Document (Welcome Letter, Consent). If that id doesn't
  resolve via the owning module's own `service.get(...)` (catching the
  `NotFound` each module already raises — `ApplicationNotFound`,
  `AccountNotFound` — no new "exists" check needed anywhere), the
  document is **orphaned**: its primary owner is gone, so the document
  itself should go.
- **`customer_id` is a secondary tag, not a primary owner** — only ever
  present on a promoted `id_photo` document (`document/`'s
  `promote_government_id_to_customer_photo`, Phase 14), layered on top
  of that document's own real ownership via `application_id`. A stale
  `customer_id` (the referenced `customers` row is gone, but the
  document's own `application_id` still resolves fine) is narrower than
  an orphan — deleting the whole document over a stale *secondary* tag
  would be wrong when its primary ownership is still intact. This is a
  **stale tag**, fixed by stripping just that one metadata entry
  (`mayan_client.delete_metadata_entry`, already built in Phase 14 for
  exactly this shape of operation), not by removing the document.

**Two modes, `--report` (default) and `--fix`**: `--report` scans and
prints findings, mutating nothing — safe to run at any time, including
production, to see what's actually orphaned before deciding to act.
`--fix` additionally moves every orphaned document to Mayan's trash
(`DELETE /documents/{id}/` — soft-delete, reversible, same as this
project's existing "moves to Mayan's trash, not a hard delete" note)
and strips every stale `customer_id` tag, then rebuilds the index once
at the end (same "rebuild once, not per-document" discipline every
other multi-document `document/service.py` operation already follows).

**Live-verified against a real orphaned state, not a synthetic one**
(27 real orphaned documents plus a deliberately constructed stale-tag
case; `--report` correctly separated the two categories, `--fix`
correctly cleaned up both) — full sweep moved to
`IMPLEMENTATION_PLAN.md`'s Session Log, 2026-09-04 docs-consolidation
entry.

