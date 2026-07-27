"""Public task and runtime configuration models for the standalone agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


def _positive_int(value: str | int, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _env(name: str, legacy_name: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    if legacy_name:
        return os.environ.get(legacy_name)
    return None


@dataclass(slots=True)
class AgentTask:
    """A harness-independent task description.

    `apps` controls prompt and tool routing. `app_urls` supplies exact browser
    entry points and is also used as the default base URL map for app tools.
    """

    task_id: str
    prompt: str
    apps: tuple[str, ...] = ()
    app_urls: dict[str, str] = field(default_factory=dict)
    description: str = ""
    input_files: tuple[str, ...] = ()
    todo: str | None = None
    credentials: dict[str, Mapping[str, str]] = field(
        default_factory=dict,
        repr=False,
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.task_id = self.task_id.strip()
        self.prompt = self.prompt.strip()
        if not self.task_id:
            raise ValueError("task_id must not be empty")
        if not self.prompt:
            raise ValueError("prompt must not be empty")

        normalized_urls: dict[str, str] = {}
        for raw_app, raw_url in self.app_urls.items():
            app = str(raw_app).strip().lower()
            url = str(raw_url).strip().rstrip("/")
            parsed = urlparse(url)
            if not app:
                raise ValueError("app_urls contains an empty app name")
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"app_urls[{app!r}] must be an absolute HTTP(S) URL")
            normalized_urls[app] = url
        self.app_urls = normalized_urls

        ordered_apps: list[str] = []
        for raw_app in (*self.apps, *normalized_urls):
            app = str(raw_app).strip().lower()
            if app and app not in ordered_apps:
                ordered_apps.append(app)
        self.apps = tuple(ordered_apps)
        self.input_files = tuple(str(Path(path).expanduser()) for path in self.input_files)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AgentTask":
        """Build a task from the standalone public mapping schema."""

        task_id = value.get("task_id") or value.get("id")
        prompt = value.get("prompt") or value.get("instruction")
        if not isinstance(task_id, str):
            raise ValueError("task mapping requires string task_id or id")
        if not isinstance(prompt, str):
            raise ValueError("task mapping requires string prompt or instruction")
        return cls(
            task_id=task_id,
            prompt=prompt,
            apps=tuple(value.get("apps") or ()),
            app_urls=dict(value.get("app_urls") or {}),
            description=str(value.get("description") or ""),
            input_files=tuple(value.get("input_files") or ()),
            todo=value.get("todo"),
            credentials=dict(value.get("credentials") or {}),
            metadata=dict(value.get("metadata") or {}),
        )

    def rendered_prompt(self) -> str:
        """Render exact app entry points together with the task instruction."""

        if not self.app_urls:
            return self.prompt
        urls = "\n".join(f"- {app}: {url}" for app, url in self.app_urls.items())
        return (
            "## Application Access URLs\n\n"
            "Use these exact URLs. Do not infer vendor-default ports or hosts.\n"
            f"{urls}\n\n"
            "## Task\n\n"
            f"{self.prompt}"
        )

    def tool_context(self, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Return generic connection context consumed by app capability routes."""

        context = dict(extra or {})
        base_urls = dict(context.get("base_urls") or {})
        base_urls.update(self.app_urls)
        context["base_urls"] = base_urls
        credentials = dict(context.get("credentials") or {})
        credentials.update(self.credentials)
        context["credentials"] = credentials
        return context


@dataclass(slots=True)
class AgentConfig:
    """Configuration for one standalone browser-agent run."""

    model: str
    base_url: str
    api_key: str = field(repr=False)
    max_steps: int = 80
    api_timeout_s: int = 600
    step_timeout_s: int = 150
    max_completion_tokens: int = 8192
    use_vision: bool = True
    headless: bool = True
    max_failures: int = 5
    prompt_mode: str = "routing_trimmed"
    tool_mode: str = "disabled"
    work_root: Path | None = None

    def __post_init__(self) -> None:
        self.model = self.model.strip()
        self.base_url = self.base_url.strip().rstrip("/")
        if not self.model:
            raise ValueError("model must not be empty")
        if not self.base_url:
            raise ValueError("base_url must not be empty")
        if not self.api_key:
            raise ValueError("api_key must not be empty")
        self.max_steps = _positive_int(self.max_steps, "max_steps")
        self.api_timeout_s = _positive_int(self.api_timeout_s, "api_timeout_s")
        self.step_timeout_s = _positive_int(self.step_timeout_s, "step_timeout_s")
        self.max_completion_tokens = _positive_int(
            self.max_completion_tokens, "max_completion_tokens"
        )
        self.max_failures = _positive_int(self.max_failures, "max_failures")
        if self.work_root is not None:
            self.work_root = Path(self.work_root).expanduser().resolve()

    @classmethod
    def from_env(cls, **overrides: Any) -> "AgentConfig":
        """Load configuration without requiring secrets at import time."""

        values: dict[str, Any] = {
            "model": _env("SAAS_AGENT_LLM_MODEL", "LLM_MODEL"),
            "base_url": _env("SAAS_AGENT_LLM_BASE_URL", "LLM_BASE_URL"),
            "api_key": _env("SAAS_AGENT_LLM_API_KEY", "LLM_API_KEY"),
            "max_steps": _env("SAAS_AGENT_MAX_STEPS") or 80,
            "api_timeout_s": _env("SAAS_AGENT_LLM_API_TIMEOUT") or 600,
            "step_timeout_s": _env("SAAS_AGENT_LLM_STEP_TIMEOUT") or 150,
            "max_completion_tokens": (
                _env("SAAS_AGENT_LLM_MAX_COMPLETION_TOKENS") or 8192
            ),
            "prompt_mode": _env("SAAS_AGENT_PROMPT_MODE") or "routing_trimmed",
            "tool_mode": _env("SAAS_AGENT_TOOL_MODE") or "disabled",
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        missing = [name for name in ("model", "base_url", "api_key") if not values.get(name)]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"missing agent configuration: {joined}")
        return cls(**values)
