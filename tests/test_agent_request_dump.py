import asyncio
import base64
import importlib
import json
import sys
import types

import httpx


def _install_agent_import_stubs(monkeypatch):
    """Let tests import saas_agent.agent without browser-use/playwright installed."""
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.example.test")
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    class StubAgent:
        pass

    class StubBrowser:
        pass

    class StubChatOpenAI:
        async def ainvoke(self, messages, output_format=None, **kwargs):
            response = self._stub_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    class StubTools:
        pass

    class StubChatInvokeCompletion:
        def __init__(self, completion=None, usage=None, stop_reason=None):
            self.completion = completion
            self.usage = usage
            self.stop_reason = stop_reason

    class StubElement:
        async def fill(self, value: str, clear: bool = True) -> None:
            return None

    class StubDefaultActionWatchdog:
        async def on_SendKeysEvent(self, event):
            return None

    browser_use = types.ModuleType("browser_use")
    browser_use.Agent = StubAgent
    browser_use.Browser = StubBrowser
    browser_use.ChatOpenAI = StubChatOpenAI

    tools_service = types.ModuleType("browser_use.tools.service")
    tools_service.Tools = StubTools

    llm_views = types.ModuleType("browser_use.llm.views")
    llm_views.ChatInvokeCompletion = StubChatInvokeCompletion

    actor_element = types.ModuleType("browser_use.actor.element")
    actor_element.Element = StubElement

    default_action_watchdog = types.ModuleType(
        "browser_use.browser.watchdogs.default_action_watchdog"
    )
    default_action_watchdog.DefaultActionWatchdog = StubDefaultActionWatchdog

    playwright_async_api = types.ModuleType("playwright.async_api")
    playwright_async_api.async_playwright = object()

    for name, module in {
        "browser_use": browser_use,
        "browser_use.tools": types.ModuleType("browser_use.tools"),
        "browser_use.tools.service": tools_service,
        "browser_use.llm": types.ModuleType("browser_use.llm"),
        "browser_use.llm.views": llm_views,
        "browser_use.actor": types.ModuleType("browser_use.actor"),
        "browser_use.actor.element": actor_element,
        "browser_use.browser": types.ModuleType("browser_use.browser"),
        "browser_use.browser.watchdogs": types.ModuleType(
            "browser_use.browser.watchdogs"
        ),
        "browser_use.browser.watchdogs.default_action_watchdog": (
            default_action_watchdog
        ),
        "playwright": types.ModuleType("playwright"),
        "playwright.async_api": playwright_async_api,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    sys.modules.pop("saas_agent.agent", None)
    return importlib.import_module("saas_agent.agent")


def test_request_dump_hook_writes_first_full_http_body_once(tmp_path, monkeypatch):
    agent = _install_agent_import_stubs(monkeypatch)
    dump_path = tmp_path / "llm_request.json"
    monkeypatch.setenv("SAAS_AGENT_DUMP_LLM_REQUEST", str(dump_path))

    owner = types.SimpleNamespace(_request_dumped=False)
    hook = agent._make_request_dump_hook(owner)

    first_payload = {
        "model": "debug-model",
        "messages": [{"role": "user", "content": "first call"}],
    }
    second_payload = {
        "model": "debug-model",
        "messages": [{"role": "user", "content": "second call"}],
    }

    first_request = httpx.Request(
        "POST", "http://llm.example.test/v1/chat/completions", json=first_payload
    )
    second_request = httpx.Request(
        "POST", "http://llm.example.test/v1/chat/completions", json=second_payload
    )

    asyncio.run(hook(first_request))
    asyncio.run(hook(second_request))

    dumped = json.loads(dump_path.read_text(encoding="utf-8"))
    assert dumped["messages"] == first_payload["messages"]
    assert dumped["model"] == "debug-model"
    assert owner._request_dumped is True


def test_message_dump_saves_data_url_images_as_files(tmp_path, monkeypatch):
    agent = _install_agent_import_stubs(monkeypatch)
    monkeypatch.setenv("SAAS_AGENT_MSG_DUMP_DIR", str(tmp_path))

    owner = object.__new__(agent._CleanOutputChatOpenAI)
    owner._call_count = 0

    image_bytes = b"fake-png-bytes"
    image_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    messages = [
        types.SimpleNamespace(
            role="user",
            content=[
                {"type": "text", "text": "look at this page"},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        )
    ]

    owner._dump_messages(messages, tag="request")

    dumped = json.loads((tmp_path / "call_0001_request.json").read_text("utf-8"))
    image_part = dumped[0]["content"][1]
    assert image_part["image_url"]["url"] == "call_0001_request_img_001.png"
    assert image_part["image_url"]["original_chars"] == len(image_url)
    assert image_part["image_url"]["mime_type"] == "image/png"
    assert (tmp_path / "call_0001_request_img_001.png").read_bytes() == image_bytes


def test_message_dump_expands_model_dump_image_parts(tmp_path, monkeypatch):
    agent = _install_agent_import_stubs(monkeypatch)
    monkeypatch.setenv("SAAS_AGENT_MSG_DUMP_DIR", str(tmp_path))

    owner = object.__new__(agent._CleanOutputChatOpenAI)
    owner._call_count = 0

    image_bytes = b"fake-image-object-bytes"
    image_data = base64.b64encode(image_bytes).decode("ascii")

    class ImagePart:
        def __str__(self):
            return "🖼️  Image[image/png, detail=auto]: <base64 image/png>"

        def model_dump(self):
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": image_data,
                },
            }

    messages = [
        types.SimpleNamespace(
            role="user",
            content=[
                "observe",
                ImagePart(),
            ],
        )
    ]

    owner._dump_messages(messages, tag="request")

    dumped = json.loads((tmp_path / "call_0001_request.json").read_text("utf-8"))
    image_part = dumped[0]["content"][1]
    source = image_part["source"]
    assert source["data"] == "call_0001_request_img_001.png"
    assert source["saved_image"] == "call_0001_request_img_001.png"
    assert source["media_type"] == "image/png"
    assert source["original_chars"] == len(image_data)
    assert (tmp_path / "call_0001_request_img_001.png").read_bytes() == image_bytes


