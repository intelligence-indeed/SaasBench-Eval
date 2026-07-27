"""Standalone browser-use runtime with routing, observability, and guardrails."""

import asyncio
import base64
import binascii
import json
import inspect
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
from contextvars import ContextVar
from collections.abc import Mapping
from dataclasses import dataclass as _dataclass
from pathlib import Path

import httpx
from browser_use import Agent, Browser, ChatOpenAI
from browser_use.llm.views import ChatInvokeCompletion
from playwright.async_api import async_playwright
from typing import Any

from saas_agent.models import AgentConfig, AgentTask
from saas_agent.prompt_routes import build_prompt_rules
from saas_agent.termination import classify_termination
from saas_agent.tool_routes import build_tool_system_rules, build_tools
from saas_agent.loop_guard import ActionLoopGuard


# All transient run data lives under one caller-overridable directory.
_TMP_BASE = os.environ.get(
    "SAAS_AGENT_TMP",
    os.path.join(tempfile.gettempdir(), "saas_agent"),
)
os.makedirs(_TMP_BASE, exist_ok=True)


def _patch_xterm_fill() -> None:
    """Monkey-patch Element.fill to use CDP insertText for xterm.js terminals.

    xterm.js handles both 'keyDown' and 'char' CDP events, causing each
    keystroke to appear twice when browser-use types character-by-character.
    insertText bypasses the keydown chain entirely, so no doubling occurs.
    """
    from browser_use.actor.element import Element

    original_fill = Element.fill

    async def _xterm_aware_fill(self, value: str, clear: bool = True) -> None:
        is_xterm = False
        try:
            result = await self._client.send.DOM.describeNode(
                params={"backendNodeId": self._backend_node_id},
                session_id=self._session_id,
            )
            attrs = result.get("node", {}).get("attributes", [])
            # attributes is a flat list: [name, value, name, value, ...]
            for i in range(0, len(attrs) - 1, 2):
                if attrs[i] == "class" and "xterm" in attrs[i + 1]:
                    is_xterm = True
                    break
        except Exception:
            pass

        if is_xterm:
            # Focus via DOM.focus to avoid triggering xterm mouse handlers
            try:
                await self._client.send.DOM.focus(
                    params={"backendNodeId": self._backend_node_id},
                    session_id=self._session_id,
                )
                await asyncio.sleep(0.02)
            except Exception:
                pass
            await self._client.send.Input.insertText(
                params={"text": value},
                session_id=self._session_id,
            )
        else:
            await original_fill(self, value, clear)

    Element.fill = _xterm_aware_fill  # type: ignore[method-assign]


_patch_xterm_fill()


def _patch_xterm_send_keys() -> None:
    """Monkey-patch DefaultActionWatchdog.on_SendKeysEvent for xterm.js terminals.

    The existing _patch_xterm_fill only covers the `input` action (Element.fill path).
    When the agent uses `send_keys` with plain text (e.g. typing a shell command),
    the text goes through on_SendKeysEvent which dispatches keyDown+char+keyUp per
    character.  xterm.js processes both keyDown and char, causing each character to
    appear twice.

    This patch intercepts plain-text send_keys: if the browser's focused element is
    inside an xterm container, we use CDP Input.insertText instead (same fix as fill).
    Modifier combos (Ctrl+C) and special keys (Enter, Escape) are left untouched.
    """
    from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog

    _original_on_send_keys = DefaultActionWatchdog.on_SendKeysEvent

    _SPECIAL_KEYS = frozenset({
        "Enter", "Tab", "Delete", "Backspace", "Escape",
        "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
        "PageUp", "PageDown", "Home", "End",
        "Control", "Alt", "Meta", "Shift",
        "F1", "F2", "F3", "F4", "F5", "F6",
        "F7", "F8", "F9", "F10", "F11", "F12",
    })

    _KEY_ALIASES = {
        "enter": "Enter", "return": "Enter", "tab": "Tab",
        "escape": "Escape", "esc": "Escape", "backspace": "Backspace",
        "delete": "Delete", "space": " ",
        "up": "ArrowUp", "down": "ArrowDown",
        "left": "ArrowLeft", "right": "ArrowRight",
        "pageup": "PageUp", "pagedown": "PageDown",
        "home": "Home", "end": "End",
    }

    async def on_SendKeysEvent(self, event):  # type: ignore[override]
        keys: str = event.keys

        # Key combos (Ctrl+C, etc.) → always use original handler
        if "+" in keys:
            return await _original_on_send_keys(self, event)

        # Normalise single key name
        normalised = _KEY_ALIASES.get(keys.strip().lower(), keys)

        # Special / modifier keys → always use original handler
        if normalised in _SPECIAL_KEYS:
            return await _original_on_send_keys(self, event)

        # Plain text — check whether the focused element lives inside xterm.js
        try:
            cdp_session = await self.browser_session.get_or_create_cdp_session(
                focus=True,
            )
            result = await cdp_session.cdp_client.send.Runtime.evaluate(
                params={
                    "expression": (
                        "!!(document.activeElement && "
                        "document.activeElement.closest('.xterm'))"
                    ),
                },
                session_id=cdp_session.session_id,
            )
            is_xterm = result.get("result", {}).get("value") is True
        except Exception:
            is_xterm = False

        if is_xterm:
            # Bypass keyDown+char and use insertText (no doubling)
            await cdp_session.cdp_client.send.Input.insertText(
                params={"text": keys},
                session_id=cdp_session.session_id,
            )
            self.logger.info(f"⌨️ Sent keys via insertText (xterm): {keys}")
            if "enter" in keys.lower() or "\n" in keys or "\r" in keys:
                await asyncio.sleep(0.1)
            return

        # Not xterm → fall back to original handler
        return await _original_on_send_keys(self, event)

    DefaultActionWatchdog.on_SendKeysEvent = on_SendKeysEvent  # type: ignore[assignment]


