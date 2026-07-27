"""Classify how a browser-use rollout terminated."""

from __future__ import annotations

from typing import Any


_BROWSER_ERROR_MARKERS = (
    "browserstaterequest",
    "screenshotwatchdog",
    "cdp",
    "reconnection failed",
    "reconnect failed",
    "client is stopping",
    "websocket message handler exited",
)

_LLM_OUTPUT_ERROR_MARKERS = (
    "invalid model output format",
    "validation error for agentoutput",
    "invalid json",
    "empty model output",
    "model output is empty",
)


def classify_termination(
    trajectory: list[dict[str, Any]],
    max_steps: int,
    history_errors: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return a rollout status and structured termination evidence."""

    done_present, done_success = _done_outcome(trajectory)
    executed_steps = max(
        (int(step.get("step") or 0) for step in trajectory),
        default=0,
    )
    max_steps_reached = bool(max_steps > 0 and executed_steps >= max_steps)
    browser_error = _contains_browser_error(trajectory, history_errors or [])
    trailing_llm_output_errors = _trailing_llm_output_error_count(trajectory)

    if done_present:
        status = "completed"
        if done_success is True:
            reason = "done_success"
        elif done_success is False:
            reason = "done_unsuccessful"
        else:
            reason = "done"
    elif browser_error:
        status = "browser_error"
        reason = "browser_connection_lost"
    elif trailing_llm_output_errors >= 2:
        status = "llm_output_error"
        reason = "repeated_invalid_or_empty_model_output"
    elif max_steps_reached:
        status = "max_steps"
        reason = "max_steps"
    else:
        status = "early_stopped"
        reason = "returned_without_done"

    detail = {
        "reason": reason,
        "done_present": done_present,
        "done_success": done_success,
        "max_steps_reached": max_steps_reached,
        "browser_error": browser_error,
        "executed_steps": executed_steps,
    }
    if trailing_llm_output_errors:
        detail["trailing_llm_output_errors"] = trailing_llm_output_errors
    return status, detail


def _done_outcome(trajectory: list[dict[str, Any]]) -> tuple[bool, bool | None]:
    done_present = False
    done_success: bool | None = None
    for step in trajectory:
        for action in step.get("actions", []):
            if "done" not in action:
                continue
            done_present = True
            payload = action.get("done")
            if isinstance(payload, dict) and isinstance(payload.get("success"), bool):
                done_success = payload["success"]
        if any(result.get("is_done") is True for result in step.get("results", [])):
            done_present = True
    return done_present, done_success


def _contains_browser_error(
    trajectory: list[dict[str, Any]],
    history_errors: list[str],
) -> bool:
    messages = [str(error) for error in history_errors if error]
    for step in trajectory:
        messages.extend(
            str(result["error"])
            for result in step.get("results", [])
            if result.get("error")
        )
    text = "\n".join(messages).lower()
    return any(marker in text for marker in _BROWSER_ERROR_MARKERS)


def _trailing_llm_output_error_count(
    trajectory: list[dict[str, Any]],
) -> int:
    count = 0
    for step in reversed(trajectory):
        errors = [
            str(result.get("error") or "").lower()
            for result in step.get("results", [])
            if result.get("error")
        ]
        if errors and any(
            marker in message
            for message in errors
            for marker in _LLM_OUTPUT_ERROR_MARKERS
        ):
            count += 1
            continue
        break
    return count
