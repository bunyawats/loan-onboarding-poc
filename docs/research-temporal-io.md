# Research note: Temporal

Unlike `research-nats-io.md`/`research-krakend.md`, this isn't
speculative background for a future decision — Temporal is already
this project's `workflow/` module, running today
(`LoanApplicationWorkflow`, `CloseAccountWorkflow`). This note is a
grounding reference: what Temporal actually is and does, condensed
from `temporal.io`/`docs.temporal.io`, with a closing section mapping
each concept to where it already shows up in this codebase.

## What it is

Temporal is an open-source (MIT-licensed), durable execution platform:
software that keeps a long-running piece of business logic (a
**Workflow**) running to completion across crashes, deploys, and
outages, without the application writing its own recovery logic. ~9
years in production lineage (architects from AWS SQS/SWF, Uber
Cadence); Temporal Cloud advertises a 99.999% uptime SLA for teams that
don't want to self-host. Official SDKs: Go, Java, Python, TypeScript,
.NET, PHP, Ruby.

The core idea, in Temporal's own framing: write the happy path as
ordinary code, and let the platform own failure — "write code as if
failure doesn't exist."

## Core pieces

- **Workflow** — the orchestrator: a deterministic sequence of steps,
  written in a real language, that decides what happens next and calls
  Activities to make it happen. A **Workflow Execution** is one running
  instance, addressed by a Workflow ID.
- **Activity** — the unit of actual work, and the only place allowed to
  be non-deterministic: an API call, a DB write, a file upload.
  Automatically retried (exponential backoff), should be idempotent,
  supports heartbeats for long-running work.
- **Worker** — a process *you* run; the Temporal Service itself never
  executes your Workflow/Activity code. Polls one or more Task Queues,
  registers as a Workflow Worker, an Activity Worker, or both.
- **Task Queue** — the routing point between the two sides: a Client
  names a queue when starting a Workflow, a Worker names the same
  queue when it starts up; any Worker on that queue can pick up the
  work. Splitting queues by tenant/region/product line is a normal way
  to shape which Worker pool handles what.
- **Event History** — an append-only, ordered log of everything that
  happened to one Workflow Execution (started, Activity scheduled,
  Activity completed, Signal received, timer fired). This *is* the
  Workflow's state — there's no separate snapshot.

## How state survives a crash

Temporal doesn't snapshot a Workflow's variables — it **replays** the
Workflow function from the top against the recorded Event History
until it reaches the same point again, then continues. This is why
Workflow code must be deterministic: no direct `time.now()`, no random
numbers, no raw network calls, nothing that could come out differently
on replay. A Workflow instead reads time from its own execution
context, uses Workflow-aware timers, and delegates every real side
effect to an Activity, whose result is written into the Event History
once and replayed from that recorded result afterward rather than
re-executed.

Practical upshot: a Workflow that's been waiting three days for a
human decision isn't running anywhere during those three days — no
process is holding it open. Any Worker, anywhere, can pick it back up
the instant a Signal arrives.

## Signals, Queries, Updates

| Mechanism | Direction | Blocks caller? | Typical use |
|---|---|---|---|
| **Signal** | write into the Workflow | no — fire and forget, no return value | "an Underwriter approved this" |
| **Query** | read out of the Workflow | yes, but cheap — never touches Event History | "what status is this in right now?" |
| **Update** | write, with a result | yes — caller awaits a real return value | a Signal + Query in one round trip |

Signal handlers are where most day-to-day Workflow business logic
lives — the hook a long-lived Workflow uses to react to something
arriving from outside, days or weeks after it started.

## Retry policies

Activities retry automatically; Workflows don't (replaying a whole
execution isn't a meaningful response to most failures, since
determinism means it would just fail the same way again). Defaults:

| Parameter | Default |
|---|---|
| Initial interval | 1 second |
| Backoff coefficient | 2.0× per attempt |
| Maximum interval | 100× initial interval |
| Maximum attempts | unlimited, unless capped |
| Non-retryable errors | none, unless named explicitly |

## Deployment: self-hosted vs. Temporal Cloud

Same Workflow/Activity/Worker programming model either way — only who
runs the Temporal Service changes.

- **Self-hosted**: open source, Docker/Kubernetes/single-binary dev
  server; you own persistence (Postgres/MySQL/Cassandra), visibility
  storage, and TLS.
- **Temporal Cloud**: managed Temporal Service, 99.999% SLA, no
  persistence layer or version upgrades to operate yourself.

## How this project uses it

- **Task Queues keyed by product type** (`workflow/task_queues.py`):
  `task_queue_for_product_type("personal_loan")` →
  `"loan-onboarding-personal_loan-task-queue"` — lets a Worker pool be
  scaled or deployed per loan product. Account closure gets its own
  single, non-product-keyed queue (`task_queue_for_account_closure()`).
- **Activities dispatched by string name, not by import** — the
  mechanism CLAUDE.md calls "Breaking the application ↔ workflow
  cycle": `workflow/workflows.py` calls
  `workflow.execute_activity(ACTIVITY_PERSIST_DECISION, ...)` using a
  shared string constant, matched against whatever
  `application/activities.py` registered under that same name via
  `@activity.defn(name=...)` — this is what lets `workflow/` orchestrate
  the loan-application domain without ever importing it.
  `DEFAULT_RETRY_POLICY` (`maximum_attempts=5`) and
  `DEFAULT_ACTIVITY_TIMEOUT` (30s) are this project's own tuned
  defaults on top of Temporal's own.
- **Signals are the entire decision lifecycle**: `submit_decision`
  (Underwriter/Manager/customer decisions), `signal_risk_decision`
  (the NATS Adapter's automated risk-tier result), `resubmit`
  (customer resubmission after `MORE_INFO_REQUESTED`) — see
  `research-nats-io.md`'s note on Phase 21 for how NATS and Temporal
  divide this specific responsibility.
- **`get_status()` is a Query** — both BFFs poll a running Workflow's
  current status cheaply, without ever touching its Event History.

## Links

- Product overview: https://temporal.io/
- Durable execution concept: https://docs.temporal.io/evaluate/understanding-temporal
- Core concepts: https://docs.temporal.io/workflows, /activities, /workers
- Retry policies: https://docs.temporal.io/encyclopedia/retry-policies
- Signals & Queries: https://docs.temporal.io/encyclopedia/workflow-message-passing
- Task Queues: https://docs.temporal.io/task-queue
- Deployment: https://docs.temporal.io/self-hosted-guide
