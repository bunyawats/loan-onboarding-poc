# Application modules diagram — Python package dependency graph

Source of truth: `CLAUDE.md`'s "Module dependency graph" (the ASCII
version, with the exact per-module rules). This is the same graph,
rendered — useful to check "is this import allowed?" at a glance
without parsing the ASCII art. **Read direction: `A --> B` means "A
imports B."** A dashed arrow marks the one narrow, deliberate exception
to the otherwise-strict layering.

```mermaid
graph TD
    appPy["app.py<br/>(composition root)"]
    workerMain["worker_main.py<br/>(composition root)"]

    bffCustomer["bff_customer/"]
    bffBackoffice["bff_backoffice/"]

    application["application/"]

    customer["customer/"]
    account["account/"]
    document["document/"]
    workflow["workflow/"]
    idgen["idgen/"]

    appPy --> bffCustomer
    appPy --> bffBackoffice

    workerMain --> workflow
    workerMain -->|"application/activities.py's concrete functions"| application

    bffCustomer --> application
    bffCustomer --> document
    bffCustomer --> workflow
    bffCustomer -->|"read-only: find_by_identifier"| customer
    bffCustomer -->|"provisional application_id"| idgen

    bffBackoffice --> application
    bffBackoffice --> document
    bffBackoffice --> workflow
    bffBackoffice --> customer
    bffBackoffice --> account

    application -->|"service.py + activities.py"| document
    application -->|"service.py + activities.py"| workflow
    application --> idgen
    application -.->|"activities.py ONLY -- approval-time provisioning"| customer
    application -.->|"activities.py ONLY -- approval-time provisioning"| account
    customer --> idgen
    account --> idgen
```

## Reading this diagram

- **`document/`, `workflow/`, `idgen/` have no outgoing arrows** —
  they're leaves. `idgen/` is the plainest of all (one pure function,
  zero I/O, zero state); `document/` and `workflow/` are leaves with
  one external dependency each (Mayan, Temporal respectively) but no
  internal one.
- **`customer/` and `account/` have exactly one outgoing arrow each —
  to `idgen/`, for primary-key generation — and nothing else.** That's
  the one exception to "leaves import nothing" these two modules get:
  `idgen/` is deliberately so minimal (no I/O, no other module's types)
  that depending on it doesn't compromise the "pure data module" claim
  `CLAUDE.md` makes about `customer/`/`account/` elsewhere.
- **The dashed arrows are the whole point of this diagram.**
  `application/service.py` never imports `customer/` or `account/` —
  only `application/activities.py` does, and only for the
  approval-time provisioning sequence (`CLAUDE.md`'s "Applying without
  being a customer yet"). Every other arrow here is an ordinary,
  unconditional import; this pair is the one place in the whole
  codebase where *which file inside a module* matters for whether an
  import is allowed, not just *which module*. An import-linter contract
  (`CLAUDE.md`'s "Enforcing the boundaries", `IMPLEMENTATION_PLAN.md`
  Phase 8) has to encode this file-level distinction, not just a
  module-level one.
- **`bff_customer/` and `bff_backoffice/` never import each other** —
  no arrow between them, and neither is a source for the other. Both
  feed into `app.py` (the web process's composition root), not into
  one another.
- **`app.py` and `worker_main.py` are the only nodes with no incoming
  arrows** — nothing imports a composition root; they're where the DAG
  terminates, "the one file allowed to know about everything"
  (`CLAUDE.md`).
- **No cycle anywhere** — this is what "Breaking the application ↔
  workflow cycle" (`CLAUDE.md`) was solving for: `application/` needs
  to *start* a workflow and `workflow/`'s activities need to *write*
  application data, but the diagram shows only `application/ -->
  workflow/`, never the reverse — `workflow/workflows.py` calls
  activities by string name instead of importing
  `application/activities.py` directly.
