#!/usr/bin/env python3
"""Clears ALL loan-onboarding data across the three independent systems
that hold it -- Postgres (applications/accounts/customers), Temporal
(every LoanApplicationWorkflow execution, running or completed), and
Mayan (every document, trashed AND permanently purged). Companion to
`generate_real_e2e_data.py`, which only ever creates data -- clearing is
a separate, deliberate step so review data survives until you ask for
this.

Talks to each system directly (Postgres via asyncpg, Temporal via the
temporalio SDK, Mayan via its REST API) -- no `docker exec`, no
`temporal` CLI binary required, no assumption about how the stack is
hosted beyond the three services being reachable at the URLs/hosts
below. This makes it portable to any machine running this project's
`docker compose` stack (or an equivalent deployment), not just the one
it was first written on.

DESTRUCTIVE. Prompts for confirmation unless run with --yes.

Usage:
    .venv/bin/python3 scripts/clear_e2e_data.py          # asks first
    .venv/bin/python3 scripts/clear_e2e_data.py --yes    # no prompt

Configuration (env vars, all optional -- defaults match this project's
own `docker-compose.yml` as published to the host):
    DATABASE_URL                    default: postgresql://postgres:postgres@localhost:5433/loan_onboarding
    TEMPORAL_HOST                   default: localhost:7233
    TEMPORAL_NAMESPACE              default: default
    MAYAN_BASE_URL                  default: http://localhost:8000
    MAYAN_SERVICE_ACCOUNT_USERNAME  default: admin
    MAYAN_SERVICE_ACCOUNT_PASSWORD  default: changeme
(Same names `.env.example`/`loan_onboarding/document/mayan_client.py`
already use -- if your `.env` sets these for the app, this script picks
up the same values.)

Needs the full stack up (`docker compose up -d`) -- same prerequisite
as `generate_real_e2e_data.py`.
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
import httpx
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
from temporalio.client import Client, WorkflowExecutionStatus

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/loan_onboarding")
TEMPORAL_HOST = os.environ.get("TEMPORAL_HOST", "localhost:7233")
TEMPORAL_NAMESPACE = os.environ.get("TEMPORAL_NAMESPACE", "default")
MAYAN_BASE_URL = os.environ.get("MAYAN_BASE_URL", "http://localhost:8000")
MAYAN_USERNAME = os.environ.get("MAYAN_SERVICE_ACCOUNT_USERNAME", "admin")
MAYAN_PASSWORD = os.environ.get("MAYAN_SERVICE_ACCOUNT_PASSWORD", "changeme")

WORKFLOW_TYPE = "LoanApplicationWorkflow"
# Matches loan_onboarding/document/mayan_client.py's own INDEX_TEMPLATE_SLUGS
# -- rebuilding by slug (not a hardcoded numeric id) so this still works
# against a freshly-provisioned Mayan instance where template ids differ.
INDEX_TEMPLATE_SLUGS = ("customer-index", "account-index", "application-index")


def log(msg: str) -> None:
    print(f"[clear-e2e-data] {msg}", flush=True)


async def clear_postgres() -> dict[str, int]:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    try:
        counts: dict[str, int] = {}
        async with pool.acquire() as conn:
            for table in ("applications", "accounts", "customers"):
                result = await conn.execute(f"DELETE FROM {table}")
                counts[table] = int(result.split()[-1])
        return counts
    finally:
        await pool.close()


async def clear_temporal() -> int:
    client = await Client.connect(TEMPORAL_HOST, namespace=TEMPORAL_NAMESPACE)
    deleted = 0
    async for wf in client.list_workflows(query=f"WorkflowType='{WORKFLOW_TYPE}'"):
        if wf.status == WorkflowExecutionStatus.RUNNING:
            handle = client.get_workflow_handle(wf.id, run_id=wf.run_id)
            await handle.terminate(reason="clearing e2e test data")
        await client.workflow_service.delete_workflow_execution(
            DeleteWorkflowExecutionRequest(
                namespace=TEMPORAL_NAMESPACE,
                workflow_execution=WorkflowExecution(workflow_id=wf.id, run_id=wf.run_id),
            )
        )
        deleted += 1
    return deleted


def clear_mayan(client: httpx.Client) -> tuple[int, int]:
    trashed_count = 0
    while True:
        resp = client.get("/documents/", params={"page_size": 100})
        resp.raise_for_status()
        results = resp.json()["results"]
        if not results:
            break
        for doc in results:
            client.delete(f"/documents/{doc['id']}/")
            trashed_count += 1

    purged_count = 0
    while True:
        resp = client.get("/trashed_documents/", params={"page_size": 100})
        resp.raise_for_status()
        results = resp.json()["results"]
        if not results:
            break
        for doc in results:
            client.delete(f"/trashed_documents/{doc['id']}/")
            purged_count += 1
        # trash listing can lag slightly behind the async purge task --
        # give it a moment before re-checking for the next page.
        import time

        time.sleep(2)

    return trashed_count, purged_count


def rebuild_mayan_indexes(client: httpx.Client) -> None:
    resp = client.get("/index_templates/", params={"page_size": 100})
    resp.raise_for_status()
    by_slug = {t["slug"]: t["id"] for t in resp.json()["results"]}
    missing = [slug for slug in INDEX_TEMPLATE_SLUGS if slug not in by_slug]
    if missing:
        log(f"  warning: index templates not found (skipping rebuild): {missing}")

    for slug in INDEX_TEMPLATE_SLUGS:
        if slug not in by_slug:
            continue
        idx_id = by_slug[slug]
        client.post(f"/index_templates/{idx_id}/rebuild/")
        # One unhurried rebuild at a time, waited out to stability --
        # firing the next one before this finishes is a real, previously
        # observed source of a stuck/incomplete tree (CLAUDE.md's
        # "Document hierarchy" section documents the exact same race).
        import time

        prev = None
        stable = 0
        for _ in range(20):
            r = client.get(f"/index_instances/{idx_id}/")
            r.raise_for_status()
            cur = r.json()["node_count"]
            if cur == prev:
                stable += 1
                if stable >= 3:
                    break
            else:
                stable = 0
            prev = cur
            time.sleep(2)
        log(f"  rebuilt {slug}: node_count={prev}")


async def main() -> None:
    if "--yes" not in sys.argv:
        print("This will PERMANENTLY delete ALL applications/accounts/customers (Postgres),")
        print(f"ALL {WORKFLOW_TYPE} executions (Temporal), and ALL documents, trashed AND")
        print("purged (Mayan). This cannot be undone.")
        answer = input("Type 'yes' to continue: ")
        if answer.strip().lower() != "yes":
            print("Aborted -- nothing was touched.")
            sys.exit(1)

    log(f"Postgres: {DATABASE_URL.split('@')[-1]}")
    pg_counts = await clear_postgres()
    log(f"  deleted {pg_counts}")

    log(f"Temporal: {TEMPORAL_HOST} (namespace={TEMPORAL_NAMESPACE})")
    wf_count = await clear_temporal()
    log(f"  deleted {wf_count} {WORKFLOW_TYPE} execution(s)")

    log(f"Mayan: {MAYAN_BASE_URL}")
    with httpx.Client(
        base_url=f"{MAYAN_BASE_URL}/api/v4", auth=(MAYAN_USERNAME, MAYAN_PASSWORD), timeout=30.0
    ) as mayan_client:
        trashed, purged = clear_mayan(mayan_client)
        log(f"  trashed {trashed} document(s), permanently purged {purged}")
        log("  rebuilding indexes...")
        rebuild_mayan_indexes(mayan_client)

    print("\n" + "=" * 72)
    print("Done. All loan-onboarding test data cleared from Postgres, Temporal, and Mayan.")
    print("=" * 72)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (httpx.HTTPError, ConnectionRefusedError) as exc:
        print(f"\nError: {exc}\nIs the full stack up (`docker compose up -d`)?", file=sys.stderr)
        sys.exit(1)
