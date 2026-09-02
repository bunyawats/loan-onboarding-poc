"""customer/service.py's tests exercise a real Postgres database rather
than mocking customer/db.py -- P2-2's DoD asks for things (idempotency,
"no second row was created") that only mean something against a real
`customers` table, not against a mock recording call order. This is the
one deliberate exception to CLAUDE.md's "tests/unit/ ... no live
services": DATABASE_URL must point at a database with db/schema.sql
applied (CI provisions this; locally, `docker compose up -d db` plus
db/init/01-init.sh having run is enough -- see CLAUDE.md's Testing
section for the full reasoning)."""

import pytest

from loan_onboarding.customer import db


@pytest.fixture(autouse=True)
async def _clean_customers_table():
    pool = await db._get_pool()
    yield
    await pool.execute("DELETE FROM customers")
