# System architecture diagram — local dev / Docker Compose topology

Source of truth: `CLAUDE.md`'s "Deployment" and "Docker Compose
topology (local dev)" sections. This is the *process/container* view —
one Docker image, multiple running processes, several third-party
containers. For the *code-module* view (which Python package imports
which), see [`application-modules.md`](application-modules.md).

```mermaid
graph TB
    customerBrowser["Customer<br/>(phone browser)"]
    staffBrowser["Underwriter / Manager<br/>(desktop browser)"]

    subgraph img["One Docker image (loan_onboarding)"]
        app["app<br/>(uvicorn: bff_customer + bff_backoffice)"]
        workerWorkflow["worker-workflow<br/>(worker_main.py, WORKER_MODE=workflow)"]
        workerActivity["worker-activity<br/>(worker_main.py, WORKER_MODE=activity)"]
    end

    subgraph pg["db (one Postgres container)"]
        loanDb[("loan_onboarding<br/>customers / accounts / applications")]
        temporalDb[("temporal<br/>(Temporal's own schema)")]
    end

    subgraph temporalStack["Temporal"]
        temporalServer["temporal server"]
        temporalUi["temporal-ui<br/>(localhost:8233)"]
    end

    subgraph kcStack["Keycloak stack"]
        keycloak["keycloak<br/>(start-dev --import-realm)"]
        backofficeRedis[("backoffice-redis<br/>sessions + bulk-selection store")]
    end

    subgraph mayanStack["Mayan stack (third-party, not app code)"]
        mayan["mayan"]
        mayanDb[("mayan-db")]
        mayanRedis[("mayan-redis")]
    end

    customerBrowser -->|"HTMX, bff_customer routes<br/>signed session cookie"| app
    staffBrowser -->|"HTMX, bff_backoffice routes<br/>Keycloak Authorization Code flow"| app

    app -->|asyncpg| loanDb
    app -->|start/signal workflow| temporalServer
    app -->|upload/preview/search| mayan
    app -->|Authorization Code flow, UMA ticket exchange| keycloak
    app -->|"/ui/* sessions, bulk-selection"| backofficeRedis

    workerWorkflow -->|poll task queue| temporalServer
    workerActivity -->|poll task queue, run activities| temporalServer
    workerActivity -->|"persist_application / persist_decision / persist_resubmit"| loanDb
    workerActivity -->|"tag_application_documents<br/>promote_government_id_to_customer_photo<br/>generate_welcome_letter"| mayan

    temporalServer --> temporalDb
    temporalUi --> temporalServer

    mayan --> mayanDb
    mayan --> mayanRedis
```

## Reading this diagram

- **One image, several processes** — `app`, `worker-workflow`, and
  `worker-activity` all run from the same built image, just started
  with a different entrypoint/`WORKER_MODE` (`CLAUDE.md`'s
  "Deployment"). Not three separately-built artifacts.
- **`worker-activity` is the only process that writes to `loan_onboarding`
  directly** — the web (`app`) process never writes `applications`
  rows itself; it starts/signals the workflow and polls its own read
  path (`_wait_until()`) waiting for the activity worker to commit. See
  `CLAUDE.md`'s "Breaking the application ↔ workflow cycle."
- **`worker-activity` also talks to Mayan** — not just Postgres. The
  account-on-approval provisioning (`CLAUDE.md`'s "Applying without
  being a customer yet") runs inside `persist_decision`, which is
  activity code, hence this process (not `app`) is what calls
  `tag_application_documents`/`promote_government_id_to_customer_photo`/
  `generate_welcome_letter`.
- **Keycloak's `backoffice-redis` and Mayan's `mayan-redis` are
  separate Redis instances** — named distinctly, no shared state
  between the two, matching `CLAUDE.md`'s explicit "not
  `mayan-redis`" callout.
- **Keycloak has no dedicated Postgres** — `start-dev` mode uses an
  in-memory H2 database; not pictured because there's nothing durable
  to show.
- **`docker-compose.yml`'s optional split** (`app-customer` +
  `app-backoffice` as two separate processes/services instead of one
  `app`) isn't drawn here — it's a scaling-profile choice with no
  effect on any arrow in this diagram, since the module boundaries and
  in-process calls underneath are identical either way (`CLAUDE.md`'s
  "Deployment").
