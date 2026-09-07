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

## Where this could matter for this project, if ever revisited

Purely speculative, not scoped or decided:

- `risk/`'s Phase 21 design already anticipates this exact gap
  (pure-NATS both directions for the mock Risk Engine, an HTTP gateway
  as "a separate, later enhancement, not scoped or designed yet") —
  KrakenD is one concrete option for that gateway, evaluated here only
  because the user asked, not chosen.
- If ever built, it would run as its own Docker Compose service
  (alongside `nats`/`mock-risk-engine`/`risk-listener`), sitting
  *outside* `risk/` entirely — `risk/nats_client.py` and
  `risk/service.py` would be unaffected either way, since the whole
  point of the gateway is translating on the *other* side of the NATS
  subject, toward a real external system this codebase doesn't own
  (same "Mayan/Keycloak are real external systems" framing `CLAUDE.md`
  already uses).

## Links

- Homepage: https://www.krakend.io/
- Publisher/Subscribe with Kafka, NATS and cloud systems: https://www.krakend.io/docs/backends/pubsub/
- Non-REST Connectivity: https://www.krakend.io/docs/non-rest-connectivity/
- HTTP Client Plugins: https://www.krakend.io/docs/extending/http-client-plugins/
- Deploying — Best Practices: https://www.krakend.io/docs/deploying/