def test_response_dump_writes_model_completion_fields(tmp_path, monkeypatch):
    agent = _install_agent_import_stubs(monkeypatch)
    monkeypatch.setenv("SAAS_AGENT_MSG_DUMP_DIR", str(tmp_path))

    owner = object.__new__(agent._CleanOutputChatOpenAI)
    agent._current_request_id.set("req-123")
    result = types.SimpleNamespace(
        completion={
            "current_state": {"memory": "created vendor"},
            "action": [{"click": {"index": 7}}],
        },
        usage={"input_tokens": 10, "output_tokens": 20},
        stop_reason="stop",
    )

    owner._dump_response(
        "call_0001_request",
        result,
        phase="structured",
        output_format=None,
    )

    dumped = json.loads((tmp_path / "call_0001_response.json").read_text("utf-8"))
    assert dumped["request_file"] == "call_0001_request.json"
    assert dumped["request_id"] == "req-123"
    assert dumped["phase"] == "structured"
    assert dumped["completion"]["current_state"]["memory"] == "created vendor"
    assert dumped["completion"]["action"][0]["click"]["index"] == 7
    assert dumped["usage"]["input_tokens"] == 10
    assert dumped["stop_reason"] == "stop"


def test_message_recording_captures_request_and_response_when_enabled(monkeypatch):
    agent = _install_agent_import_stubs(monkeypatch)
    monkeypatch.setenv("SAAS_AGENT_RECORD_LLM_MESSAGES", "1")

    owner = object.__new__(agent._CleanOutputChatOpenAI)
    owner.message_calls = []
    agent._current_request_id.set("req-message-1")
    messages = [
        types.SimpleNamespace(role="system", content="rules"),
        types.SimpleNamespace(role="user", content="do the task"),
    ]
    result = types.SimpleNamespace(
        completion={"action": [{"done": {"success": True}}]},
        usage={"input_tokens": 12, "output_tokens": 4},
        stop_reason="stop",
    )

    owner._record_message_exchange(
        messages,
        result,
        phase="structured",
        output_format=None,
    )

    assert owner.message_calls == [
        {
            "call_index": 1,
            "request_id": "req-message-1",
            "phase": "structured",
            "output_format": None,
            "request": {
                "messages": [
                    {"role": "system", "content": "rules"},
                    {"role": "user", "content": "do the task"},
                ],
            },
            "response": {
                "completion": {"action": [{"done": {"success": True}}]},
                "usage": {"input_tokens": 12, "output_tokens": 4},
                "stop_reason": "stop",
            },
        }
    ]


def test_llm_message_result_payload_is_only_present_when_enabled(monkeypatch):
    agent = _install_agent_import_stubs(monkeypatch)
    owner = types.SimpleNamespace(message_calls=[{"call_index": 1}])

    monkeypatch.delenv("SAAS_AGENT_RECORD_LLM_MESSAGES", raising=False)
    assert agent._llm_messages_payload(owner) is None

    monkeypatch.setenv("SAAS_AGENT_RECORD_LLM_MESSAGES", "1")
    assert agent._llm_messages_payload(owner) == {
        "summary": {"calls": 1},
        "calls": [{"call_index": 1}],
    }


def test_usage_summary_accepts_openai_and_anthropic_token_fields(monkeypatch):
    agent = _install_agent_import_stubs(monkeypatch)

    calls = [
        {
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            }
        },
        {
            "usage": {
                "input_tokens": 5,
                "output_tokens": 3,
            }
        },
        {"usage": None},
    ]

    assert agent._summarize_llm_usage(calls) == {
        "calls": 3,
        "calls_with_usage": 2,
        "input_tokens": 16,
        "output_tokens": 10,
        "total_tokens": 26,
    }


