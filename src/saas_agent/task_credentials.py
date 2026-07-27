"""Strict extraction of per-application credentials from task descriptions."""

from __future__ import annotations

import re
from dataclasses import dataclass


class TaskCredentialError(ValueError):
    """Raised when a task contains ambiguous or malformed credentials."""


@dataclass(frozen=True)
class TaskCredential:
    username: str
    password: str


_CREDENTIAL_LINE_RE = re.compile(
    r"^\s*-\s*(?P<app>[a-z0-9][a-z0-9_-]*)\s*:\s*"
    r"(?P<username>[^/\r\n]+?)\s*/\s*(?P<password>\S(?:.*\S)?)\s*$",
    re.IGNORECASE,
)


def parse_task_credential(description: str, app: str) -> TaskCredential | None:
    """Return one exact ``- app: username / password`` entry, if present.

    The parser intentionally accepts only a complete credential line. It does
    not infer credentials from prose and fails closed when an app appears more
    than once.
    """

    app_name = str(app or "").strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", app_name):
        raise TaskCredentialError("app must be a simple application identifier")

    matches: list[TaskCredential] = []
    for line in str(description or "").splitlines():
        match = _CREDENTIAL_LINE_RE.match(line)
        if not match or match.group("app").casefold() != app_name:
            continue
        username = match.group("username").strip()
        password = match.group("password").strip()
        if not username or not password:
            raise TaskCredentialError(f"malformed {app_name} credential line")
        matches.append(TaskCredential(username=username, password=password))

    if len(matches) > 1:
        raise TaskCredentialError(
            f"multiple credential lines found for {app_name}; refusing ambiguity"
        )
    return matches[0] if matches else None
