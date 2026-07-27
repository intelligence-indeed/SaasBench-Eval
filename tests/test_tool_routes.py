import asyncio
import json

import pytest

from saas_agent.tool_routes import build_tools, build_tool_system_rules


class FakeTools:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.actions = {}

    def action(self, description, **kwargs):
        name = kwargs.get("name")

        def decorator(func):
            self.actions[name or func.__name__] = {
                "description": description,
                "func": func,
            }
            return func

        return decorator


def test_baserow_tool_is_registered_only_for_baserow_tasks(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    apps = ["code-server", "baserow"]
    tools, meta = build_tools(
        apps,
        {"base_urls": {"baserow": "http://localhost:32009"}},
        tools_factory=FakeTools,
    )

    assert isinstance(tools, FakeTools)
    assert "baserow_ensure_table" in tools.actions
    assert meta["mode"] == "routing"
    assert meta["app_tools"] == ["baserow"]
    assert meta["actions"] == ["baserow_ensure_table"]
    assert meta["base_urls"]["baserow"] == "http://localhost:32009"


def test_no_baserow_site_registers_no_helper(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    apps = ["siyuan"]
    tools, meta = build_tools(
        apps,
        {"base_urls": {"siyuan": "http://localhost:32001"}},
        tools_factory=FakeTools,
    )

    assert tools.actions == {}
    assert meta["app_tools"] == []
    assert meta["actions"] == []


def test_twenty_and_bigcapital_tools_are_routed_by_sites(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    monkeypatch.setenv("SAAS_AGENT_BIGCAPITAL_WRITE_MODE", "routing")
    apps = ["twenty", "bigcapital"]
    tools, meta = build_tools(
        apps,
        {
            "base_urls": {"bigcapital": "http://localhost:32005"},
            "container_names": {"twenty": "slot_0_twenty"},
        },
        tools_factory=FakeTools,
    )

    assert meta["app_tools"] == ["twenty", "bigcapital"]
    assert meta["actions"] == [
        "twenty_query_records",
        "bigcapital_query_customers",
        "bigcapital_ensure_customers",
    ]
    assert meta["base_urls"]["bigcapital"] == "http://localhost:32005"
    assert set(tools.actions) == set(meta["actions"])
    rules = build_tool_system_rules(meta)
    assert "Twenty Readback Tool" in rules
    assert "BigCapital Customer Tools" in rules


def test_twenty_and_bigcapital_actions_call_clients(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    monkeypatch.setenv("SAAS_AGENT_BIGCAPITAL_WRITE_MODE", "routing")
    calls = []

    class TwentyStub:
        def __init__(self, container_name):
            calls.append(("twenty_init", container_name))

        def query_records(self, entity, exact_names, limit):
            calls.append(("twenty_query", entity, exact_names, limit))
            return {"matched_count": 1}

    class BigCapitalStub:
        def __init__(self, base_url):
            calls.append(("bigcapital_init", base_url))

        def query_customers(self, exact_names, limit):
            calls.append(("bigcapital_query", exact_names, limit))
            return {"matched_count": 1}

        def ensure_customers(self, customers, currency_code):
            calls.append(("bigcapital_ensure", customers, currency_code))
            return {"created": [customers[0]["display_name"]]}

    tools, _ = build_tools(
        ["twenty", "bigcapital"],
        {
            "base_urls": {"bigcapital": "http://localhost:32005"},
            "container_names": {"twenty": "slot_0_twenty"},
        },
        tools_factory=FakeTools,
        twenty_client_cls=TwentyStub,
        bigcapital_client_cls=BigCapitalStub,
    )

    twenty_result = asyncio.run(
        tools.actions["twenty_query_records"]["func"](
            "opportunities", ["Deal"], 20
        )
    )
    query_result = asyncio.run(
        tools.actions["bigcapital_query_customers"]["func"](["Acme"], 30)
    )
    ensure_result = asyncio.run(
        tools.actions["bigcapital_ensure_customers"]["func"](
            [{"display_name": "Acme"}], "USD"
        )
    )

    assert '"matched_count": 1' in twenty_result
    assert '"matched_count": 1' in query_result
    assert '"created": ["Acme"]' in ensure_result
    assert ("twenty_query", "opportunities", ["Deal"], 20) in calls
    assert ("bigcapital_query", ["Acme"], 30) in calls


def test_bigcapital_and_twenty_write_actions_default_off(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    apps = ["twenty", "bigcapital"]

    tools, meta = build_tools(
        apps,
        {
            "base_urls": {
                "twenty": "http://localhost:32004",
                "bigcapital": "http://localhost:32005",
            },
            "container_names": {"twenty": "slot_0_twenty"},
        },
        tools_factory=FakeTools,
    )

    assert meta["write_modes"] == {"bigcapital": "off", "twenty": "off"}
    assert "twenty_ensure_records" not in tools.actions
    assert "bigcapital_ensure_customers" not in tools.actions
    assert "twenty_query_records" in tools.actions
    assert "bigcapital_query_customers" in tools.actions


def test_twenty_write_action_routes_with_credentials_without_leaking_them(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    monkeypatch.setenv("SAAS_AGENT_TWENTY_WRITE_MODE", "routing")
    calls = []

    class TwentyReadStub:
        def __init__(self, container_name):
            calls.append(("read_init", container_name))

        def query_records(self, entity, exact_names, limit):
            return {"records": []}

    class TwentyWriteStub:
        def __init__(self, base_url, email, password, read_client):
            calls.append(("write_init", base_url, email, password, type(read_client).__name__))

        def ensure_records(self, companies, people, opportunities, tasks, notes):
            calls.append(("ensure", companies, people, opportunities, tasks, notes))
            return {"created": {"companies": [companies[0]["name"]]}}

    apps = ["twenty"]
    tools, meta = build_tools(
        apps,
        {
            "base_urls": {"twenty": "http://localhost:32004"},
            "container_names": {"twenty": "slot_0_twenty"},
        },
        credentials={
            "twenty": {
                "username": "user@example.test",
                "password": "secret-pass",
            }
        },
        tools_factory=FakeTools,
        twenty_client_cls=TwentyReadStub,
        twenty_write_client_cls=TwentyWriteStub,
    )

    result = asyncio.run(
        tools.actions["twenty_ensure_records"]["func"](
            [{"name": "Acme"}], None, None, None, None
        )
    )

    assert '"Acme"' in result
    assert meta["actions"] == ["twenty_query_records", "twenty_ensure_records"]
    assert meta["base_urls"]["twenty"] == "http://localhost:32004"
    assert "user@example.test" not in json.dumps(meta)
    assert "secret-pass" not in json.dumps(meta)
    assert ("write_init", "http://localhost:32004", "user@example.test", "secret-pass", "TwentyReadStub") in calls


def test_twenty_write_missing_credentials_keeps_read_action(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    monkeypatch.setenv("SAAS_AGENT_TWENTY_WRITE_MODE", "routing")

    tools, meta = build_tools(
        ["twenty"],
        {
            "base_urls": {"twenty": "http://localhost:32004"},
            "container_names": {"twenty": "slot_0_twenty"},
        },
        description="No credential line",
        tools_factory=FakeTools,
    )

    assert set(tools.actions) == {"twenty_query_records"}
    assert "twenty credentials" in meta["missing_context"]


def test_openproject_tool_is_routed_with_slot_url(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    apps = ["openproject"]
    tools, meta = build_tools(
        apps,
        {"base_urls": {"openproject": "http://localhost:32003"}},
        tools_factory=FakeTools,
    )

    assert meta["app_tools"] == ["openproject"]
    assert meta["actions"] == [
        "openproject_ensure_work_packages",
        "openproject_query_work_packages",
    ]
    assert meta["base_urls"]["openproject"] == "http://localhost:32003"
    assert "openproject_ensure_work_packages" in tools.actions


def test_registered_openproject_action_calls_helper(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    calls = {}

    class StubClient:
        def __init__(self, base_url):
            calls["base_url"] = base_url

        def ensure_work_packages(
            self,
            project_name,
            work_packages,
            users=None,
            exact_subject_set=False,
        ):
            calls["payload"] = {
                "project_name": project_name,
                "work_packages": work_packages,
                "users": users,
                "exact_subject_set": exact_subject_set,
            }
            return {"created_ids": [7]}

        def query_work_packages(
            self,
            project_name,
            version_name=None,
            status_name=None,
            max_items=1000,
        ):
            calls["query"] = {
                "project_name": project_name,
                "version_name": version_name,
                "status_name": status_name,
                "max_items": max_items,
            }
            return {"count": 1, "work_packages": [{"id": 7}]}

    apps = ["openproject"]
    tools, _meta = build_tools(
        apps,
        {"base_urls": {"openproject": "http://localhost:32003"}},
        tools_factory=FakeTools,
        openproject_client_cls=StubClient,
    )
    action = tools.actions["openproject_ensure_work_packages"]["func"]
    result = asyncio.run(action(
        "Security Audit",
        [{
            "subject": "Task",
            "type": "Task",
            "priority": "Normal",
            "assignee_login": "admin",
            "description": "text",
        }],
        None,
        True,
    ))

    assert calls["base_url"] == "http://localhost:32003"
    assert calls["payload"]["exact_subject_set"] is True
    assert '"created_ids": [7]' in result

    query_action = tools.actions["openproject_query_work_packages"]["func"]
    query_result = asyncio.run(query_action("security-audit", "Release 1", "Closed", 50))
    assert calls["query"] == {
        "project_name": "security-audit",
        "version_name": "Release 1",
        "status_name": "Closed",
        "max_items": 50,
    }
    assert '"count": 1' in query_result


def test_openproject_credentials_are_injected_without_metadata_leak(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    calls = {}

    class StubClient:
        def __init__(self, base_url, **kwargs):
            calls["init"] = {"base_url": base_url, **kwargs}

        def query_work_packages(self, **kwargs):
            return {"count": 0, "work_packages": []}

    tools, meta = build_tools(
        ["openproject"],
        {
            "base_urls": {"openproject": "http://localhost:32003"},
            "credentials": {
                "openproject": {
                    "username": "agent-user",
                    "password": "private-password",
                }
            },
        },
        tools_factory=FakeTools,
        openproject_client_cls=StubClient,
    )

    asyncio.run(
        tools.actions["openproject_query_work_packages"]["func"]("Demo")
    )

    assert calls["init"] == {
        "base_url": "http://localhost:32003",
        "username": "agent-user",
        "password": "private-password",
    }
    serialized_meta = json.dumps(meta)
    assert "agent-user" not in serialized_meta
    assert "private-password" not in serialized_meta


def test_openproject_repeated_failure_opens_shared_task_circuit(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    calls = {"count": 0}

    class FailingClient:
        def __init__(self, base_url):
            pass

        def ensure_work_packages(self, **kwargs):
            calls["count"] += 1
            raise RuntimeError("authentication preflight failed: HTTP 401")

        def query_work_packages(self, **kwargs):
            calls["count"] += 1
            raise RuntimeError("authentication preflight failed: HTTP 401")

    apps = ["openproject"]
    tools, _meta = build_tools(
        apps,
        {"base_urls": {"openproject": "http://localhost:32003"}},
        tools_factory=FakeTools,
        openproject_client_cls=FailingClient,
    )
    query = tools.actions["openproject_query_work_packages"]["func"]

    with pytest.raises(RuntimeError, match="HTTP 401"):
        asyncio.run(query("Demo"))
    with pytest.raises(RuntimeError, match="Circuit opened"):
        asyncio.run(query("Demo"))
    with pytest.raises(RuntimeError, match="circuit is open"):
        asyncio.run(query("Demo"))
    assert calls["count"] == 2


def test_metabase_tools_are_routed_with_slot_database_context(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    monkeypatch.delenv("SAAS_AGENT_METABASE_TOOL_MODE", raising=False)
    apps = ["metabase"]
    tools, meta = build_tools(
        apps,
        {
            "base_urls": {"metabase": "http://localhost:32002"},
            "postgres": {
                "baserow": {"host": "host.docker.internal", "port": 32018}
            },
        },
        tools_factory=FakeTools,
    )

    assert meta["app_tools"] == ["metabase"]
    assert meta["actions"] == [
        "metabase_inspect_schema",
        "metabase_ensure_analytics",
    ]
    assert meta["base_urls"]["metabase"] == "http://localhost:32002"
    assert set(tools.actions) == set(meta["actions"])


def test_registered_metabase_actions_call_helper_without_prompt_credentials(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    calls = {}

    class StubClient:
        def __init__(
            self,
            base_url,
            *,
            baserow_host,
            baserow_port,
            baserow_api_url,
        ):
            calls["init"] = {
                "base_url": base_url,
                "baserow_host": baserow_host,
                "baserow_port": baserow_port,
                "baserow_api_url": baserow_api_url,
            }

        def inspect_schema(self, table_names=None, *, sync=True):
            calls["inspect"] = {"table_names": table_names, "sync": sync}
            return {"tables": [{"name": "events"}]}

        def ensure_analytics(
            self,
            collection_name,
            questions,
            dashboard,
            *,
            sync=True,
        ):
            calls["ensure"] = {
                "collection_name": collection_name,
                "questions": questions,
                "dashboard": dashboard,
                "sync": sync,
            }
            return {"dashboard": {"id": 7}}

    apps = ["baserow", "metabase"]
    tools, _meta = build_tools(
        apps,
        {
            "base_urls": {
                "baserow": "http://localhost:32003",
                "metabase": "http://localhost:32002",
            },
            "postgres": {
                "baserow": {"host": "host.docker.internal", "port": 32018}
            },
        },
        tools_factory=FakeTools,
        metabase_client_cls=StubClient,
    )

    inspect_result = asyncio.run(
        tools.actions["metabase_inspect_schema"]["func"](["Events"], True)
    )
    ensure_result = asyncio.run(
        tools.actions["metabase_ensure_analytics"]["func"](
            "Event Analytics",
            [{"name": "Events", "source_table": "Events", "display": "table"}],
            {"name": "Event Dashboard", "question_names": ["Events"]},
            True,
        )
    )

    assert calls["init"] == {
        "base_url": "http://localhost:32002",
        "baserow_host": "host.docker.internal",
        "baserow_port": 32018,
        "baserow_api_url": "http://localhost:32003",
    }
    assert calls["inspect"] == {"table_names": ["Events"], "sync": True}
    assert calls["ensure"]["collection_name"] == "Event Analytics"
    assert '"tables"' in inspect_result
    assert '"dashboard"' in ensure_result
    combined = inspect_result + ensure_result
    assert "mw-admin-123" not in combined
    assert "kdpzkuy" not in combined


def test_metabase_repeated_connection_failure_opens_shared_task_circuit(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    calls = {"count": 0}

    class FailingClient:
        def __init__(self, base_url, *, baserow_host, baserow_port):
            pass

        def inspect_schema(self, table_names=None, *, sync=True):
            calls["count"] += 1
            raise RuntimeError("database connection failed with HTTP 400")

        def ensure_analytics(self, *args, **kwargs):
            calls["count"] += 1
            raise RuntimeError("database connection failed with HTTP 400")

    apps = ["metabase"]
    tools, _meta = build_tools(
        apps,
        {
            "base_urls": {"metabase": "http://localhost:32002"},
            "postgres": {
                "baserow": {"host": "host.docker.internal", "port": 32018}
            },
        },
        tools_factory=FakeTools,
        metabase_client_cls=FailingClient,
    )
    inspect = tools.actions["metabase_inspect_schema"]["func"]

    with pytest.raises(RuntimeError, match="HTTP 400"):
        asyncio.run(inspect(["Events"]))
    with pytest.raises(RuntimeError, match="Circuit opened"):
        asyncio.run(inspect(["Events"]))
    with pytest.raises(RuntimeError, match="circuit is open"):
        asyncio.run(inspect(["Events"]))
    assert calls["count"] == 2


def test_metabase_tool_can_be_disabled_without_disabling_other_tools(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    monkeypatch.setenv("SAAS_AGENT_METABASE_TOOL_MODE", "disabled")
    apps = ["baserow", "metabase"]
    tools, meta = build_tools(
        apps,
        {
            "base_urls": {
                "baserow": "http://localhost:32003",
                "metabase": "http://localhost:32002",
            },
            "postgres": {
                "baserow": {"host": "host.docker.internal", "port": 32018}
            },
        },
        tools_factory=FakeTools,
    )

    assert meta["app_tools"] == ["baserow"]
    assert meta["actions"] == ["baserow_ensure_table"]
    assert "metabase_inspect_schema" not in tools.actions


def test_metabase_missing_pg_port_is_reported_without_guessing(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    monkeypatch.delenv("SAAS_AGENT_METABASE_TOOL_MODE", raising=False)
    apps = ["metabase"]
    tools, meta = build_tools(
        apps,
        {"base_urls": {"metabase": "http://localhost:32002"}},
        tools_factory=FakeTools,
    )

    assert tools.actions == {}
    assert "baserow postgres port" in meta["missing_context"]


def test_code_server_tools_are_registered_for_code_server_tasks(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    apps = ["code-server", "baserow"]
    tools, meta = build_tools(
        apps,
        {
            "base_urls": {"baserow": "http://localhost:32009"},
            "container_names": {"code-server": "rollout_0_code-server"},
        },
        tools_factory=FakeTools,
    )

    assert meta["app_tools"] == ["baserow", "code-server"]
    assert set(meta["actions"]) == {
        "baserow_ensure_table",
        "code_search_files",
        "code_read_files",
        "code_write_file",
        "code_exec",
        "code_run_python",
        "code_scan_docker_security",
        "code_scan_project_dependencies",
        "code_collect_test_metrics",
        "code_git_commit",
    }
    assert set(tools.actions) == set(meta["actions"])
    rules = build_tool_system_rules(meta)
    assert "Every project entry requires `project`, `command`, and `test_globs`" in rules
    assert '"parser":"pytest"' in rules
    assert "not a reason to install packages" in rules


def test_tool_mode_disabled_registers_no_helper(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "disabled")
    apps = ["baserow"]
    tools, meta = build_tools(
        apps,
        {"base_urls": {"baserow": "http://localhost:32009"}},
        tools_factory=FakeTools,
    )

    assert tools.actions == {}
    assert meta["mode"] == "disabled"


def test_registered_baserow_action_calls_helper(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    calls = {}

    class StubClient:
        def __init__(self, base_url):
            calls["base_url"] = base_url

        def ensure_table(self, database_name, table_name, fields, rows, views=None, replace_rows=False):
            calls["payload"] = {
                "database_name": database_name,
                "table_name": table_name,
                "fields": fields,
                "rows": rows,
                "views": views,
                "replace_rows": replace_rows,
            }
            return {"ok": True, "row_count": len(rows)}

    apps = ["baserow"]
    tools, _meta = build_tools(
        apps,
        {"base_urls": {"baserow": "http://localhost:32009"}},
        tools_factory=FakeTools,
        baserow_client_cls=StubClient,
    )

    action = tools.actions["baserow_ensure_table"]["func"]
    result = asyncio.run(
        action(
            "DB",
            "Table",
            [{"name": "Name", "type": "text"}],
            [{"Name": "row"}],
            [{"name": "Grid", "type": "grid"}],
            True,
        )
    )

    assert calls["base_url"] == "http://localhost:32009"
    assert calls["payload"]["table_name"] == "Table"
    assert calls["payload"]["views"] == [{"name": "Grid", "type": "grid"}]
    assert calls["payload"]["replace_rows"] is True
    assert '"row_count": 1' in result


def test_app_credentials_are_injected_without_metadata_leak(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    monkeypatch.setenv("SAAS_AGENT_BIGCAPITAL_WRITE_MODE", "routing")
    calls = {}

    class BaserowStub:
        def __init__(self, base_url, **kwargs):
            calls["baserow"] = {"base_url": base_url, **kwargs}

        def ensure_table(self, **kwargs):
            return {"ok": True}

    class BigCapitalStub:
        def __init__(self, base_url, **kwargs):
            calls["bigcapital"] = {"base_url": base_url, **kwargs}

        def query_customers(self, exact_names, limit):
            return {"matched_count": 0}

        def ensure_customers(self, customers, currency_code):
            return {"created": []}

    class MetabaseStub:
        def __init__(self, base_url, *, baserow_host, baserow_port, **kwargs):
            calls["metabase"] = {
                "base_url": base_url,
                "baserow_host": baserow_host,
                "baserow_port": baserow_port,
                **kwargs,
            }

        def inspect_schema(self, table_names=None, *, sync=True):
            return {"tables": []}

        def ensure_analytics(self, *args, **kwargs):
            return {"dashboard": {}}

    context = {
        "base_urls": {
            "baserow": "http://baserow.test",
            "bigcapital": "http://bigcapital.test",
            "metabase": "http://metabase.test",
        },
        "credentials": {
            "baserow": {
                "email": "baserow@example.test",
                "password": "baserow-secret",
            },
            "bigcapital": {
                "username": "bigcapital@example.test",
                "password": "bigcapital-secret",
            },
            "metabase": {
                "username": "metabase@example.test",
                "password": "metabase-secret",
            },
        },
        "postgres": {
            "baserow": {
                "host": "database.test",
                "port": 5432,
                "database": "baserow_app",
                "username": "baserow_reader",
                "password": "postgres-secret",
            }
        },
    }
    tools, meta = build_tools(
        ["baserow", "bigcapital", "metabase"],
        context,
        tools_factory=FakeTools,
        baserow_client_cls=BaserowStub,
        bigcapital_client_cls=BigCapitalStub,
        metabase_client_cls=MetabaseStub,
    )

    asyncio.run(
        tools.actions["baserow_ensure_table"]["func"]("DB", "Table", [], [])
    )
    asyncio.run(tools.actions["bigcapital_query_customers"]["func"]([], 10))
    asyncio.run(tools.actions["metabase_inspect_schema"]["func"]([], False))

    assert calls["baserow"]["email"] == "baserow@example.test"
    assert calls["baserow"]["password"] == "baserow-secret"
    assert calls["bigcapital"]["email"] == "bigcapital@example.test"
    assert calls["bigcapital"]["password"] == "bigcapital-secret"
    assert calls["metabase"]["username"] == "metabase@example.test"
    assert calls["metabase"]["password"] == "metabase-secret"
    assert calls["metabase"]["baserow_password"] == "postgres-secret"
    serialized_meta = json.dumps(meta, sort_keys=True)
    for secret in (
        "baserow-secret",
        "bigcapital-secret",
        "metabase-secret",
        "postgres-secret",
    ):
        assert secret not in serialized_meta


def test_tool_system_rules_explain_registered_helper():
    rules = build_tool_system_rules({"actions": ["baserow_ensure_table"]})

    assert "baserow_ensure_table" in rules
    assert "enabled in addition to the standard" in rules
    assert "Do not invent rows" in rules


def test_openproject_tool_rules_explain_query_and_identifier_resolution():
    rules = build_tool_system_rules({
        "actions": [
            "openproject_ensure_work_packages",
            "openproject_query_work_packages",
        ]
    })

    assert "openproject_query_work_packages" in rules
    assert "identifier" in rules
    assert "read-only" in rules


def test_code_server_tool_rules_distinguish_generic_write_file():
    rules = build_tool_system_rules({
        "actions": [
            "code_write_file",
            "code_exec",
            "code_scan_docker_security",
            "code_scan_project_dependencies",
            "code_collect_test_metrics",
        ]
    })

    assert "code_write_file" in rules
    assert "code_exec" in rules
    assert "code_scan_docker_security" in rules
    assert "code_scan_project_dependencies" in rules
    assert "integrated terminal" in rules
    assert "/home/coder/project" in rules
    assert "durable workspace root" in rules
    assert "Do not use the generic `write_file`" in rules
    assert "todo.md" in rules
    assert "aggregate totals only" in rules
    assert "exact code-search pattern returning zero" in rules
    assert "per-test-case" in rules


def test_metabase_tool_rules_require_schema_and_readback():
    rules = build_tool_system_rules({
        "actions": ["metabase_inspect_schema", "metabase_ensure_analytics"]
    })

    assert "metabase_inspect_schema" in rules
    assert "metabase_ensure_analytics" in rules
    assert "Baserow PostgreSQL" in rules
    assert "query and dashboard readback" in rules
    assert "mw-admin-123" not in rules


def test_registered_code_server_scanner_actions_call_helper(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_TOOL_MODE", "routing")
    calls = []

    class StubClient:
        def __init__(self, container_name):
            calls.append(("init", container_name))

        def scan_docker_security(
            self,
            dockerfiles,
            audit_date,
            finding_id_prefix="HS",
        ):
            calls.append((
                "scan_docker_security",
                dockerfiles,
                audit_date,
                finding_id_prefix,
            ))
            return {"audit_rows": [], "secret_rows": []}

        def scan_project_dependencies(self, projects, scan_roots, ownership):
            calls.append(
                ("scan_project_dependencies", projects, scan_roots, ownership)
            )
            return {"team_ownership": [], "dependency_edges": []}

        def collect_test_metrics(self, projects):
            calls.append(("collect_test_metrics", projects))
            return {"projects": projects}

    apps = ["code-server"]
    tools, _meta = build_tools(
        apps,
        {
            "container_names": {"code-server": "rollout_0_code-server"},
        },
        tools_factory=FakeTools,
        code_server_client_cls=StubClient,
    )

    docker_action = tools.actions["code_scan_docker_security"]["func"]
    deps_action = tools.actions["code_scan_project_dependencies"]["func"]
    metrics_action = tools.actions["code_collect_test_metrics"]["func"]

    asyncio.run(docker_action(
        {"todo-api": "todo-api/Dockerfile"},
        "2026-04-12",
        "HS",
    ))
    asyncio.run(deps_action(
        ["todo-api"],
        {"todo-api": "todo-api/app"},
        {"todo-api": {"Owning Team": "Backend"}},
    ))
    metric_projects = [{
        "project": "data-analyzer",
        "path": "data-analyzer",
        "command": "pytest tests/ -v",
        "parser": "pytest",
        "test_globs": ["tests/**/*.py"],
    }]
    asyncio.run(metrics_action(metric_projects))

    assert (
        "scan_docker_security",
        {"todo-api": "todo-api/Dockerfile"},
        "2026-04-12",
        "HS",
    ) in calls
    assert (
        "scan_project_dependencies",
        ["todo-api"],
        {"todo-api": "todo-api/app"},
        {"todo-api": {"Owning Team": "Backend"}},
    ) in calls
    assert ("collect_test_metrics", metric_projects) in calls


def test_tool_system_rules_empty_without_actions():
    assert build_tool_system_rules({"actions": []}) is None