def test_clean_chat_openai_records_per_call_usage(monkeypatch):
    agent = _install_agent_import_stubs(monkeypatch)

    owner = object.__new__(agent._CleanOutputChatOpenAI)
    agent._current_request_id.set("req-token-1")
    result = types.SimpleNamespace(
        usage={
            "prompt_tokens": 101,
            "completion_tokens": 17,
            "total_tokens": 118,
        },
    )

    owner._record_usage_call(result, phase="structured", output_format=None)

    assert owner.usage_calls == [
        {
            "call_index": 1,
            "request_id": "req-token-1",
            "phase": "structured",
            "output_format": None,
            "usage": {
                "prompt_tokens": 101,
                "completion_tokens": 17,
                "total_tokens": 118,
            },
            "input_tokens": 101,
            "output_tokens": 17,
            "total_tokens": 118,
        }
    ]


def test_timeout_config_is_parameterized_and_validated(monkeypatch):
    agent = _install_agent_import_stubs(monkeypatch)
    monkeypatch.setenv("SAAS_AGENT_LLM_API_TIMEOUT", "700")
    monkeypatch.setenv("SAAS_AGENT_LLM_STEP_TIMEOUT", "300")

    assert agent.get_llm_timeout_config() == {"api_s": 700, "step_s": 300}

    monkeypatch.setenv("SAAS_AGENT_LLM_STEP_TIMEOUT", "0")
    try:
        agent.get_llm_timeout_config()
    except RuntimeError as exc:
        assert "SAAS_AGENT_LLM_STEP_TIMEOUT must be a positive integer" in str(exc)
    else:
        raise AssertionError("invalid timeout was accepted")


def test_generation_config_is_parameterized_and_validated(monkeypatch):
    agent = _install_agent_import_stubs(monkeypatch)
    monkeypatch.setenv("SAAS_AGENT_LLM_MAX_COMPLETION_TOKENS", "16384")

    assert agent.get_llm_generation_config() == {
        "max_completion_tokens": 16384,
    }

    monkeypatch.setenv("SAAS_AGENT_LLM_MAX_COMPLETION_TOKENS", "invalid")
    try:
        agent.get_llm_generation_config()
    except RuntimeError as exc:
        assert "SAAS_AGENT_LLM_MAX_COMPLETION_TOKENS must be a positive integer" in str(exc)
    else:
        raise AssertionError("invalid completion token budget was accepted")


def _schema_test_owner(agent, responses):
    owner = object.__new__(agent._CleanOutputChatOpenAI)
    owner._stub_responses = list(responses)
    owner.request_ids = []
    owner.usage_calls = []
    owner.message_calls = []
    owner.schema_recovery_events = []
    owner._call_count = 0
    return owner


class _JsonOutputFormat:
    @classmethod
    def model_validate_json(cls, value):
        return json.loads(value)


def test_schema_recovery_failure_records_second_raw_response(monkeypatch):
    agent = _install_agent_import_stubs(monkeypatch)
    completion_cls = sys.modules["browser_use.llm.views"].ChatInvokeCompletion
    owner = _schema_test_owner(agent, [
        completion_cls("{", {"input_tokens": 10, "output_tokens": 1}, "stop"),
        completion_cls("", {"input_tokens": 11, "output_tokens": 0}, "stop"),
    ])

    try:
        asyncio.run(owner.ainvoke([], output_format=_JsonOutputFormat))
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("invalid recovery output was accepted")

    summary = agent._summarize_schema_recovery(owner.schema_recovery_events)
    assert summary["attempts"] == 1
    assert summary["failures"] == 1
    assert summary["failure_types"] == ["JSONDecodeError"]
    assert [call["phase"] for call in owner.usage_calls] == [
        "structured_parse_error",
        "recovered_parse_error",
    ]
    assert owner.usage_calls[1]["input_tokens"] == 11


def test_schema_recovery_success_is_counted_once(monkeypatch):
    agent = _install_agent_import_stubs(monkeypatch)
    completion_cls = sys.modules["browser_use.llm.views"].ChatInvokeCompletion
    owner = _schema_test_owner(agent, [
        completion_cls("not-json", {"input_tokens": 10}, "stop"),
        completion_cls('{"action": []}', {"input_tokens": 11}, "stop"),
    ])

    result = asyncio.run(owner.ainvoke([], output_format=_JsonOutputFormat))

    assert result.completion == {"action": []}
    summary = agent._summarize_schema_recovery(owner.schema_recovery_events)
    assert summary["attempts"] == 1
    assert summary["successes"] == 1
    assert summary["failures"] == 0
