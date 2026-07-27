"""Detect repeated browser actions that do not produce a visible state change."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any


_VALID_MODES = {"off", "observe", "enforce"}
_IGNORED_ACTIONS = {"done"}


@dataclass(frozen=True)
class LoopGuardDecision:
    kind: str
    repetition: int
    action_signature: str
    state_fingerprint: str
    message: str


class ActionLoopGuard:
    """Track consecutive equivalent actions against equivalent page state."""

    def __init__(
        self,
        mode: str = "off",
        *,
        warn_threshold: int = 3,
        stop_threshold: int = 8,
    ) -> None:
        normalized_mode = str(mode or "off").strip().lower()
        self.config_error: str | None = None
        if normalized_mode not in _VALID_MODES:
            self.config_error = f"unknown SAAS_AGENT_LOOP_GUARD={normalized_mode}"
            normalized_mode = "off"
        if warn_threshold < 2:
            raise ValueError("warn_threshold must be at least 2")
        if stop_threshold <= warn_threshold:
            raise ValueError("stop_threshold must be greater than warn_threshold")

        self.mode = normalized_mode
        self.warn_threshold = int(warn_threshold)
        self.stop_threshold = int(stop_threshold)
        self._last_key: tuple[str, str] | None = None
        self._repetition = 0
        self._warned_for_key = False
        self.events: list[dict[str, Any]] = []
        self.steps_observed = 0
        self.max_repetition = 0
        self.stop_requested = False

    @classmethod
    def from_environment(cls) -> "ActionLoopGuard":
        return cls(
            os.environ.get("SAAS_AGENT_LOOP_GUARD", "off"),
            warn_threshold=_positive_int_env("SAAS_AGENT_LOOP_GUARD_WARN", 3),
            stop_threshold=_positive_int_env("SAAS_AGENT_LOOP_GUARD_STOP", 8),
        )

    def observe(
        self,
        *,
        actions: Any,
        targets: Any = None,
        url: str | None,
        title: str | None,
        results: Any,
        step: int | None = None,
    ) -> LoopGuardDecision | None:
        self.steps_observed += 1
        action_signature = _action_signature(actions, targets)
        if not action_signature:
            self._reset_run()
            return None

        state_fingerprint = _state_fingerprint(url, title, results)
        key = (action_signature, state_fingerprint)
        if key == self._last_key:
            self._repetition += 1
        else:
            self._last_key = key
            self._repetition = 1
            self._warned_for_key = False
        self.max_repetition = max(self.max_repetition, self._repetition)

        decision: LoopGuardDecision | None = None
        if self._repetition >= self.stop_threshold:
            self.stop_requested = self.mode == "enforce"
            decision = LoopGuardDecision(
                kind="stop" if self.mode == "enforce" else "would_stop",
                repetition=self._repetition,
                action_signature=action_signature,
                state_fingerprint=state_fingerprint,
                message=(
                    "Loop guard detected the same browser action and unchanged "
                    f"page state {self._repetition} times. Stop this run instead "
                    "of spending more steps on the same target."
                ),
            )
        elif self._repetition >= self.warn_threshold and not self._warned_for_key:
            self._warned_for_key = True
            decision = LoopGuardDecision(
                kind="warn" if self.mode == "enforce" else "observed",
                repetition=self._repetition,
                action_signature=action_signature,
                state_fingerprint=state_fingerprint,
                message=(
                    "Loop guard: the same action produced no visible state change "
                    f"{self._repetition} times. Do not click or type into that "
                    "target again. Re-read the page, use a routed helper, switch "
                    "to a genuinely different UI path, or skip this subtask."
                ),
            )

        if decision is not None and self.mode != "off":
            event = asdict(decision)
            event["step"] = step
            self.events.append(event)
        return decision if self.mode != "off" else None

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "mode": self.mode,
            "warn_threshold": self.warn_threshold,
            "stop_threshold": self.stop_threshold,
            "steps_observed": self.steps_observed,
            "max_repetition": self.max_repetition,
            "stop_requested": self.stop_requested,
            "events": self.events,
        }
        if self.config_error:
            result["config_error"] = self.config_error
        return result

    def _reset_run(self) -> None:
        self._last_key = None
        self._repetition = 0
        self._warned_for_key = False


def _positive_int_env(name: str, default: int) -> int:
    raw = str(os.environ.get(name, default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _action_signature(actions: Any, targets: Any = None) -> str:
    normalized = _jsonable(actions)
    if not isinstance(normalized, list):
        normalized = [normalized]
    normalized_targets = _jsonable(targets)
    if not isinstance(normalized_targets, list):
        normalized_targets = [normalized_targets]
    compact_actions = []
    for index, action in enumerate(normalized):
        if not isinstance(action, dict) or len(action) != 1:
            compact_actions.append(action)
            continue
        name = next(iter(action))
        if name in _IGNORED_ACTIONS:
            continue
        payload = action[name]
        if isinstance(payload, dict) and "index" in payload:
            payload = dict(payload)
            payload["index"] = "<dom-index>"
        target = normalized_targets[index] if index < len(normalized_targets) else None
        compact_actions.append({
            name: payload,
            "_target": _stable_target(target),
        })
    if not compact_actions:
        return ""
    return json.dumps(compact_actions, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _state_fingerprint(url: str | None, title: str | None, results: Any) -> str:
    compact_results = []
    normalized = _jsonable(results)
    if not isinstance(normalized, list):
        normalized = [normalized]
    for result in normalized:
        if not isinstance(result, dict):
            compact_results.append(_compact_text(result))
            continue
        compact_results.append({
            "error": _compact_text(result.get("error")),
            "extracted_content": _compact_text(result.get("extracted_content")),
            "is_done": result.get("is_done"),
            "success": result.get("success"),
        })
    payload = {
        "url": _compact_text(url, limit=500),
        "title": _compact_text(title, limit=200),
        "results": compact_results,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _jsonable(model_dump(exclude_none=True))
        except TypeError:
            return _jsonable(model_dump())
    return str(value)


def _stable_target(value: Any) -> Any:
    if not isinstance(value, dict):
        return None
    volatile = {
        "node_id", "backend_node_id", "frame_id", "bounds", "center",
        "viewport_coordinates", "page_coordinates",
    }

    def prune(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: prune(child)
                for key, child in sorted(item.items())
                if key not in volatile and child not in (None, "", [], {})
            }
        if isinstance(item, list):
            return [prune(child) for child in item]
        return item

    return prune(value)


def _compact_text(value: Any, *, limit: int = 800) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:limit]
