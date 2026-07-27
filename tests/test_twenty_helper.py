import json
from types import SimpleNamespace

import pytest

from saas_agent.twenty_helper import TwentyClient, TwentyError


def test_query_records_discovers_schema_and_returns_exact_matches():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        sql = args[-1]
        if "information_schema.schemata" in sql:
            return SimpleNamespace(returncode=0, stdout="workspace_demo\n", stderr="")
        if "information_schema.columns" in sql:
            return SimpleNamespace(
                returncode=0,
                stdout="id\nname\nstage\ncreatedAt\ndeletedAt\n",
                stderr="",
            )
        rows = [
            {"id": "1", "name": "Target Deal", "stage": "NEW"},
            {"id": "2", "name": "Other Deal", "stage": "WON"},
        ]
        return SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(row) for row in rows),
            stderr="",
        )

    client = TwentyClient("slot_0_twenty", runner=runner)
    result = client.query_records("opportunities", ["Target Deal"])

    assert result["workspace_schema"] == "workspace_demo"
    assert result["missing_exact_names"] == []
    assert result["records"] == [{
        "id": "1",
        "name": "Target Deal",
        "stage": "NEW",
        "_exact_name": "Target Deal",
    }]
    assert all(call[:3] == ["docker", "exec", "slot_0_twenty"] for call in calls)
    record_query = next(call[-1] for call in calls if "row_to_json" in call[-1])
    assert 'WHERE "deletedAt" IS NULL AND ("name") IN (\'Target Deal\')' in record_query
    assert record_query.index("WHERE") < record_query.index("LIMIT")


def test_query_records_rejects_arbitrary_entity_and_limit():
    client = TwentyClient("slot_0_twenty", runner=lambda *a, **k: None)
    with pytest.raises(TwentyError, match="unsupported entity"):
        client.query_records("raw_sql")
    with pytest.raises(TwentyError, match="between 1 and 1000"):
        client.query_records("companies", limit=5000)


def test_query_by_business_key_filters_person_email_before_limit():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        sql = args[-1]
        if "information_schema.schemata" in sql:
            return SimpleNamespace(returncode=0, stdout="workspace_demo\n", stderr="")
        if "information_schema.columns" in sql:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "id\nnameFirstName\nnameLastName\nemailsPrimaryEmail\n"
                    "companyId\ncreatedAt\ndeletedAt\n"
                ),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "id": "person-1",
                "emailsPrimaryEmail": "target@example.test",
            }),
            stderr="",
        )

    result = TwentyClient("slot_0_twenty", runner=runner).query_by_business_key(
        "people", "email", ["target@example.test"]
    )

    assert result["missing_exact_values"] == []
    assert result["records"][0]["_exact_email"] == "target@example.test"
    sql = next(call[-1] for call in calls if "row_to_json" in call[-1])
    assert '"emailsPrimaryEmail") IN (\'target@example.test\')' in sql
    assert sql.index("WHERE") < sql.index("LIMIT")


def test_query_activity_targets_uses_only_whitelisted_relation_table():
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        sql = args[-1]
        if "information_schema.schemata" in sql:
            return SimpleNamespace(returncode=0, stdout="workspace_demo\n", stderr="")
        if "information_schema.columns" in sql:
            return SimpleNamespace(
                returncode=0,
                stdout="id\ntaskId\ntargetCompanyId\ndeletedAt\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "id": "target-1",
                "taskId": "task-1",
                "targetCompanyId": "company-1",
            }),
            stderr="",
        )

    rows = TwentyClient("slot_0_twenty", runner=runner).query_activity_targets(
        "tasks", "task-1"
    )

    assert rows[0]["targetCompanyId"] == "company-1"
    sql = next(call[-1] for call in calls if "row_to_json" in call[-1])
    assert '"taskTarget"' in sql
    assert '"taskId"=\'task-1\'' in sql
