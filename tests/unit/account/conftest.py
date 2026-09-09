"""Same deliberate exception as tests/unit/customer/ -- these hit a
real Postgres rather than mocking account/db.py, since P3-2's DoD asks
for the real partial unique index to fire, not just that the function
would reject a duplicate. See CLAUDE.md's Testing section."""

import pytest

from loan_onboarding.account import db


@pytest.fixture(autouse=True)
async def _clean_accounts_table():
    pool = await db._get_pool()
    yield
    # account_closure_requests has no real FK to accounts (CLAUDE.md's
    # "no real FKs between tables" rule) -- clean both explicitly, not
    # relying on a cascade that doesn't exist.
    await pool.execute("DELETE FROM account_closure_requests")
    await pool.execute("DELETE FROM accounts")
