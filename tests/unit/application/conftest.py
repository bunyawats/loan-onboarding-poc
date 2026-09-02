"""Same deliberate exception as tests/unit/customer/ and
tests/unit/account/ -- application/db.py's tests hit a real Postgres
rather than mocking, since some of them (jsonb round-tripping, COALESCE
preserving existing columns) are statements about database behavior a
mock can't verify. See CLAUDE.md's Testing section."""

import pytest

from loan_onboarding.application import db


@pytest.fixture(autouse=True)
async def _clean_applications_table():
    pool = await db._get_pool()
    yield
    await pool.execute("DELETE FROM applications")
