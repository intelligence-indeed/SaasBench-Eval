from saas_agent.termination import classify_termination


def test_done_success_action_is_completed_with_success_metadata():
    status, detail = classify_termination(
        [
            {
                "step": 1,
                "actions": [{"done": {"text": "finished", "success": True}}],
                "results": [{"is_done": True}],
            }
        ],
        max_steps=400,
    )

    assert status == "completed"
    assert detail == {
        "reason": "done_success",
        "done_present": True,
        "done_success": True,
        "max_steps_reached": False,
        "browser_error": False,
        "executed_steps": 1,
    }


def test_done_unsuccessful_action_is_completed_with_failure_metadata():
    status, detail = classify_termination(
        [
            {
                "step": 1,
                "actions": [{"done": {"text": "blocked", "success": False}}],
                "results": [{"is_done": True}],
            }
        ],
        max_steps=400,
    )

    assert status == "completed"
    assert detail["reason"] == "done_unsuccessful"
    assert detail["done_present"] is True
    assert detail["done_success"] is False


def test_is_done_result_is_completed_without_serialized_done_action():
    status, detail = classify_termination(
        [{"step": 1, "actions": [], "results": [{"is_done": True}]}],
        max_steps=400,
    )

    assert status == "completed"
    assert detail["reason"] == "done"
    assert detail["done_present"] is True
    assert detail["done_success"] is None


def test_short_history_without_done_is_early_stopped():
    status, detail = classify_termination(
        [
            {
                "step": 1,
                "actions": [{"click": {"index": 1}}],
                "results": [{"is_done": False}],
            }
        ],
        max_steps=400,
    )

    assert status == "early_stopped"
    assert detail["reason"] == "returned_without_done"
    assert detail["done_present"] is False
    assert detail["done_success"] is None


def test_history_reaching_max_steps_is_classified_as_max_steps():
    trajectory = [
        {"step": step, "actions": [{"wait": {"seconds": 1}}], "results": []}
        for step in range(1, 4)
    ]

    status, detail = classify_termination(trajectory, max_steps=3)

    assert status == "max_steps"
    assert detail["reason"] == "max_steps"
    assert detail["max_steps_reached"] is True


def test_browser_error_has_priority_over_generic_early_stop():
    status, detail = classify_termination(
        [{"step": 1, "actions": [], "results": []}],
        max_steps=400,
        history_errors=["DOMWatchdog: CDP reconnection failed"],
    )

    assert status == "browser_error"
    assert detail["reason"] == "browser_connection_lost"
    assert detail["browser_error"] is True


def test_browser_error_can_be_read_from_trajectory_results():
    status, detail = classify_termination(
        [
            {
                "step": 1,
                "actions": [],
                "results": [
                    {
                        "error": (
                            "BrowserStateRequest failed because "
                            "ScreenshotWatchdog timed out"
                        )
                    }
                ],
            }
        ],
        max_steps=400,
    )

    assert status == "browser_error"
    assert detail["browser_error"] is True


def test_repeated_empty_structured_output_is_classified_as_llm_output_error():
    error = (
        "Invalid model output format. Please follow the correct schema. "
        "Invalid JSON: EOF while parsing a value"
    )
    status, detail = classify_termination(
        [
            {"step": 1, "actions": [], "results": [{"error": error}]},
            {"step": 2, "actions": [], "results": [{"error": error}]},
            {"step": 3, "actions": [], "results": [{"error": error}]},
        ],
        max_steps=400,
    )

    assert status == "llm_output_error"
    assert detail["reason"] == "repeated_invalid_or_empty_model_output"
    assert detail["trailing_llm_output_errors"] == 3
