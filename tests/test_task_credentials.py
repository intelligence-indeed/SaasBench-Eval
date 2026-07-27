import pytest

from saas_agent.task_credentials import (
    TaskCredential,
    TaskCredentialError,
    parse_task_credential,
)


def test_parse_task_credential_reads_one_exact_line():
    description = """\
**Login Credentials:**

- twenty: jony.ive@apple.dev / tim@apple.dev
- bigcapital: admin@example.test / admin123
"""

    assert parse_task_credential(description, "twenty") == TaskCredential(
        username="jony.ive@apple.dev",
        password="tim@apple.dev",
    )
    assert parse_task_credential(description, "siyuan") is None


def test_parse_task_credential_does_not_infer_from_prose():
    description = "Log in to twenty with jony.ive@apple.dev / tim@apple.dev."

    assert parse_task_credential(description, "twenty") is None


def test_parse_task_credential_rejects_duplicate_app_lines():
    description = """\
- twenty: first@example.test / one
- twenty: second@example.test / two
"""

    with pytest.raises(TaskCredentialError, match="multiple credential lines"):
        parse_task_credential(description, "twenty")


def test_parse_task_credential_rejects_invalid_app_identifier():
    with pytest.raises(TaskCredentialError, match="simple application identifier"):
        parse_task_credential("", "twenty app")
