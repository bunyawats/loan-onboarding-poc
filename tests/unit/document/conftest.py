"""Same deliberate exception `tests/unit/application/conftest.py`
already documents -- `document/db.py`'s own tests (Phase 24, "Document
metadata persistence in Postgres") hit a real Postgres rather than
mocking, since some of them (the `(account_id, category)`/
`(customer_id, category)` unique indexes actually firing an
`ON CONFLICT ... DO UPDATE`, not just a rejection) are statements about
real database behavior a mock can't verify. See CLAUDE.md's Testing
section.

NOT autouse at the package level -- this directory also holds
`test_service.py` (`FakeMayanClient`-backed, no Postgres involved yet
as of P24-2) and `test_mayan_client.py` (`respx`-mocked, no Postgres at
all). Only `test_db.py` opts in, via its own
`pytestmark = pytest.mark.usefixtures(...)`, same convention
`tests/unit/application/conftest.py` already establishes."""

import pytest

from loan_onboarding.document import db


@pytest.fixture
async def _clean_document_tables():
    pool = await db._get_pool()
    yield
    # No real FKs between these three tables and anything else
    # (CLAUDE.md's "no FKs anywhere" rule) -- clean explicitly, not
    # relying on a cascade that doesn't exist.
    await pool.execute("DELETE FROM application_document")
    await pool.execute("DELETE FROM account_document")
    await pool.execute("DELETE FROM customer_document")
