import pytest

from saas_agent.loop_guard import ActionLoopGuard


def observe_click(
    guard,
    step,
    index=7,
    url="http://app/records",
    content="Clicked button",
    target=None,
):
    return guard.observe(
        actions=[{"click": {"index": index}}],
        targets=[target] if target else None,
        url=url,
        title="Records",
        results=[{"extracted_content": content, "is_done": False}],
        step=step,
    )


def test_enforce_warns_then_stops_unchanged_action_loop():
    guard = ActionLoopGuard("enforce", warn_threshold=3, stop_threshold=5)

    assert observe_click(guard, 1) is None
    assert observe_click(guard, 2) is None
    warning = observe_click(guard, 3)
    assert warning.kind == "warn"
    assert warning.repetition == 3
    assert observe_click(guard, 4) is None
    stopped = observe_click(guard, 5)
    assert stopped.kind == "stop"
    assert guard.stop_requested is True
    assert [event["kind"] for event in guard.summary()["events"]] == ["warn", "stop"]


def test_state_or_action_change_resets_repetition():
    guard = ActionLoopGuard("enforce", warn_threshold=3, stop_threshold=5)

    observe_click(guard, 1)
    observe_click(guard, 2)
    assert observe_click(guard, 3, url="http://app/records/42") is None
    assert observe_click(guard, 4, content="Clicked a different button") is None
    assert guard.summary()["max_repetition"] == 2


def test_dom_index_change_does_not_hide_same_target_loop():
    guard = ActionLoopGuard("enforce", warn_threshold=3, stop_threshold=5)
    target = {
        "backend_node_id": 100,
        "node_name": "button",
        "attributes": {"aria-label": "Save"},
        "element_hash": {"attributes_hash": "stable-save"},
    }

    observe_click(guard, 1, index=10, target=target)
    target["backend_node_id"] = 200
    observe_click(guard, 2, index=999, target=target)
    warning = observe_click(guard, 3, index=42, target=target)

    assert warning.kind == "warn"
    assert warning.repetition == 3


def test_observe_mode_records_without_requesting_stop():
    guard = ActionLoopGuard("observe", warn_threshold=2, stop_threshold=4)

    observe_click(guard, 1)
    assert observe_click(guard, 2).kind == "observed"
    observe_click(guard, 3)
    assert observe_click(guard, 4).kind == "would_stop"
    assert guard.stop_requested is False


def test_off_mode_is_noop():
    guard = ActionLoopGuard("off", warn_threshold=2, stop_threshold=4)
    for step in range(1, 8):
        assert observe_click(guard, step) is None
    assert guard.summary()["events"] == []
    assert guard.stop_requested is False


def test_done_action_is_not_treated_as_loop():
    guard = ActionLoopGuard("enforce", warn_threshold=2, stop_threshold=4)
    for step in range(1, 5):
        decision = guard.observe(
            actions=[{"done": {"success": False, "text": "partial"}}],
            url="http://app",
            title="App",
            results=[{"is_done": True}],
            step=step,
        )
        assert decision is None


def test_invalid_thresholds_are_rejected():
    with pytest.raises(ValueError, match="warn_threshold"):
        ActionLoopGuard("enforce", warn_threshold=1, stop_threshold=4)
    with pytest.raises(ValueError, match="stop_threshold"):
        ActionLoopGuard("enforce", warn_threshold=3, stop_threshold=3)
