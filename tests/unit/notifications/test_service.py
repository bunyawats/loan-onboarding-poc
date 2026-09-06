from unittest.mock import MagicMock

import pytest

from loan_onboarding.notifications import service


@pytest.fixture(autouse=True)
def _no_smtp_env(monkeypatch):
    """Guards against a developer's own local `.env`/shell leaking real
    `SMTP_*` values into this file's fake-path tests -- every test here
    gets a clean slate and opts into real-send behavior explicitly via
    `monkeypatch.setenv`, same discipline `_send_email`'s real-vs-fake
    branch itself relies on."""
    for var in ("SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_HOST", "SMTP_PORT", "SMTP_FROM_ADDRESS"):
        monkeypatch.delenv(var, raising=False)


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


def _mock_smtp(monkeypatch):
    smtp_instance = MagicMock()
    smtp_instance.__enter__.return_value = smtp_instance
    smtp_cls = MagicMock(return_value=smtp_instance)
    monkeypatch.setattr(service.smtplib, "SMTP", smtp_cls)
    return smtp_cls, smtp_instance


def test_send_account_closure_decision_sends_real_email_when_smtp_configured(monkeypatch, capsys):
    monkeypatch.setenv("SMTP_USERNAME", "poc@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    smtp_cls, smtp_instance = _mock_smtp(monkeypatch)

    service.send_account_closure_decision(
        applicant_identifier="bob@example.com",
        account_id="ACC-000000001",
        product_type="personal_loan",
        decision="APPROVED",
        comment="balance confirmed zero",
    )

    smtp_cls.assert_called_once_with("smtp.gmail.com", 587)
    smtp_instance.starttls.assert_called_once()
    smtp_instance.login.assert_called_once_with("poc@gmail.com", "app-password")
    sent_message = smtp_instance.send_message.call_args[0][0]
    assert sent_message["To"] == "bob@example.com"
    assert sent_message["From"] == "poc@gmail.com"
    assert "APPROVED" in sent_message["Subject"]
    assert "balance confirmed zero" in sent_message.get_content()
    # No fake fallback text should leak into a genuinely-sent email.
    assert "POC: no real email/SMS provider configured" not in sent_message.get_content()
    assert capsys.readouterr().out == ""


def test_send_welcome_letter_email_sends_real_email_when_smtp_configured(monkeypatch):
    monkeypatch.setenv("SMTP_USERNAME", "poc@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    monkeypatch.setenv("SMTP_FROM_ADDRESS", "loans@example.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "2525")
    smtp_cls, smtp_instance = _mock_smtp(monkeypatch)

    service.send_welcome_letter_email(
        applicant_identifier="carol@example.com",
        account_id="ACC-000000002",
        product_type="auto_loan",
        amount="15000.00",
    )

    smtp_cls.assert_called_once_with("smtp.example.com", 2525)
    sent_message = smtp_instance.send_message.call_args[0][0]
    assert sent_message["To"] == "carol@example.com"
    assert sent_message["From"] == "loans@example.com"
    assert "15000.00" in sent_message.get_content()


def test_send_email_falls_back_to_print_and_does_not_raise_on_smtp_failure(monkeypatch, capsys):
    monkeypatch.setenv("SMTP_USERNAME", "poc@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "app-password")
    smtp_cls = MagicMock(side_effect=OSError("connection refused"))
    monkeypatch.setattr(service.smtplib, "SMTP", smtp_cls)

    service.send_account_closure_decision(
        applicant_identifier="bob@example.com",
        account_id="ACC-000000001",
        product_type="personal_loan",
        decision="REJECTED",
        comment="dispute pending",
    )

    out = capsys.readouterr().out
    assert "bob@example.com" in out
    assert "connection refused" in out
