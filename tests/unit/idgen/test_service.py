from loan_onboarding.idgen import service


def test_generate_id_has_prefix_and_digit_length():
    result = service.generate_id("cus", 9)
    assert result.startswith("cus-")
    suffix = result.removeprefix("cus-")
    assert len(suffix) == 9
    assert suffix.isdigit()


def test_generate_id_alphabet_is_digits_only():
    for _ in range(50):
        suffix = service.generate_id("acc", 9).removeprefix("acc-")
        assert all(c in "0123456789" for c in suffix)


def test_generate_id_is_statistically_unique_across_a_large_sample():
    """Not a proof -- a statistical sanity check that generate_id isn't
    silently returning a constant or a low-entropy value. 10_000 draws
    over 10**9 possible values has a negligible chance of a real
    collision if secrets.choice is doing its job."""
    ids = {service.generate_id("app", 9) for _ in range(10_000)}
    assert len(ids) == 10_000
