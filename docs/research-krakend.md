# Research note: KrakenD

Not used anywhere in this codebase today — this is reference material
from an ad hoc research session (2026-09-07), kept in case a future
phase considers it. `CLAUDE.md`'s "Automated risk assessment via NATS"
section (Phase 21, planned) names "an open-source gateway component
sitting between `risk/nats_client.py` and a *real* (non-mock) Risk
Engine, translating NATS ↔ HTTP" as a later, unscoped enhancement —
KrakenD is a candidate for that role, not a decided choice. Nothing
here proposes building it now.

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
latest message off a subject" — a real, third-party Risk Engine
speaking REST/webhooks could sit behind KrakenD, with KrakenD doing the
REST↔NATS translation against the same `risk.assessment.submitted`/
`risk.assessment.decided` subjects `risk/nats_client.py` already
publishes/subscribes to, with zero change to this codebase's own
NATS-facing code. Known limitation: NATS subjects don't support
query-parameter-style config the way some other KrakenD backends do.

## Sketch: fitting into Phase 21's topology

Purely speculative, not scoped or decided — a sketch from a follow-up
question in the same research session, not a proposal. KrakenD's own
model (an inbound HTTP request triggers a backend action and a
response) covers one of Phase 21's two legs cleanly; the other needs a
caveat, not a clean fit.

**Today's Phase 21 design (pure NATS, both directions, mock engine):**

```
application/activities.py          mock-risk-engine           risk_listener_main.py
  submit_risk_assessment    ──publish──►  risk.assessment.submitted
                                          (subscribes, sleeps to
                                           simulate latency, buckets
                                           on amount)
                                    ──publish──►  risk.assessment.decided ──subscribe──► signal_risk_decision(tier)
```

**With a real, REST/webhook-only Risk Engine behind KrakenD, swapped in
for `mock-risk-engine`'s position:**

```
application/activities.py                    KrakenD                      Real Risk Engine
  submit_risk_assessment ──publish──► risk.assessment.submitted
                                            │
                                    (Leg 1 — see caveat below)
                                            │
                                            ▼
                                    outbound call ─────────────►  POST /assess
                                                                          │
                                                                    (processes async,
                                                                     holds a callback URL)
                                                                          │
                                     POST /webhooks/decision  ◄──────────┘
                                            │
                                    KrakenD publisher backend
                                    (backend/pubsub/publisher,
                                     topic_url: risk.assessment.decided)
                                            │
                                            ▼
                                    risk.assessment.decided ──subscribe──► risk_listener_main.py
                                                                            signal_risk_decision(tier)
```

**Leg 2 (decision callback) is a clean fit.** The real engine's webhook
POST is exactly the inbound-HTTP-request KrakenD is built around — a
`backend/pubsub/publisher` backend on that endpoint publishes the
decision straight onto `risk.assessment.decided`. `risk_listener_main.py`
doesn't change at all.

**Leg 1 (submission) doesn't fit KrakenD's own model as neatly.**
Everything KrakenD does is *inbound-HTTP-request-triggered* — a
`backend/pubsub/subscriber` backend serves a NATS message back as an
HTTP response when something calls it, but nothing found in KrakenD's
docs lets it sit idle, watch a NATS subject on its own, and fire an
outbound HTTP call when a message arrives. Two honest options for that
leg, neither of which is "KrakenD alone, transparently":
1. **If the real engine supports pull/polling** (it periodically calls
   something like `GET /risk/pending`), KrakenD's subscriber backend
   fits perfectly — the engine becomes the HTTP client, KrakenD serves
   it off the subject.
2. **If the engine expects to be pushed to** (the common shape — a
   submission POST + a callback URL), a small, separate always-on NATS
   consumer is needed to bridge `risk.assessment.submitted` → an
   outbound `POST /assess` call. That's not KrakenD's job; it'd be a
   tiny dedicated bridge process alongside it, or hand-rolled the same
   way `risk_listener_main.py` already is.

Either way, `risk/nats_client.py`, `risk/service.py`, and
`risk_listener_main.py` stay untouched — this whole thing replaces only
`mock-risk-engine`'s position in the Compose topology, confined behind
the two NATS subjects already designed (same "Mayan/Keycloak are real
external systems this codebase doesn't own" framing `CLAUDE.md` already
uses elsewhere). It would run as its own Docker Compose service if ever
built, sitting *outside* `risk/` entirely.

## Links

- Homepage: https://www.krakend.io/
- Publisher/Subscribe with Kafka, NATS and cloud systems: https://www.krakend.io/docs/backends/pubsub/
- Non-REST Connectivity: https://www.krakend.io/docs/non-rest-connectivity/
- HTTP Client Plugins: https://www.krakend.io/docs/extending/http-client-plugins/
- Deploying — Best Practices: https://www.krakend.io/docs/deploying/
