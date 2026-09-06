"""workflow/service.py's own input validation -- the parts that don't
need a live Temporal server at all. P4-4's DoD is integration-verify
(a real local Temporal server, run manually -- see the Session Log), so
this file only covers the validation this module does before it ever
touches a client.
"""

import pytest

from loan_onboarding.workflow.service import (
    _MAX_BULK_SIZE,
    _validate_bulk_ids,
    bulk_signal_decision,
    signal_close_account_decision,
    signal_decision,
)


def test_validate_bulk_ids_dedupes_preserving_order():
    assert _validate_bulk_ids(["a", "b", "a", "c"]) == ["a", "b", "c"]


def test_validate_bulk_ids_rejects_empty():
    with pytest.raises(ValueError, match="must not be empty"):
        _validate_bulk_ids([])


def test_validate_bulk_ids_rejects_over_max():
    with pytest.raises(ValueError, match="at most"):
        _validate_bulk_ids([f"wf-{i}" for i in range(_MAX_BULK_SIZE + 1)])


async def test_signal_decision_rejects_unknown_actor_role():
    with pytest.raises(ValueError, match="actor_role"):
        await signal_decision(None, "wf-1", "clerk", "APPROVE", "x")


async def test_signal_decision_rejects_unknown_decision():
    with pytest.raises(ValueError, match="decision"):
        await signal_decision(None, "wf-1", "underwriter", "MAYBE", "x")


async def test_bulk_signal_decision_rejects_unknown_actor_role():
    with pytest.raises(ValueError, match="actor_role"):
        await bulk_signal_decision(None, ["wf-1"], "clerk", "APPROVE", "x")


async def test_bulk_signal_decision_rejects_unknown_decision():
    with pytest.raises(ValueError, match="decision"):
        await bulk_signal_decision(None, ["wf-1"], "underwriter", "MAYBE", "x")


async def test_signal_close_account_decision_rejects_customer_actor_role():
    """The customer's own decision path is signal_close_account_cancel(),
    not this function -- unlike the loan workflow's signal_decision,
    "customer" is never a valid actor_role here."""
    with pytest.raises(ValueError, match="actor_role"):
        await signal_close_account_decision(None, "wf-1", "customer", "APPROVE", "x")


async def test_signal_close_account_decision_rejects_unknown_actor_role():
    with pytest.raises(ValueError, match="actor_role"):
        await signal_close_account_decision(None, "wf-1", "clerk", "APPROVE", "x")


async def test_signal_close_account_decision_rejects_unknown_decision():
    with pytest.raises(ValueError, match="decision"):
        await signal_close_account_decision(None, "wf-1", "underwriter", "MAYBE", "x")


async def test_signal_close_account_decision_rejects_cancelled_decision():
    """CANCELLED is a valid LoanApplicationWorkflow decision but not a
    valid staff closure decision -- only the customer's own cancel()
    signal produces that outcome for CloseAccountWorkflow."""
    with pytest.raises(ValueError, match="decision"):
        await signal_close_account_decision(None, "wf-1", "underwriter", "CANCELLED", "x")
