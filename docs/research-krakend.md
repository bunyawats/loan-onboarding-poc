# Research note: KrakenD

Not used anywhere in this codebase today — this started as reference
material from an ad hoc research session (2026-09-07). **Superseded by
a real decision later the same day**: KrakenD is now the chosen gateway
for Phase 21's Risk-Engine boundary, alongside a new standalone NATS
Adapter service — see `CLAUDE.md`'s "Automated risk assessment via
NATS" for the authoritative, current design. This file's general
KrakenD research (below) still stands; its own speculative topology
sketch has been updated to show the decided shape instead of the three
options it originally weighed, since the decision it was weighing has
now been made.

## What it is

KrakenD is a stateless, high-performance API gateway written in Go,
distributed as a single binary. No shared state between instances, so
it scales horizontally by just running more of them. Independent
benchmarks cite 80K+ req/s on commodity hardware. Two editions:
**Community** (open source, free, full gateway functionality) and
**Enterprise** (adds SLAs, advanced security/AI features, support) —
both share the same core engine.

## Core capabilities

- **Traffic/security**: rate limiting, circuit breakers, bot detection,
  IP filtering, JWT/OAuth2/OIDC/mTLS/API-key auth.
- **Data handling**: protocol conversion (REST/SOAP/JSON/XML), API
  composition/aggregation (fan out to multiple backends, merge
  responses), field filtering/response shaping.
- **Backend connectivity**: REST, gRPC, GraphQL, and pub/sub systems —
  Kafka, RabbitMQ, AWS SNS/SQS, Azure Service Bus, GCP Pub/Sub, and
  **NATS**.
- **Ops**: OpenTelemetry (logs/metrics/traces), GitOps-friendly
  declarative JSON config, OpenAPI generation.
- **Config-first, not plugin-first**: driven by a JSON config file (a
  "designer" web tool exists to build it visually) rather than custom
  code; an HTTP-client plugin extension point exists for cases the
  config model doesn't cover.

## NATS pub/sub, specifically

A KrakenD backend entry can be typed as a NATS publisher or subscriber
via `extra_config`:

```json
{
  "host": ["nats://"],
  "url_pattern": "/ignored",
  "disable_host_sanitize": true,
  "extra_config": {
    "backend/pubsub/subscriber": { "subscription_url": "mysubject" }
  }
}
```

(swap to `backend/pubsub/publisher`/`topic_url` for the send
direction). The actual NATS server address comes from a
`NATS_SERVER_URL` env var, not the config file. This turns a plain
REST endpoint into "push a message onto a NATS subject" or "return the
latest message off a subject" — the general mechanism this project's
own KrakenD research considered and ultimately **didn't use**: the
decided Phase 21 design (see below) keeps all NATS-protocol knowledge
inside one dedicated service (the NATS Adapter) and uses KrakenD purely
as a plain HTTP↔HTTP gateway instead, specifically so KrakenD itself
never needs a NATS dependency. Known limitation of this feature, for
whenever it *is* the right fit elsewhere: NATS subjects don't support
query-parameter-style config the way some other KrakenD backends do.

## The decided topology (not speculative anymore)

This section originally sketched three options for fitting KrakenD into
Phase 21, unresolved, because at the time it was written the design had
the mock Risk Engine speaking NATS directly and left "what does a real,
REST/webhook-only engine look like" fully open. **The user resolved
this the same day**: instead of asking KrakenD to bridge NATS↔HTTP
itself (which its own request/response model can't do cleanly for the
submission leg — see the original reasoning kept below), NATS
connectivity moved entirely into one new standalone service, the **NATS
Adapter** (`risk-adapter`). The Risk Engine (mock, and any real one
later) never touches NATS at all, in either direction — only plain
HTTP, all of it fronted by KrakenD:

```
application/activities.py       risk-adapter (NATS Adapter)              KrakenD              mock-risk-engine
  submit_risk_assessment ──HTTP──► POST /assessments
                                          │
                                  publish: risk.assessment.submitted ──► nats
                                          │
                              (risk-adapter's own subscriber loop)
                                          │
                                          ▼
                                  POST /assess ──────────────────────► (routes) ──────────────►  POST /assess
                                                                                                        │
                                                                                                  (simulated delay,
                                                                                                   buckets on amount)
                                                                                                        │
                                  POST /decisions (webhook) ◄────────── (routes) ◄─────────────  POST /decisions
                                          │
                                  publish: risk.assessment.decided ──► nats
                                          │
                              (risk-adapter's own subscriber loop)
                                          │
                                          ▼
                                  signal_risk_decision(tier)  ──── direct Temporal client, no
                                                                    import of workflow.service
```

This matches what the third option below (once labeled "Option 2")
already anticipated — a small, separate, always-on NATS consumer
bridging the submission leg — except it's now decided, named
(`risk-adapter`), and owns *both* legs end to end rather than splitting
NATS-awareness between KrakenD and a bridge. KrakenD's own job shrank to
exactly its best-supported case: plain HTTP↔HTTP reverse-proxying,
never touching its NATS pub/sub backend feature at all (using that
feature would have given KrakenD its own NATS dependency, which the
decided design explicitly rules out — "only the Adapter depends on
NATS").

**The original reasoning, kept for context on *why* a pure-KrakenD
bridge wasn't chosen:**

KrakenD's own model (an inbound HTTP request triggers a backend action
and a response) covers one direction of a NATS↔HTTP bridge cleanly, the
other needs a caveat:

- **The decision-callback leg (webhook in → NATS publish) is a clean
  fit.** An inbound webhook POST is exactly what KrakenD is built
  around — a `backend/pubsub/publisher` backend on that endpoint
  publishes straight onto a NATS subject.
- **The submission leg (NATS → outbound HTTP call) doesn't fit as
  neatly.** Everything KrakenD does is *inbound-HTTP-request-triggered*
  — nothing in KrakenD's docs lets it sit idle, watch a NATS subject on
  its own, and fire an outbound HTTP call when a message arrives. That
  would have needed either a polling-capable Risk Engine (it calls a
  KrakenD-fronted `GET /risk/pending` itself) or a small, separate
  always-on NATS consumer doing exactly what `risk-adapter` now does.

Given the Risk Engine was always going to be push-style (a submission
call + a callback), not polling, the decided design folds *both* legs
into one purpose-built Adapter service rather than splitting the work
between KrakenD (for the leg it's good at) and a bridge (for the leg
it's not) — simpler to reason about, and it's what actually shipped in
`CLAUDE.md`'s design.

## Links

- Homepage: https://www.krakend.io/
- Publisher/Subscribe with Kafka, NATS and cloud systems: https://www.krakend.io/docs/backends/pubsub/
- Non-REST Connectivity: https://www.krakend.io/docs/non-rest-connectivity/
- HTTP Client Plugins: https://www.krakend.io/docs/extending/http-client-plugins/
- Deploying — Best Practices: https://www.krakend.io/docs/deploying/
