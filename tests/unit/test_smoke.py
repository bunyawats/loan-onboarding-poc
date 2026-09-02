"""Package-import smoke test -- keeps `pytest tests/unit` from failing on
zero collected tests before any real module has its own tests yet (Phase
2+). Once every module has real coverage this stays a cheap, honest check
that the package tree still imports cleanly."""


def test_package_imports():
    import loan_onboarding  # noqa: F401


def test_all_module_packages_import():
    import loan_onboarding.account  # noqa: F401
    import loan_onboarding.application  # noqa: F401
    import loan_onboarding.bff_backoffice  # noqa: F401
    import loan_onboarding.bff_customer  # noqa: F401
    import loan_onboarding.customer  # noqa: F401
    import loan_onboarding.document  # noqa: F401
    import loan_onboarding.workflow  # noqa: F401
