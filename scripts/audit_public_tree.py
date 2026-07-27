#!/usr/bin/env python3
"""Audit a release candidate without printing matched sensitive values."""

from __future__ import annotations

import argparse
import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path


SKIP_DIRS = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}

TEXT_SUFFIXES = {
    "",
    ".cff",
    ".css",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}

ARCHIVE_SUFFIXES = (
    ".7z",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".zip",
)

RAW_RESULT_PATTERNS = (
    "*_r[0-9].json",
    "*_r[0-9][0-9].json",
    "*_r[0-9]_verify.json",
    "*_r[0-9][0-9]_verify.json",
    "*.nohup.log",
    "errors.log",
)

SENSITIVE_PATTERNS = {
    "provider-token": re.compile(
        r"(?:sk-[A-Za-z0-9_-]{16,}|github_pat_[A-Za-z0-9_]{20,}|"
        r"ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|"
        r"AKIA[0-9A-Z]{16})"
    ),
    "hardcoded-api-key": re.compile(
        r"(?im)^\s*(?:export\s+)?"
        r"(?:SAAS_AGENT_LLM|LLM|MINDRA|OPENAI|ANTHROPIC|ARK|AZURE_OPENAI)_API_KEY"
        r"\s*=\s*[\"']?(?!\$\{|<|your-|replace|sk-replace-me|dummy|test-|"
        r"os\.environ|os\.getenv)"
        r"[^\s#\"']{12,}"
    ),
    "known-private-host": re.compile(
        r"(?i)(?:10\.1\.0\." r"249|deqing-" r"gpu-249|yingxin" r"@)"
    ),
    "windows-user-path": re.compile(r"(?i)[A-Z]:\\Users\\[^\\/\s]+"),
    "private-linux-work-path": re.compile(
        r"/home/[A-Za-z_][A-Za-z0-9_-]*/"
        r"(?:workplace|Desktop|Downloads|Documents)(?:/|\\)"
    ),
}

RELEASE_PLACEHOLDERS = re.compile(
    r"(?:<future-public-repository-url>|SECURITY_CONTACT_TBD|OWNER_TBD)"
)

HARDCODED_APP_SECRET = re.compile(
    r"(?im)^\s*[A-Z][A-Z0-9_]*(?:PASSWORD|API_KEY|SECRET|TOKEN)\s*=\s*"
    r"[\"'](?!replace-me|dummy|test-|example)[^\"']+[\"']"
)


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    path: str


def _is_skipped(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return any(part in SKIP_DIRS for part in relative.parts)


def _looks_like_raw_result(path: Path) -> bool:
    if any(part == "results" or part.startswith("results_") for part in path.parts):
        return True
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in RAW_RESULT_PATTERNS)


def _is_archive(path: Path) -> bool:
    lowered = path.name.lower()
    return any(lowered.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def _read_text(path: Path) -> str | None:
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {
        "NOTICE",
        "LICENSE",
    }:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def audit_tree(root: Path, max_file_bytes: int = 10 * 1024 * 1024) -> list[Finding]:
    root = root.resolve()
    findings: list[Finding] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or _is_skipped(path, root):
            continue

        relative = path.relative_to(root)
        display = relative.as_posix()

        if path.name == ".env":
            findings.append(Finding("error", "environment-file", display))
        if _is_archive(relative):
            findings.append(Finding("error", "archive-artifact", display))
        if _looks_like_raw_result(relative):
            findings.append(Finding("error", "raw-result-artifact", display))
        try:
            if path.stat().st_size > max_file_bytes:
                findings.append(Finding("error", "oversized-file", display))
        except OSError:
            findings.append(Finding("error", "unreadable-file", display))
            continue

        text = _read_text(path)
        if text is None:
            continue
        for rule, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(text):
                findings.append(Finding("error", rule, display))
        if display.startswith("src/") and HARDCODED_APP_SECRET.search(text):
            findings.append(Finding("error", "hardcoded-app-secret", display))

        if display in {"README.md", "CITATION.cff", "SECURITY.md"}:
            if RELEASE_PLACEHOLDERS.search(text):
                findings.append(Finding("warning", "release-placeholder", display))

    return findings


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--max-file-mib",
        type=int,
        default=10,
        help="Reject individual files larger than this size (default: 10 MiB).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat release placeholders and other warnings as errors.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    findings = audit_tree(
        args.root,
        max_file_bytes=args.max_file_mib * 1024 * 1024,
    )
    errors = [item for item in findings if item.severity == "error"]
    warnings = [item for item in findings if item.severity == "warning"]

    for item in findings:
        print(f"{item.severity.upper():7} {item.rule:24} {item.path}")

    print(
        f"Public-tree audit: {len(errors)} error(s), "
        f"{len(warnings)} warning(s)."
    )
    if errors or (args.strict and warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
