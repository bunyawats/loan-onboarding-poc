"""Same deliberate exception as tests/unit/customer/ and
tests/unit/account/ -- application/db.py's tests hit a real Postgres
rather than mocking, since some of them (jsonb round-tripping, COALESCE
preserving existing columns) are statements about database behavior a
mock can't verify. See CLAUDE.md's Testing section.

NOT autouse at the package level (unlike customer/account, whose whole
test directory needs the database) -- this package also holds
test_schemas.py (no I/O at all) and will hold test_service.py (mocked
at the mayan_client/workflow.service boundary style, not a real
Postgres). Only test_db.py opts in, via its own
`pytestmark = pytest.mark.usefixtures(...)`."""

import pytest

from loan_onboarding.application import db


@pytest.fixture
async def _clean_applications_table():
    pool = await db._get_pool()
    yield
    await pool.execute("DELETE FROM applications")
