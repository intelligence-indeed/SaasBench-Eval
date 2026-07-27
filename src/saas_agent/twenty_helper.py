"""Read-only Twenty CRM helper backed by persisted PostgreSQL state."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any, Callable


class TwentyError(RuntimeError):
    """Raised when deterministic Twenty readback cannot be completed."""


_ENTITIES = {
    "companies": {
        "table": "company",
        "columns": [
            "id", "name", "domainNamePrimaryLinkUrl", "employees",
            "createdAt", "updatedAt", "deletedAt",
        ],
        "name_fields": ["name"],
    },
    "people": {
        "table": "person",
        "columns": [
            "id", "nameFirstName", "nameLastName", "emailsPrimaryEmail",
            "jobTitle", "phonesPrimaryPhoneNumber",
            "phonesPrimaryPhoneCountryCode", "phonesPrimaryPhoneCallingCode",
            "companyId",
            "createdAt", "updatedAt", "deletedAt",
        ],
        "name_fields": ["nameFirstName", "nameLastName"],
    },
    "opportunities": {
        "table": "opportunity",
        "columns": [
            "id", "name", "amountAmountMicros", "stage", "closeDate",
            "companyId", "pointOfContactId", "createdAt", "updatedAt",
            "deletedAt",
        ],
        "name_fields": ["name"],
    },
    "tasks": {
        "table": "task",
        "columns": [
            "id", "title", "status", "dueAt", "bodyV2Markdown",
            "createdAt", "updatedAt", "deletedAt",
        ],
        "name_fields": ["title"],
    },
    "notes": {
        "table": "note",
        "columns": [
            "id", "title", "bodyV2Markdown", "createdAt", "updatedAt",
            "deletedAt",
        ],
        "name_fields": ["title"],
    },
}

_BUSINESS_KEY_COLUMNS = {
    "companies": {"name": "name"},
    "people": {"email": "emailsPrimaryEmail"},
    "opportunities": {"name": "name"},
    "tasks": {"title": "title"},
    "notes": {"title": "title"},
}


class TwentyClient:
    """Query whitelisted Twenty entities without exposing arbitrary SQL."""

    def __init__(
        self,
        container_name: str,
        *,
        runner: Callable[..., Any] = subprocess.run,
        timeout: int = 30,
    ) -> None:
        if not str(container_name or "").strip():
            raise TwentyError("container_name is required")
        self.container_name = str(container_name).strip()
        self.runner = runner
        self.timeout = int(timeout)
        self._database_login: tuple[str, str] | None = None
        self._workspace: str | None = None

    def query_records(
        self,
        entity: str,
        exact_names: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        entity = str(entity or "").strip().lower()
        if entity not in _ENTITIES:
            raise TwentyError(
                f"unsupported entity {entity!r}; choose one of {sorted(_ENTITIES)}"
            )
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise TwentyError("limit must be an integer") from exc
        if limit < 1 or limit > 1000:
            raise TwentyError("limit must be between 1 and 1000")
        requested = [_required_text(value, "exact name") for value in exact_names or []]
        if len(set(requested)) != len(requested):
            raise TwentyError("exact_names must be unique")

        spec = _ENTITIES[entity]
        return self._query_records(
            entity,
            requested,
            limit,
            key_expression=_name_expression(entity, spec["name_fields"]),
            requested_label="requested_exact_names",
            missing_label="missing_exact_names",
        )

    def query_by_business_key(
        self,
        entity: str,
        key: str,
        exact_values: list[str],
        limit: int = 200,
    ) -> dict[str, Any]:
        """Read exact persisted rows using a whitelisted stable key."""

        entity = str(entity or "").strip().lower()
        key = str(key or "").strip().lower()
        if entity not in _BUSINESS_KEY_COLUMNS or key not in _BUSINESS_KEY_COLUMNS[entity]:
            raise TwentyError(f"unsupported business key {entity}.{key}")
        requested = [_required_text(value, f"{key} value") for value in exact_values]
        if not requested:
            raise TwentyError("exact_values must be a non-empty list")
        if len(set(requested)) != len(requested):
            raise TwentyError("exact_values must be unique")
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise TwentyError("limit must be an integer") from exc
        if limit < 1 or limit > 1000:
            raise TwentyError("limit must be between 1 and 1000")
        column = _BUSINESS_KEY_COLUMNS[entity][key]
        return self._query_records(
            entity,
            requested,
            limit,
            key_expression=_quote_identifier(column),
            requested_label="requested_exact_values",
            missing_label="missing_exact_values",
            exact_key_name=key,
        )

    def query_activity_targets(self, entity: str, activity_id: str) -> list[dict[str, Any]]:
        """Return persisted task/note target links for one activity."""

        entity = str(entity or "").strip().lower()
        if entity not in {"tasks", "notes"}:
            raise TwentyError("activity target entity must be tasks or notes")
        activity_id = _required_text(activity_id, "activity_id")
        workspace = self._workspace_schema()
        table = "taskTarget" if entity == "tasks" else "noteTarget"
        activity_column = "taskId" if entity == "tasks" else "noteId"
        available = self._table_columns(workspace, table)
        selected = [
            column for column in (
                "id", activity_column, "targetCompanyId", "targetPersonId",
                "targetOpportunityId", "deletedAt",
            ) if column in available
        ]
        where = [
            f"{_quote_identifier(activity_column)}={_sql_literal(activity_id)}"
        ]
        if "deletedAt" in available:
            where.append(f"{_quote_identifier('deletedAt')} IS NULL")
        sql = (
            "SELECT row_to_json(q)::text FROM (SELECT "
            + ", ".join(_quote_identifier(column) for column in selected)
            + f" FROM {_quote_identifier(workspace)}.{_quote_identifier(table)}"
            + " WHERE " + " AND ".join(where) + ") q;"
        )
        return _json_rows(self._psql(sql))

    def query_company_favorites(self, company_id: str) -> list[dict[str, Any]]:
        """Return active favorite rows for a company when the object exists."""

        company_id = _required_text(company_id, "company_id")
        workspace = self._workspace_schema()
        available = self._table_columns(workspace, "favorite")
        selected = [
            column for column in ("id", "companyId", "deletedAt")
            if column in available
        ]
        if "companyId" not in selected:
            raise TwentyError("Twenty favorite table has no companyId column")
        where = [f'{_quote_identifier("companyId")}={_sql_literal(company_id)}']
        if "deletedAt" in available:
            where.append(f'{_quote_identifier("deletedAt")} IS NULL')
        sql = (
            "SELECT row_to_json(q)::text FROM (SELECT "
            + ", ".join(_quote_identifier(column) for column in selected)
            + f" FROM {_quote_identifier(workspace)}.{_quote_identifier('favorite')}"
            + " WHERE " + " AND ".join(where) + ") q;"
        )
        return _json_rows(self._psql(sql))

    def _query_records(
        self,
        entity: str,
        requested: list[str],
        limit: int,
        *,
        key_expression: str,
        requested_label: str,
        missing_label: str,
        exact_key_name: str = "name",
    ) -> dict[str, Any]:
        spec = _ENTITIES[entity]
        workspace = self._workspace_schema()
        available = self._table_columns(workspace, spec["table"])
        selected = [column for column in spec["columns"] if column in available]
        if "id" not in selected:
            raise TwentyError(f"Twenty table {spec['table']} has no id column")
        quoted = ", ".join(_quote_identifier(column) for column in selected)
        predicates = []
        if "deletedAt" in available:
            predicates.append(f"{_quote_identifier('deletedAt')} IS NULL")
        if requested:
            predicates.append(
                f"({key_expression}) IN ("
                + ", ".join(_sql_literal(value) for value in requested)
                + ")"
            )
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        order_column = "createdAt" if "createdAt" in available else "id"
        sql = (
            "SELECT row_to_json(q)::text FROM (SELECT "
            f"{quoted} FROM {_quote_identifier(workspace)}."
            f"{_quote_identifier(spec['table'])}{where} "
            f"ORDER BY {_quote_identifier(order_column)} DESC NULLS LAST "
            f"LIMIT {limit}) q;"
        )
        raw = self._psql(sql)
        records = _json_rows(raw)

        matches = []
        for record in records:
            if exact_key_name == "name":
                exact_value = " ".join(
                    str(record.get(field) or "").strip()
                    for field in spec["name_fields"]
                ).strip()
                record["_exact_name"] = exact_value
            else:
                column = _BUSINESS_KEY_COLUMNS[entity][exact_key_name]
                exact_value = str(record.get(column) or "").strip()
                record[f"_exact_{exact_key_name}"] = exact_value
            if not requested or exact_value in requested:
                matches.append(record)
        found = {
            record.get("_exact_name") if exact_key_name == "name"
            else record.get(f"_exact_{exact_key_name}")
            for record in matches
        }
        counts = {value: 0 for value in requested}
        for record in matches:
            value = (
                record.get("_exact_name") if exact_key_name == "name"
                else record.get(f"_exact_{exact_key_name}")
            )
            if value in counts:
                counts[value] += 1
        return {
            "entity": entity,
            "workspace_schema": workspace,
            requested_label: requested,
            "matched_count": len(matches),
            missing_label: [value for value in requested if value not in found],
            "duplicate_exact_values": [
                value for value, count in counts.items() if count > 1
            ],
            "records": matches,
        }

    def _workspace_schema(self) -> str:
        if self._workspace:
            return self._workspace
        raw = self._psql(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE 'workspace_%' ORDER BY schema_name LIMIT 1;"
        ).strip()
        workspace = raw.splitlines()[0].strip() if raw else ""
        if not re.fullmatch(r"workspace_[A-Za-z0-9_]+", workspace):
            raise TwentyError(f"invalid or missing Twenty workspace schema: {workspace!r}")
        self._workspace = workspace
        return workspace

    def _table_columns(self, workspace: str, table: str) -> set[str]:
        sql = (
            "SELECT column_name FROM information_schema.columns WHERE "
            f"table_schema={_sql_literal(workspace)} AND "
            f"table_name={_sql_literal(table)} ORDER BY ordinal_position;"
        )
        columns = {line.strip() for line in self._psql(sql).splitlines() if line.strip()}
        if not columns:
            raise TwentyError(f"Twenty table {workspace}.{table} was not found")
        return columns

    def _psql(self, sql: str) -> str:
        candidates = [self._database_login] if self._database_login else [
            ("twenty", "default"),
            ("twenty", "twenty"),
            ("postgres", "default"),
            ("postgres", "twenty"),
        ]
        errors = []
        for candidate in candidates:
            if candidate is None:
                continue
            user, database = candidate
            proc = self.runner(
                [
                    "docker", "exec", self.container_name, "psql",
                    "-U", user, "-d", database, "-t", "-A", "-c", sql,
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            if proc.returncode == 0:
                self._database_login = (user, database)
                return str(proc.stdout or "").strip()
            errors.append(f"{user}@{database}: {(proc.stderr or proc.stdout).strip()[:300]}")
        raise TwentyError("Twenty psql failed: " + " | ".join(errors))


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise TwentyError(f"{label} is required")
    return text


def _name_expression(entity: str, fields: list[str]) -> str:
    if entity == "people":
        return "trim(concat_ws(' ', " + ", ".join(
            f"coalesce({_quote_identifier(field)}, '')" for field in fields
        ) + "))"
    return _quote_identifier(fields[0])


def _json_rows(raw: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TwentyError(f"invalid JSON row from Twenty: {line[:200]}") from exc
        if not isinstance(value, dict):
            raise TwentyError(f"Twenty row is not an object: {line[:200]}")
        records.append(value)
    return records