_patch_xterm_send_keys()


def _strip_tool_call_wrapper(content: str) -> str:
    """Strip common LLM output decorations so we get raw JSON.

    Handles (in order):
      - ```json ... ``` / ``` ... ``` markdown fences
      - <thinking>...</thinking> blocks (Claude reasoning leaks)
      - <tool_call>/<tool_calls>/<json_schema> XML wrappers
      - Leading natural-language text before the first '{'
      - Trailing characters after the last balanced '}'
    """
    if not content:
        return content

    # 1. Extract from markdown code fence if present.
    fence_match = re.search(r'```(?:json|JSON)?\s*\n?(.*?)\n?```', content, re.DOTALL)
    if fence_match:
        content = fence_match.group(1)

    # 2. Drop <thinking> blocks entirely (content inside is reasoning, not JSON).
    content = re.sub(r'<thinking>.*?</thinking>', '', content, flags=re.DOTALL | re.IGNORECASE)

    # 3. Drop XML tag wrappers Claude sometimes emits.
    content = re.sub(
        r'</?(?:tool_calls?|json_schema|response|output|answer)\s*/?>',
        '',
        content,
        flags=re.IGNORECASE,
    )

    # 4. Trim to first '{'.
    idx = content.find('{')
    if idx > 0:
        content = content[idx:]

    # 5. Trim trailing junk after last balanced '}'.
    content = content.strip()
    if content.startswith('{'):
        depth = 0
        last_close = -1
        in_str = False
        escape = False
        for i, ch in enumerate(content):
            if escape:
                escape = False
                continue
            if ch == '\\' and in_str:
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    last_close = i
                    break
        if last_close > 0 and last_close < len(content) - 1:
            content = content[: last_close + 1]

    return content.strip()


_BANNED_ACTIONS = {"evaluate"}


