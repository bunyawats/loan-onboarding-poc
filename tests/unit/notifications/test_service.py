from loan_onboarding.notifications import service


def test_send_verification_code_prints_identifier_and_code(capsys):
    service.send_verification_code("alice@example.com", "123456")
    out = capsys.readouterr().out
    assert "alice@example.com" in out
    assert "123456" in out


def test_send_account_closure_decision_prints_all_fields(capsys):
    service.send_account_closure_decision(
        applicant_identifier="bob@example.com",
        account_id="ACC-000000001",
        product_type="personal_loan",
        decision="APPROVED",
        comment="balance confirmed zero",
    )
    out = capsys.readouterr().out
    assert "bob@example.com" in out
    assert "ACC-000000001" in out
    assert "personal_loan" in out
    assert "APPROVED" in out
    assert "balance confirmed zero" in out


def test_send_welcome_letter_email_prints_all_fields(capsys):
    service.send_welcome_letter_email(
        applicant_identifier="carol@example.com",
        account_id="ACC-000000002",
        product_type="auto_loan",
        amount="15000.00",
    )
    out = capsys.readouterr().out
    assert "carol@example.com" in out
    assert "ACC-000000002" in out
    assert "auto_loan" in out
    assert "15000.00" in out
