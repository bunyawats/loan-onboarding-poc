# loan-onboarding-poc

A loan onboarding proof of concept: a customer applies for a loan
themself from a mobile-first web app, a standalone mock Risk Engine
auto-decides the clear-cut cases over NATS, and an Underwriter reviews
anything left over — with a Manager giving final sign-off on larger
loans. Built with Python FastAPI, HTMX, Mayan EDMS, Temporal,
PostgreSQL, and Keycloak (staff auth), plus a NATS/KrakenD-fronted
mock Risk Engine for the automated assessment step — as a **modular
monolith**: one deployable Python codebase, organized into seven core
modules plus a few small supporting leaf modules (see `CLAUDE.md`'s
module dependency graph), with the standalone Risk Engine/NATS
Adapter/KrakenD gateway as genuinely separate services it talks to
over HTTP/NATS, not part of that codebase. Module boundaries are
enforced by `import-linter` in CI, not just documented.

## Read these in order

1. **[`PRD.md`](PRD.md)** — what this is: product requirements, roles,
   the approval workflow, the identity model, success criteria.
2. **[`CLAUDE.md`](CLAUDE.md)** — how it's built: module boundaries and
   the dependency rules between them, the Mayan/Temporal/Keycloak
   integration design, data storage, deployment.
3. **[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)** — where things
   actually stand right now: phased, checkbox-tracked tasks, current
   status, and a session log. **This is the file that says what to do
   next.**

Also useful, rendered from `CLAUDE.md`'s ASCII diagrams —
[`docs/api-specification.md`](docs/api-specification.md) (every
module's exact `service.py` signatures) and `docs/diagrams/`
([ER](docs/diagrams/er-diagram.md),
[system architecture](docs/diagrams/system-architecture.md),
[module dependency graph](docs/diagrams/application-modules.md)).

## If you're a coding agent picking this up

Read `IMPLEMENTATION_PLAN.md`'s **"How a session should use this
file"** section before writing any code — it's a short protocol for
resuming work with no memory of prior sessions (where to look for
current status, when to check a box vs. leave a status note, when to
update `CLAUDE.md` vs. just log a decision, commit discipline). Skipping
it is the single easiest way to duplicate work or silently diverge from
the architecture already decided in `CLAUDE.md`.

## Status

All 22 planned phases (0 through 21) are complete, including several
added after the original build-out closed at Phase 12: human-readable
primary keys, returning-customer profile refresh, document/database
reconciliation, account closure, Welcome Letter email, real Gmail SMTP
delivery, and — most recently — automated risk assessment via NATS
(Phase 21), which routes clear-cut low/high-risk applications straight
to an auto-decision and leaves only the ambiguous middle for human
underwriting. `IMPLEMENTATION_PLAN.md`'s own backlog is empty again;
remaining work is limited to the accepted, documented limitations in
`CLAUDE.md`'s Known Gaps section (e.g. Phase 21's risk-tier thresholds
currently make the Manager-escalation path practically unreachable —
a known, not-yet-fixed interaction between two independently-tuned
thresholds). See `IMPLEMENTATION_PLAN.md`'s **Current Status** for the
full phase-by-phase history and session log.

## Reference projects

This design deliberately reuses validated patterns from two existing
projects rather than inventing from scratch — see `CLAUDE.md`'s opening
section for specifics on what's borrowed from where:

- [`review-approval-temporal`](https://github.com/bunyawats/review-approval-temporal)
- [`mayan-edms-customer-archive`](https://github.com/bunyawats/mayan-edms-customer-archive)
