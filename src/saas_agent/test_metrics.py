"""Parse read-only test command output into structured metrics."""

from __future__ import annotations

import re
from typing import Any


def _result(
    parser: str,
    passed: int | None,
    failed: int | None,
    skipped: int = 0,
    total: int | None = None,
) -> dict[str, Any]:
    parsed = passed is not None and failed is not None
    if parsed and total is None:
        total = passed + failed
    pass_rate = round(passed / total * 100, 2) if parsed and total else None
    return {
        "parser": parser,
        "parsed": parsed,
        "passed": passed,
        "failed": failed,
        "skipped": skipped if parsed else None,
        "total": total,
        "pass_rate": pass_rate,
    }


def parse_test_output(output: str, parser: str = "auto") -> dict[str, Any]:
    """Parse pytest, CTest, Jest, or Vitest summaries without guessing counts."""

    parser = (parser or "auto").strip().lower()
    if parser == "auto":
        if re.search(r"tests failed out of\s+\d+", output, re.IGNORECASE):
            parser = "ctest"
        elif re.search(r"^\s*Tests:\s", output, re.MULTILINE):
            parser = "jest"
        elif re.search(r"\b\d+\s+(?:passed|failed)\b", output):
            parser = "pytest"
        else:
            return _result("auto", None, None, total=None)

    if parser == "pytest":
        counts = {
            name: int(value)
            for value, name in re.findall(
                r"\b(\d+)\s+(passed|failed|skipped)\b", output, re.IGNORECASE
            )
        }
        if "passed" not in counts and "failed" not in counts:
            if re.search(r"\bno tests ran\b", output, re.IGNORECASE):
                return _result(parser, 0, 0, total=0)
            return _result(parser, None, None, total=None)
        return _result(
            parser,
            counts.get("passed", 0),
            counts.get("failed", 0),
            counts.get("skipped", 0),
        )

    if parser == "ctest":
        match = re.search(
            r"(\d+)\s+tests?\s+failed\s+out\s+of\s+(\d+)",
            output,
            re.IGNORECASE,
        )
        if not match:
            return _result(parser, None, None, total=None)
        failed, total = (int(match.group(1)), int(match.group(2)))
        return _result(parser, max(total - failed, 0), failed, total=total)

    if parser in {"jest", "vitest"}:
        summary = re.search(
            r"^\s*Tests(?::|\s{2,})\s*(.+)$",
            output,
            re.MULTILINE,
        )
        if not summary:
            return _result(parser, None, None, total=None)
        counts = {
            name.lower(): int(value)
            for value, name in re.findall(
                r"(\d+)\s+(failed|passed|skipped|total)",
                summary.group(1),
                re.IGNORECASE,
            )
        }
        if "passed" not in counts and "failed" not in counts:
            return _result(parser, None, None, total=None)
        return _result(
            parser,
            counts.get("passed", 0),
            counts.get("failed", 0),
            counts.get("skipped", 0),
            counts.get("total"),
        )

    raise ValueError(f"unsupported test output parser: {parser}")
