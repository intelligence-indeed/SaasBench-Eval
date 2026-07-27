from saas_agent.test_metrics import parse_test_output


def test_parse_pytest_summary():
    result = parse_test_output("2 failed, 18 passed, 1 skipped in 4.2s", "pytest")

    assert result == {
        "parser": "pytest",
        "parsed": True,
        "passed": 18,
        "failed": 2,
        "skipped": 1,
        "total": 20,
        "pass_rate": 90.0,
    }


def test_parse_ctest_summary():
    result = parse_test_output(
        "90% tests passed, 2 tests failed out of 20\nTotal Test time = 1.2 sec",
        "ctest",
    )

    assert result["passed"] == 18
    assert result["failed"] == 2
    assert result["total"] == 20
    assert result["pass_rate"] == 90.0


def test_parse_jest_or_vitest_summary():
    result = parse_test_output(
        "Tests: 1 failed, 9 passed, 10 total\nSnapshots: 0 total",
        "jest",
    )

    assert result["passed"] == 9
    assert result["failed"] == 1
    assert result["total"] == 10


def test_parse_vitest_native_summary():
    result = parse_test_output(
        "Test Files  2 passed (2)\nTests  7 passed | 1 failed (8)",
        "vitest",
    )

    assert result["passed"] == 7
    assert result["failed"] == 1
    assert result["total"] == 8


def test_pytest_no_tests_is_an_exact_zero_result():
    result = parse_test_output("no tests ran in 0.01s", "pytest")

    assert result["parsed"] is True
    assert result["passed"] == 0
    assert result["failed"] == 0
    assert result["total"] == 0
    assert result["pass_rate"] is None


def test_auto_parser_reports_unparsed_output_without_inventing_counts():
    result = parse_test_output("build directory does not exist", "auto")

    assert result["parsed"] is False
    assert result["passed"] is None
    assert result["failed"] is None
    assert result["total"] is None
    assert result["pass_rate"] is None
