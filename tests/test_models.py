import inspect

import pytest

from saas_agent import AgentConfig, AgentTask, run_agent


def test_task_renders_exact_app_urls_and_normalizes_apps():
    task = AgentTask(
        task_id="demo",
        prompt="Create the requested table.",
        apps=(" Baserow ", "baserow"),
        app_urls={"Baserow": "http://127.0.0.1:8000/"},
    )

    assert task.apps == ("baserow",)
    assert task.app_urls == {"baserow": "http://127.0.0.1:8000"}
    assert "- baserow: http://127.0.0.1:8000" in task.rendered_prompt()
    assert task.rendered_prompt().endswith("Create the requested table.")


def test_task_rejects_non_http_app_url():
    with pytest.raises(ValueError, match="absolute HTTP"):
        AgentTask(
            task_id="demo",
            prompt="Do work",
            app_urls={"baserow": "file:///tmp/state"},
        )


def test_task_mapping_uses_public_schema_only():
    task = AgentTask.from_mapping(
        {
            "id": "demo",
            "instruction": "Do work",
            "apps": ["siyuan"],
            "app_urls": {"siyuan": "https://notes.example.test"},
        }
    )

    assert task.task_id == "demo"
    assert task.apps == ("siyuan",)


def test_task_context_combines_urls_and_credentials_without_harness_ports():
    task = AgentTask(
        task_id="demo",
        prompt="Do work",
        app_urls={"twenty": "http://crm.example.test"},
        credentials={
            "twenty": {"username": "user@example.test", "password": "secret"}
        },
    )

    context = task.tool_context(
        {"container_names": {"twenty": "crm-container"}}
    )

    assert context["base_urls"] == {"twenty": "http://crm.example.test"}
    assert context["container_names"] == {"twenty": "crm-container"}
    assert context["credentials"]["twenty"]["username"] == "user@example.test"
    assert "port_map" not in context


def test_config_loads_agent_prefixed_environment(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_LLM_MODEL", "model-a")
    monkeypatch.setenv("SAAS_AGENT_LLM_BASE_URL", "https://llm.example.test/v1/")
    monkeypatch.setenv("SAAS_AGENT_LLM_API_KEY", "secret")
    monkeypatch.setenv("SAAS_AGENT_MAX_STEPS", "120")

    config = AgentConfig.from_env()

    assert config.model == "model-a"
    assert config.base_url == "https://llm.example.test/v1"
    assert config.max_steps == 120
    assert "secret" not in repr(config)


def test_task_repr_does_not_expose_app_credentials():
    task = AgentTask(
        task_id="private-task",
        prompt="Perform the requested operation.",
        credentials={
            "baserow": {
                "email": "agent@example.test",
                "password": "private-app-password",
            }
        },
    )

    assert "private-app-password" not in repr(task)


def test_config_is_not_required_at_package_import(monkeypatch):
    for name in (
        "SAAS_AGENT_LLM_MODEL",
        "SAAS_AGENT_LLM_BASE_URL",
        "SAAS_AGENT_LLM_API_KEY",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValueError, match="missing agent configuration"):
        AgentConfig.from_env()


def test_run_agent_public_signature_has_no_external_lifecycle_arguments():
    parameters = inspect.signature(run_agent).parameters

    assert set(parameters) == {
        "task",
        "config",
        "system_rules",
        "tool_context",
        "output_path",
    }
    assert not {
        "slot_id",
        "result_dir",
        "run_idx",
        "routing_meta",
    } & set(parameters)