def _rewrite_banned_actions(text: str) -> str:
    """Rewrite banned actions (e.g. evaluate) to 'think' so Pydantic validation passes."""
    try:
        obj = json.loads(text)
        actions = obj.get("action", [])
        if not isinstance(actions, list):
            return text
        changed = False
        for act in actions:
            if not isinstance(act, dict):
                continue
            for banned in _BANNED_ACTIONS:
                if banned in act:
                    payload = act.pop(banned)
                    act["think"] = (
                        f"{banned} action is disabled. "
                        f"Use click/input/send_keys instead. Wanted: {str(payload)[:200]}"
                    )
                    changed = True
                    break
        if changed:
            return json.dumps(obj, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return text


# ContextVar that each httpx response hook writes the request id into.
# One slot per async task — safe under concurrent workers.
_current_request_id: ContextVar[str | None] = ContextVar("_current_request_id", default=None)


# Header names that providers use for the per-call request id.  Listed in
# preference order; the first non-empty match wins.  Note: httpx normalises
# headers to lowercase, so we compare lower-cased names.
_REQUEST_ID_HEADERS = (
    "http_x_reqid",     # qnaigc / APISIX gateway (literal header name)
    "x-reqid",          # alternative gateway form
    "x-request-id",     # OpenAI / Anthropic standard
    "x-amzn-requestid", # AWS Bedrock
    "request-id",
    "x-tt-logid",       # ByteDance / Volc
)


def _make_request_id_hook():
    """Return an httpx event hook that captures the request id into the ContextVar."""
    async def _capture(response: httpx.Response) -> None:
        for name in _REQUEST_ID_HEADERS:
            rid = response.headers.get(name)
            if rid:
                _current_request_id.set(rid)
                return
    return _capture


def _make_request_dump_hook(owner):
    """Return an httpx request hook that dumps the first full LLM HTTP body.

    Enable with:
        SAAS_AGENT_DUMP_LLM_REQUEST=/absolute/path/to/llm_request.json

    This runs at the HTTP layer, after ChatOpenAI/browser-use have converted
    their internal message objects into the final OpenAI-compatible request
    JSON. It intentionally writes only the first request per LLM instance so a
    short debug run can capture one complete interaction without generating a
    large pile of screenshot-heavy JSON files.
    """

    async def _dump(request: httpx.Request) -> None:
        dump_path = os.environ.get("SAAS_AGENT_DUMP_LLM_REQUEST", "")
        if not dump_path or getattr(owner, "_request_dumped", False):
            return

        body = await request.aread()
        target = Path(dump_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            target.write_bytes(body)
        else:
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        owner._request_dumped = True

    return _dump


_IMAGE_DUMP_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _save_message_dump_base64(
    dump_dir: Path,
    file_stem: str,
    image_index: int,
    mime_type: str,
    encoded: str,
) -> tuple[str, str] | None:
    mime_type = mime_type.lower()
    ext = _IMAGE_DUMP_EXTENSIONS.get(mime_type, ".bin")
    try:
        image_bytes = base64.b64decode(encoded, validate=False)
    except (binascii.Error, ValueError):
        return None

    image_name = f"{file_stem}_img_{image_index:03d}{ext}"
    (dump_dir / image_name).write_bytes(image_bytes)
    return image_name, mime_type


def _save_message_dump_image(
    dump_dir: Path,
    file_stem: str,
    image_index: int,
    data_url: str,
) -> tuple[str, str] | None:
    match = re.match(r"^data:([^;,]+);base64,(.*)$", data_url, flags=re.DOTALL)
    if not match:
        return None

    mime_type = match.group(1).lower()
    return _save_message_dump_base64(
        dump_dir,
        file_stem,
        image_index,
        mime_type,
        match.group(2),
    )


def _dump_jsonable(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _dump_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump_jsonable(v) for v in value]

    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            return _dump_jsonable(method())
        except Exception:
            pass

    return str(value)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _record_llm_messages_enabled() -> bool:
    return _env_truthy("SAAS_AGENT_RECORD_LLM_MESSAGES")


def _serialize_llm_messages(messages: list) -> list[dict[str, Any]]:
    serializable = []
    for msg in messages:
        entry = {
            "role": getattr(msg, "role", "unknown"),
            "content": _dump_jsonable(getattr(msg, "content", "")),
        }
        if hasattr(msg, "tool_calls") and getattr(msg, "tool_calls"):
            entry["tool_calls"] = _dump_jsonable(getattr(msg, "tool_calls"))
        if hasattr(msg, "name") and getattr(msg, "name"):
            entry["name"] = _dump_jsonable(getattr(msg, "name"))
        serializable.append(entry)
    return serializable


def _llm_messages_payload(llm) -> dict | None:
    if not _record_llm_messages_enabled():
        return None
    calls = getattr(llm, "message_calls", []) if llm is not None else []
    return {
        "summary": {"calls": len(calls)},
        "calls": calls,
    }


def _token_count_value(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _first_token_count(usage: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _token_count_value(usage.get(key))
        if value is not None:
            return value
    return None


def _usage_token_counts(usage) -> dict[str, int]:
    usage = _dump_jsonable(usage)
    if not isinstance(usage, dict):
        return {}

    input_tokens = _first_token_count(usage, ("input_tokens", "prompt_tokens"))
    output_tokens = _first_token_count(
        usage, ("output_tokens", "completion_tokens")
    )
    total_tokens = _first_token_count(usage, ("total_tokens",))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    counts: dict[str, int] = {}
    if input_tokens is not None:
        counts["input_tokens"] = input_tokens
    if output_tokens is not None:
        counts["output_tokens"] = output_tokens
    if total_tokens is not None:
        counts["total_tokens"] = total_tokens
    return counts


def _output_format_name(output_format) -> str | None:
    if output_format is None:
        return None
    return (
        getattr(output_format, "__name__", None)
        or getattr(output_format, "__qualname__", None)
        or str(output_format)
    )


def _summarize_llm_usage(calls: list[dict]) -> dict[str, int]:
    summary = {
        "calls": len(calls),
        "calls_with_usage": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    for call in calls:
        usage = call.get("usage")
        counts = _usage_token_counts(usage)
        if counts:
            summary["calls_with_usage"] += 1
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            summary[key] += counts.get(key, 0)
    return summary


def _summarize_schema_recovery(events: list[dict]) -> dict:
    failures = [event for event in events if not event.get("success")]
    return {
        "attempts": len(events),
        "successes": len(events) - len(failures),
        "failures": len(failures),
        "failure_types": sorted({
            str(event.get("error_type"))
            for event in failures
            if event.get("error_type")
        }),
        "events": events,
    }


def _positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}")
    return value


def get_llm_timeout_config() -> dict[str, int]:
    return {
        "api_s": _positive_env_int("SAAS_AGENT_LLM_API_TIMEOUT", 600),
        "step_s": _positive_env_int("SAAS_AGENT_LLM_STEP_TIMEOUT", 150),
    }


def get_llm_generation_config() -> dict[str, int]:
    return {
        "max_completion_tokens": _positive_env_int(
            "SAAS_AGENT_LLM_MAX_COMPLETION_TOKENS", 8192
        ),
    }


@_dataclass
class _CleanOutputChatOpenAI(ChatOpenAI):
    """ChatOpenAI that:
    - cleans Claude's decorated output before JSON validation
    - captures the per-call request id (X-Reqid / x-request-id / etc.) from
      the HTTP response headers via an httpx event hook

    request_ids is populated in order: one entry per ainvoke call, None when
    no known header was present.  _extract_trajectory zips these with history
    steps so each trajectory step carries the id of the API call that
    produced it.

    New flow:
      1. Try the parent's normal path (fast happy path).
      2. On any parsing failure, fetch the raw completion (output_format=None),
         clean it, rewrite banned actions, then validate ourselves.
    """

    def __post_init__(self):
        self.request_ids: list[str | None] = []
        self.usage_calls: list[dict] = []
        self.message_calls: list[dict] = []
        self.schema_recovery_events: list[dict] = []
        self._call_count = 0  # Stable sequence number for dump filenames.
        self._request_dumped = False
        # Build a single reusable httpx.AsyncClient with the capture hook so
        # get_client() returns the same transport every call within this instance.
        self._http_client = httpx.AsyncClient(
            event_hooks={
                "request": [_make_request_dump_hook(self)],
                "response": [_make_request_id_hook()],
            },
        )
        # Inject into the dataclass field that ChatOpenAI.get_client() reads.
        object.__setattr__(self, "http_client", self._http_client)

    def _record_schema_recovery(
        self,
        *,
        success: bool,
        initial_error: Exception | None,
        error: Exception | None = None,
    ) -> None:
        if not hasattr(self, "schema_recovery_events"):
            self.schema_recovery_events = []
        event = {
            "attempt_index": len(self.schema_recovery_events) + 1,
            "request_id": _current_request_id.get(),
            "success": success,
            "initial_error_type": (
                type(initial_error).__name__ if initial_error is not None else None
            ),
            "initial_error": str(initial_error)[:500] if initial_error else None,
            "error_type": type(error).__name__ if error is not None else None,
            "error": str(error)[:500] if error is not None else None,
        }
        self.schema_recovery_events.append(event)

    def _record_usage_call(self, result, *, phase: str, output_format) -> None:
        if not hasattr(self, "usage_calls"):
            self.usage_calls = []
        usage = (
            _dump_jsonable(getattr(result, "usage", None))
            if result is not None
            else None
        )
        record = {
            "call_index": len(self.usage_calls) + 1,
            "request_id": _current_request_id.get(),
            "phase": phase,
            "output_format": _output_format_name(output_format),
            "usage": usage,
        }
        record.update(_usage_token_counts(usage))
        self.usage_calls.append(record)

    def _record_message_exchange(
        self,
        messages,
        result,
        *,
        phase: str,
        output_format,
        extra: dict | None = None,
        error: Exception | None = None,
    ) -> None:
        if not _record_llm_messages_enabled():
            return
        if not hasattr(self, "message_calls"):
            self.message_calls = []

        response = {
            "completion": _dump_jsonable(getattr(result, "completion", None))
            if result is not None
            else None,
            "usage": _dump_jsonable(getattr(result, "usage", None))
            if result is not None
            else None,
            "stop_reason": _dump_jsonable(getattr(result, "stop_reason", None))
            if result is not None
            else None,
        }
        if error is not None:
            response["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        if extra:
            response.update(_dump_jsonable(extra))

        self.message_calls.append({
            "call_index": len(self.message_calls) + 1,
            "request_id": _current_request_id.get(),
            "phase": phase,
            "output_format": _output_format_name(output_format),
            "request": {"messages": _serialize_llm_messages(messages)},
            "response": response,
        })

    def _dump_messages(self, messages: list, tag: str = "") -> str | None:
        """Serialize the complete model message list to a JSON file.

        SAAS_AGENT_MSG_DUMP_DIR controls the destination. Embedded base64
        images are truncated to keep dump files bounded.
        """
        dump_dir = os.environ.get("SAAS_AGENT_MSG_DUMP_DIR", "")
        if not dump_dir:
            return None
        dump_path = Path(dump_dir)
        dump_path.mkdir(parents=True, exist_ok=True)
        self._call_count += 1
        suffix = f"_{tag}" if tag else ""
        file_stem = f"call_{self._call_count:04d}{suffix}"
        image_counter = 0
        save_images = os.environ.get("SAAS_AGENT_MSG_DUMP_SAVE_IMAGES", "1").lower() not in {
            "0",
            "false",
            "no",
        }

        def _serialize_content(content):
            """Serialize content while bounding embedded base64 image data."""
            nonlocal image_counter
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                result = []
                for part in content:
                    if isinstance(part, dict):
                        part = dict(part)  # shallow copy
                        # Retain the image type and prefix without the full payload.
                        if part.get("type") == "image_url":
                            raw_image_url = part.get("image_url") or {}
                            if isinstance(raw_image_url, dict):
                                image_url = dict(raw_image_url)
                                url = image_url.get("url", "")
                            else:
                                image_url = {"url": str(raw_image_url)}
                                url = str(raw_image_url)
                            if url.startswith("data:"):
                                saved = None
                                if save_images:
                                    image_counter += 1
                                    saved = _save_message_dump_image(
                                        dump_path,
                                        file_stem,
                                        image_counter,
                                        url,
                                    )
                                if saved:
                                    image_name, mime_type = saved
                                    image_url["url"] = image_name
                                    image_url["mime_type"] = mime_type
                                    image_url["original_chars"] = len(url)
                                else:
                                    image_url["url"] = (
                                        url[:80]
                                        + f"...[truncated, {len(url)} chars]"
                                    )
                                part["image_url"] = image_url
                        result.append(part)
                    else:
                        result.append(str(part))
                return result
            return str(content)

        def _to_jsonable(value):
            if isinstance(value, (str, int, float, bool)) or value is None:
                return value
            if isinstance(value, dict):
                return {str(k): _to_jsonable(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [_to_jsonable(v) for v in value]

            for method_name in ("model_dump", "dict"):
                method = getattr(value, method_name, None)
                if not callable(method):
                    continue
                try:
                    return _to_jsonable(method())
                except Exception:
                    pass

            return str(value)

        def _save_data_url(url: str) -> tuple[str, str] | None:
            nonlocal image_counter
            if not (save_images and url.startswith("data:")):
                return None
            image_counter += 1
            saved = _save_message_dump_image(dump_path, file_stem, image_counter, url)
            if saved is None:
                image_counter -= 1
            return saved

        def _save_base64_image(mime_type: str, encoded: str) -> tuple[str, str] | None:
            nonlocal image_counter
            if not (save_images and mime_type.startswith("image/") and encoded):
                return None
            image_counter += 1
            saved = _save_message_dump_base64(
                dump_path,
                file_stem,
                image_counter,
                mime_type,
                encoded,
            )
            if saved is None:
                image_counter -= 1
            return saved

        def _serialize_content(content):
            value = _to_jsonable(content)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                return [_serialize_content(part) for part in value]
            if not isinstance(value, dict):
                return value

            part = {k: _serialize_content(v) for k, v in value.items()}

            raw_image_url = part.get("image_url")
            if raw_image_url is not None:
                if isinstance(raw_image_url, dict):
                    image_url = dict(raw_image_url)
                    url = image_url.get("url", "")
                else:
                    image_url = {"url": str(raw_image_url)}
                    url = str(raw_image_url)
                if isinstance(url, str) and url.startswith("data:"):
                    saved = _save_data_url(url)
                    if saved:
                        image_name, mime_type = saved
                        image_url["url"] = image_name
                        image_url["mime_type"] = mime_type
                        image_url["original_chars"] = len(url)
                    else:
                        image_url["url"] = (
                            url[:80] + f"...[truncated, {len(url)} chars]"
                        )
                    part["image_url"] = image_url

            source = part.get("source")
            if isinstance(source, dict):
                media_type = str(
                    source.get("media_type") or source.get("mime_type") or ""
                ).lower()
                data = source.get("data")
                if isinstance(data, str):
                    saved = _save_base64_image(media_type, data)
                    if saved:
                        image_name, mime_type = saved
                        source["data"] = image_name
                        source["saved_image"] = image_name
                        source["media_type"] = mime_type
                        source["original_chars"] = len(data)
                    elif media_type.startswith("image/") and len(data) > 80:
                        source["data"] = (
                            data[:80] + f"...[truncated, {len(data)} chars]"
                        )
                    part["source"] = source

            return part

        serializable = []
        for msg in messages:
            entry = {
                "role": getattr(msg, "role", "unknown"),
                "content": _serialize_content(getattr(msg, "content", "")),
            }
            # Assistant messages may contain tool calls.
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                entry["tool_calls"] = [
                    tc.model_dump() if hasattr(tc, "model_dump") else str(tc)
                    for tc in msg.tool_calls
                ]
            serializable.append(entry)

        out_path = dump_path / f"{file_stem}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2, default=str)
        return file_stem

    def _dump_response(
        self,
        request_file_stem: str | None,
        result,
        *,
        phase: str,
        output_format=None,
        extra: dict | None = None,
        error: Exception | None = None,
    ) -> None:
        dump_dir = os.environ.get("SAAS_AGENT_MSG_DUMP_DIR", "")
        if not dump_dir or not request_file_stem:
            return

        dump_path = Path(dump_dir)
        dump_path.mkdir(parents=True, exist_ok=True)
        if request_file_stem.endswith("_request"):
            response_file_stem = request_file_stem[:-len("_request")] + "_response"
        else:
            response_file_stem = request_file_stem + "_response"

        payload = {
            "request_file": f"{request_file_stem}.json",
            "request_id": _current_request_id.get(),
            "phase": phase,
            "output_format": getattr(output_format, "__name__", None)
            or getattr(output_format, "__qualname__", None)
            or str(output_format)
            if output_format is not None
            else None,
            "completion": _dump_jsonable(getattr(result, "completion", None))
            if result is not None
            else None,
            "usage": _dump_jsonable(getattr(result, "usage", None))
            if result is not None
            else None,
            "stop_reason": _dump_jsonable(getattr(result, "stop_reason", None))
            if result is not None
            else None,
        }
        if error is not None:
            payload["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        if extra:
            payload.update(_dump_jsonable(extra))

        out_path = dump_path / f"{response_file_stem}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    async def ainvoke(self, messages, output_format=None, **kwargs) -> Any:
        # Reset the slot so we get a fresh id for this call.
        _current_request_id.set(None)

        # Dump messages before LLM call for debugging/inspection
        request_file_stem = self._dump_messages(messages, tag="request")

        if output_format is None:
            result = await super().ainvoke(messages, output_format=None, **kwargs)
            self._record_usage_call(result, phase="raw", output_format=output_format)
            self.request_ids.append(_current_request_id.get())
            self._record_message_exchange(
                messages,
                result,
                phase="raw",
                output_format=output_format,
            )
            self._dump_response(
                request_file_stem,
                result,
                phase="raw",
                output_format=output_format,
            )
            return result

        # Happy path: let parent try first.
        parse_error = None
        structured_result = None
        try:
            result: ChatInvokeCompletion = await super().ainvoke(
                messages, output_format=output_format, **kwargs
            )
            structured_result = result
            if not isinstance(result.completion, str):
                self._record_usage_call(
                    result, phase="structured", output_format=output_format
                )
                self.request_ids.append(_current_request_id.get())
                self._record_message_exchange(
                    messages,
                    result,
                    phase="structured",
                    output_format=output_format,
                )
                self._dump_response(
                    request_file_stem,
                    result,
                    phase="structured",
                    output_format=output_format,
                )
                return result
            # Parent returned raw string (e.g. dont_force_structured_output=True path)
            cleaned = _strip_tool_call_wrapper(result.completion)
            cleaned = _rewrite_banned_actions(cleaned)
            parsed = output_format.model_validate_json(cleaned)
            self.request_ids.append(_current_request_id.get())
            final_result = ChatInvokeCompletion(
                completion=parsed,
                usage=result.usage,
                stop_reason=result.stop_reason,
            )
            self._record_usage_call(
                final_result,
                phase="structured_cleaned",
                output_format=output_format,
            )
            self._record_message_exchange(
                messages,
                final_result,
                phase="structured_cleaned",
                output_format=output_format,
                extra={
                    "raw_completion": result.completion,
                    "cleaned_completion": cleaned,
                },
            )
            self._dump_response(
                request_file_stem,
                final_result,
                phase="structured_cleaned",
                output_format=output_format,
                extra={
                    "raw_completion": result.completion,
                    "cleaned_completion": cleaned,
                },
            )
            return final_result
        except Exception as e:
            # Fall through to recovery: re-fetch as raw string and clean.
            parse_error = e
            if structured_result is not None:
                self._record_usage_call(
                    structured_result,
                    phase="structured_parse_error",
                    output_format=output_format,
                )
                self._record_message_exchange(
                    messages,
                    structured_result,
                    phase="structured_parse_error",
                    output_format=output_format,
                    error=parse_error,
                )

        raw_result = None
        raw_completion = ""
        cleaned = ""
        try:
            raw_result = await super().ainvoke(
                messages, output_format=None, **kwargs
            )
            raw_completion = raw_result.completion
            if not isinstance(raw_completion, str):
                raw_completion = str(raw_completion)

            cleaned = _strip_tool_call_wrapper(raw_completion)
            cleaned = _rewrite_banned_actions(cleaned)
            parsed = output_format.model_validate_json(cleaned)
        except Exception as recovery_error:
            if raw_result is not None:
                self._record_usage_call(
                    raw_result,
                    phase="recovered_parse_error",
                    output_format=output_format,
                )
            self._record_schema_recovery(
                success=False,
                initial_error=parse_error,
                error=recovery_error,
            )
            recovery_extra = {
                "initial_error": {
                    "type": type(parse_error).__name__,
                    "message": str(parse_error),
                }
                if parse_error is not None
                else None,
                "raw_completion": raw_completion,
                "cleaned_completion": cleaned,
            }
            self._record_message_exchange(
                messages,
                raw_result,
                phase="recovered_parse_error",
                output_format=output_format,
                extra=recovery_extra,
                error=recovery_error,
            )
            self._dump_response(
                request_file_stem,
                raw_result,
                phase="recovered_parse_error",
                output_format=output_format,
                extra=recovery_extra,
                error=recovery_error,
            )
            raise

        self.request_ids.append(_current_request_id.get())
        final_result = ChatInvokeCompletion(
            completion=parsed,
            usage=raw_result.usage,
            stop_reason=raw_result.stop_reason,
        )
        self._record_usage_call(
            final_result, phase="recovered", output_format=output_format
        )
        self._record_schema_recovery(
            success=True,
            initial_error=parse_error,
        )
        recovery_extra = {
            "initial_error": {
                "type": type(parse_error).__name__,
                "message": str(parse_error),
            }
            if parse_error is not None
            else None,
            "raw_completion": raw_completion,
            "cleaned_completion": cleaned,
        }
        self._record_message_exchange(
            messages,
            final_result,
            phase="recovered",
            output_format=output_format,
            extra=recovery_extra,
        )
        self._dump_response(
            request_file_stem,
            final_result,
            phase="recovered",
            output_format=output_format,
            extra=recovery_extra,
        )
        return final_result



def _build_llm(
    model_name: str,
    base_url: str,
    api_key: str,
    timeout_config: dict[str, int] | None = None,
    generation_config: dict[str, int] | None = None,
) -> _CleanOutputChatOpenAI:
    timeout_config = timeout_config or get_llm_timeout_config()
    generation_config = generation_config or get_llm_generation_config()
    reasoning_effort = None
    if ":" in model_name:
        base, suffix = model_name.rsplit(":", 1)
        if suffix in {"minimal", "low", "medium", "high"}:
            model_name = base
            reasoning_effort = suffix
    kwargs: dict = {}
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    kwargs.update(generation_config)
    return _CleanOutputChatOpenAI(
        model=model_name,
        base_url=base_url,
        api_key=api_key,
        timeout=timeout_config["api_s"],
        max_retries=5,
        dont_force_structured_output=True,
        add_schema_to_system_prompt=True,
        **kwargs,
    )


def _free_port() -> int:
    """Pick a random port in 40000-59999 that is not currently in use."""
    for _ in range(100):
        port = random.randint(40000, 59999)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("Could not find a free port for Chrome CDP")


_CHROME_TMP_BASE = os.path.join(_TMP_BASE, "chrome")


def _start_chrome(
    executable_path: str,
    port: int,
    *,
    headless: bool = True,
) -> tuple[subprocess.Popen, str]:
    user_data = f"{_CHROME_TMP_BASE}_{port}_{int(time.time())}"
    os.makedirs(user_data, exist_ok=True)
    command = [
        executable_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "about:blank",
    ]
    if headless:
        command.insert(-1, "--headless=new")
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        command,
        **popen_kwargs,
    )
    # Poll until Chrome CDP is ready (up to 120s)
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1)
            return proc, user_data
        except Exception:
            time.sleep(0.5)
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        proc.kill()
    shutil.rmtree(user_data, ignore_errors=True)
    raise RuntimeError(f"Chrome CDP port {port} not ready after 120s")


async def _kill(proc, browser) -> None:
    if browser:
        try:
            await asyncio.wait_for(browser.close(), timeout=5)
        except BaseException:
            pass
    if proc and getattr(proc, "poll", lambda: None)() is None:
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _extract_trajectory(history, request_ids: list[str | None] | None = None) -> list[dict]:
    """Serialize history.history into a list of step dicts for analysis."""
    steps = []
    for i, step in enumerate(history.history):
        entry: dict = {"step": i + 1}

        # Browser state: URL + title
        if hasattr(step, "state") and step.state:
            entry["url"] = getattr(step.state, "url", None)
            entry["title"] = getattr(step.state, "title", None)

        # Agent thought
        if step.model_output and hasattr(step.model_output, "current_state"):
            brain = step.model_output.current_state
            entry["thought"] = {
                "evaluation": getattr(brain, "evaluation_previous_goal", None),
                "memory": getattr(brain, "memory", None),
                "next_goal": getattr(brain, "next_goal", None),
            }

        # Actions taken this step
        if step.model_output and hasattr(step.model_output, "action"):
            actions = []
            for action in step.model_output.action:
                try:
                    actions.append(action.model_dump(exclude_none=True))
                except Exception:
                    actions.append(str(action))
            entry["actions"] = actions

        # Results (errors, extracted content, done signal)
        results = step.result if isinstance(step.result, list) else ([step.result] if step.result else [])
        entry["results"] = [
            {
                k: v for k, v in {
                    "error": getattr(r, "error", None),
                    "extracted_content": getattr(r, "extracted_content", None),
                    "is_done": getattr(r, "is_done", None),
                    "success": getattr(r, "success", None),
                }.items() if v is not None
            }
            for r in results
        ]

        # API request id (x-request-id from HTTP response header)
        if request_ids is not None and i < len(request_ids) and request_ids[i] is not None:
            entry["request_id"] = request_ids[i]

        steps.append(entry)
    return steps


async def run_agent(
    task: AgentTask | Mapping[str, Any],
    config: AgentConfig | None = None,
    *,
    system_rules: str | None = None,
    tool_context: Mapping[str, Any] | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Execute one standalone SaaS task and return a serializable result.

    The caller owns application lifecycle, authentication availability, and
    evaluation. This function starts only its browser process and never starts
    application containers, allocates worker slots, or invokes an evaluator.
    """

    task_spec = task if isinstance(task, AgentTask) else AgentTask.from_mapping(task)
    run_config = config or AgentConfig.from_env()
    task_id = task_spec.task_id
    port = _free_port()
    chrome_proc = None
    chrome_user_data = None
    browser = None
    llm = None
    tool_routing_meta: dict[str, Any] = {"mode": "not_initialized"}
    prompt_rules, prompt_routing_meta = build_prompt_rules(
        task_spec.apps,
        run_config.prompt_mode,
    )
    timeout_config = {
        "api_s": run_config.api_timeout_s,
        "step_s": run_config.step_timeout_s,
    }
    generation_config = {
        "max_completion_tokens": run_config.max_completion_tokens,
    }
    loop_guard = ActionLoopGuard.from_environment()

    work_root = run_config.work_root or Path(_TMP_BASE)
    work_root.mkdir(parents=True, exist_ok=True)
    workdir = work_root / f"run_{task_id}_{port}_{int(time.time())}"
    workdir.mkdir(parents=True, exist_ok=True)
    if task_spec.todo:
        (workdir / "todo.md").write_text(task_spec.todo, encoding="utf-8")

    try:
        async with async_playwright() as playwright:
            executable_path = playwright.chromium.executable_path

        chrome_proc, chrome_user_data = _start_chrome(
            executable_path,
            port,
            headless=run_config.headless,
        )
        llm = _build_llm(
            run_config.model,
            run_config.base_url,
            run_config.api_key,
            timeout_config,
            generation_config,
        )
        browser = Browser(
            cdp_url=f"http://127.0.0.1:{port}",
            keep_alive=False,
            disable_security=True,
        )

        tools, tool_routing_meta = build_tools(
            task_spec.apps,
            task_spec.tool_context(tool_context),
            description=task_spec.description,
            mode=run_config.tool_mode,
            credentials=task_spec.credentials,
        )
        effective_system_rules = system_rules if system_rules is not None else prompt_rules
        tool_system_rules = build_tool_system_rules(tool_routing_meta)
        if tool_system_rules:
            effective_system_rules = "\n\n".join(
                part for part in (effective_system_rules, tool_system_rules) if part
            )
        agent = Agent(
            task=task_spec.rendered_prompt(),
            llm=llm,
            browser=browser,
            tools=tools,
            use_vision=run_config.use_vision,
            generate_gif=False,
            save_conversation_path=None,
            max_failures=run_config.max_failures,
            judge=None,
            file_system_path=str(workdir),
            extend_system_message=effective_system_rules,
            available_file_paths=list(task_spec.input_files),
            llm_timeout=run_config.step_timeout_s,
        )

        async def on_step_end(current_agent) -> None:
            items = list(
                getattr(getattr(current_agent, "history", None), "history", []) or []
            )
            if not items:
                return
            item = items[-1]
            model_output = getattr(item, "model_output", None)
            actions = list(getattr(model_output, "action", []) or [])
            state = getattr(item, "state", None)
            raw_results = getattr(item, "result", None)
            results = (
                raw_results
                if isinstance(raw_results, list)
                else ([raw_results] if raw_results else [])
            )
            decision = loop_guard.observe(
                actions=actions,
                targets=getattr(state, "interacted_element", None),
                url=getattr(state, "url", None),
                title=getattr(state, "title", None),
                results=results,
                step=len(items),
            )
            if decision is None or loop_guard.mode != "enforce":
                return
            if decision.kind == "stop":
                current_agent.state.stopped = True
                return
            if decision.kind == "warn" and results:
                result_type = type(results[0])
                try:
                    guard_result = result_type(
                        error=decision.message,
                        include_in_memory=True,
                        long_term_memory=decision.message,
                    )
                except TypeError:
                    guard_result = result_type(error=decision.message)
                current_agent.state.last_result = [*results, guard_result]

        run_parameters = inspect.signature(agent.run).parameters
        run_kwargs: dict[str, Any] = {"max_steps": run_config.max_steps}
        if "on_step_end" in run_parameters:
            run_kwargs["on_step_end"] = on_step_end
        elif loop_guard.mode != "off":
            loop_guard.config_error = (
                "installed browser-use Agent.run has no on_step_end callback; "
                "loop guard was not attached"
            )
        history = await agent.run(**run_kwargs)
        raw = history.final_result() or ""
        output = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()

        trajectory = _extract_trajectory(history, request_ids=llm.request_ids)
        history_errors: list[str] = []
        errors_attr = getattr(history, "errors", None)
        try:
            raw_errors = errors_attr() if callable(errors_attr) else errors_attr
            if raw_errors:
                history_errors = [str(error) for error in raw_errors if error]
        except Exception:
            history_errors = []
        status, termination = classify_termination(
            trajectory,
            max_steps=run_config.max_steps,
            history_errors=history_errors,
        )
        if loop_guard.stop_requested:
            status = "early_stopped"
            termination.update(
                {
                    "reason": "repeated_action_loop",
                    "loop_guard_stop": True,
                }
            )

        result: dict[str, Any] = {
            "task_id": task_id,
            "status": status,
            "agent_output": output,
            "trajectory": trajectory,
            "termination": termination,
            "llm_usage": {
                "summary": _summarize_llm_usage(llm.usage_calls),
                "calls": llm.usage_calls,
            },
            "schema_recovery": _summarize_schema_recovery(
                llm.schema_recovery_events
            ),
            "runtime": {
                "model": run_config.model,
                "max_steps": run_config.max_steps,
                "use_vision": run_config.use_vision,
                "headless": run_config.headless,
                "llm_timeouts": timeout_config,
                "llm_generation": generation_config,
            },
            "prompt_routing": prompt_routing_meta,
            "tool_routing": tool_routing_meta,
            "loop_guard": loop_guard.summary(),
        }
        llm_messages = _llm_messages_payload(llm)
        if llm_messages is not None:
            result["llm_messages"] = llm_messages

    except Exception as exc:
        usage_calls = getattr(llm, "usage_calls", []) if llm is not None else []
        schema_recovery_events = (
            getattr(llm, "schema_recovery_events", []) if llm is not None else []
        )
        result = {
            "task_id": task_id,
            "status": "error",
            "agent_output": "",
            "trajectory": [],
            "error_steps": [str(exc)],
            "termination": {
                "reason": "exception",
                "done_present": False,
                "done_success": None,
                "max_steps_reached": False,
                "browser_error": False,
                "executed_steps": 0,
            },
            "llm_usage": {
                "summary": _summarize_llm_usage(usage_calls),
                "calls": usage_calls,
            },
            "schema_recovery": _summarize_schema_recovery(
                schema_recovery_events
            ),
            "runtime": {
                "model": run_config.model,
                "max_steps": run_config.max_steps,
                "use_vision": run_config.use_vision,
                "headless": run_config.headless,
                "llm_timeouts": timeout_config,
                "llm_generation": generation_config,
            },
            "prompt_routing": prompt_routing_meta,
            "tool_routing": tool_routing_meta,
            "loop_guard": loop_guard.summary(),
        }
        llm_messages = _llm_messages_payload(llm)
        if llm_messages is not None:
            result["llm_messages"] = llm_messages

    finally:
        await _kill(chrome_proc, browser)
        if chrome_user_data:
            shutil.rmtree(chrome_user_data, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)

    if output_path is not None:
        destination = Path(output_path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    print(f"[{task_id}] done - status={result['status']}", flush=True)
    return result
