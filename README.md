# loan-onboarding-poc

A loan onboarding proof of concept: a customer applies for a loan
themself from a mobile-first web app, an Underwriter reviews it, and a
Manager gives final sign-off on larger loans. Built with Python
FastAPI, HTMX, Mayan EDMS, Temporal, and PostgreSQL, as a **modular
monolith** — one deployable, seven strictly-bounded internal modules.

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

Build-out is substantially complete (all 16 planned phases done) — see
`IMPLEMENTATION_PLAN.md`'s **Current Status** for exactly where things
stand and what, if anything, is next.

## Reference projects

This design deliberately reuses validated patterns from two existing
projects rather than inventing from scratch — see `CLAUDE.md`'s opening
section for specifics on what's borrowed from where:

- [`review-approval-temporal`](https://github.com/bunyawats/review-approval-temporal)
- [`mayan-edms-customer-archive`](https://github.com/bunyawats/mayan-edms-customer-archive)
