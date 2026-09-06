# Research note: NATS.io

Not used anywhere in this codebase today — this is reference material
from an ad hoc research session (2026-09-06), kept in case a future
phase considers it. `workflow/` in this project uses Temporal for
orchestration, not a message broker; nothing here proposes replacing
that.

## What it is

NATS is an open-source, high-performance messaging system for
distributed systems. CNCF incubating project, 18K+ GitHub stars, 400M+
downloads. Ships as a single lightweight binary (~15MB memory
footprint) that handles pub/sub, request/reply, queueing, streaming,
key-value storage, and object storage all in one system.

## Core pieces

- **NATS Server** — the core messaging engine: one binary, small
  client API, sub-millisecond latency, millions of msgs/sec.
- **JetStream** — built-in persistence layer on top of the (otherwise
  fire-and-forget) core: durable streaming, at-least-once/exactly-once
  delivery, replay.
- **Location transparency** — clients don't need to know network
  topology; NATS handles service discovery, load balancing, failover.
- **Leaf nodes & superclusters** — topology grows organically (edge →
  regional → global) without downtime; popular for multi-cloud and
  edge/IoT.
- **Multi-tenancy** — built-in account isolation for secure
  multi-tenant use on one server/cluster.
- **Clients** — official Go, Rust, JS, Python, Java, C#; 30+ community
  clients.

## How it compares

| | NATS | Kafka | RabbitMQ |
|---|---|---|---|
| Latency | Sub-millisecond (5–10x lower than Kafka) | <10ms p99 | Moderate |
| Throughput | Very high, lower per-broker ceiling than Kafka | 1M+ msgs/sec/broker | 10K–100K msgs/sec |
| Operational complexity | Lowest — no ZooKeeper/BookKeeper/schema registry, binary is the broker | Highest | Moderate |
| Strength | Simple, fast pub/sub for microservices, IoT, edge | Event streaming, log aggregation, analytics at scale | Flexible routing (topic exchanges, priority queues) |
| Persistence | Optional via JetStream, speed-first by default | Disk-based, durability-first | Configurable, in between |

Rough consensus: Kafka for high-throughput event streaming/analytics,
RabbitMQ for complex routing at moderate scale, NATS when simplicity
and low latency matter most.

## Where this could matter for this project, if ever revisited

Purely speculative, not scoped or decided — listed only because they're
the two places this codebase currently does the kind of thing NATS is
good at:

- `notifications/service.py`'s fake/real (Phase 20) email delivery is
  a single fire-and-forget send with no queueing — a real notification
  fan-out (SMS, push, multiple providers) would be a natural NATS
  pub/sub use case, but this POC has no such requirement today.
- `workflow/` already uses Temporal for durable orchestration, which
  overlaps with what JetStream provides for streaming — no reason to
  introduce a second system for the same job unless a future
  requirement needs pub/sub fan-out Temporal doesn't naturally give
  (e.g., broadcasting a status change to several independent
  subscribers rather than one workflow's own signal/activity chain).

## Links

- Docs: https://docs.nats.io
- Install/quick start: https://docs.nats.io/running-a-nats-service/introduction/installation
- Comparison page: https://docs.nats.io/nats-concepts/overview/compare-nats
