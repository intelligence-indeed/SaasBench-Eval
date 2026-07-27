"""Deterministic Metabase helper for analytics over Baserow PostgreSQL."""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

from saas_agent.baserow_helper import BaserowClient

try:
    import requests
except Exception:  # pragma: no cover - exercised only in minimal envs.
    requests = None


BASEROW_CONNECTION_NAME = "Baserow Postgres"

_DISPLAYS = {"table", "bar", "pie", "scatter", "scalar"}
_AGGREGATIONS = {"count", "sum", "avg"}
_FILTER_OPERATORS = {"=", "in", "<", "<=", ">", ">="}
_SECRET_PATTERN = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+"
)


class MetabaseError(RuntimeError):
    """Raised when a Metabase operation cannot be completed exactly."""


class MetabaseClient:
    """Create durable Metabase questions and dashboards through the API."""

    def __init__(
        self,
        base_url: str,
        *,
        baserow_host: str,
        baserow_port: int,
        username: str | None = None,
        password: str | None = None,
        baserow_database: str | None = None,
        baserow_username: str | None = None,
        baserow_password: str | None = None,
        connection_name: str = BASEROW_CONNECTION_NAME,
        baserow_api_url: str | None = None,
        baserow_api_email: str | None = None,
        baserow_api_password: str | None = None,
        baserow_api_client: Any | None = None,
        session: Any | None = None,
        timeout: int = 30,
        sequence_retry_attempts: int = 24,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not str(base_url or "").strip():
            raise MetabaseError("base_url is required")
        if not str(baserow_host or "").strip():
            raise MetabaseError("baserow_host is required")
        try:
            baserow_port = int(baserow_port)
        except (TypeError, ValueError) as exc:
            raise MetabaseError("baserow_port must be a positive integer") from exc
        if baserow_port <= 0:
            raise MetabaseError("baserow_port must be a positive integer")
        if session is None:
            if requests is None:
                raise MetabaseError("requests is required when no session is supplied")
            session = requests.Session()

        self.base_url = str(base_url).rstrip("/")
        self.baserow_host = str(baserow_host).strip()
        self.baserow_port = baserow_port
        self.username = str(
            username or os.environ.get("SAAS_AGENT_METABASE_USERNAME") or ""
        ).strip()
        self.password = str(
            password or os.environ.get("SAAS_AGENT_METABASE_PASSWORD") or ""
        )
        self.baserow_database = str(
            baserow_database
            or os.environ.get("SAAS_AGENT_BASEROW_PG_DATABASE")
            or ""
        ).strip()
        self.baserow_username = str(
            baserow_username
            or os.environ.get("SAAS_AGENT_BASEROW_PG_USERNAME")
            or ""
        ).strip()
        self.baserow_password = str(
            baserow_password
            or os.environ.get("SAAS_AGENT_BASEROW_PG_PASSWORD")
            or ""
        )
        missing = [
            name
            for name, value in (
                ("Metabase username", self.username),
                ("Metabase password", self.password),
                ("Baserow PostgreSQL database", self.baserow_database),
                ("Baserow PostgreSQL username", self.baserow_username),
                ("Baserow PostgreSQL password", self.baserow_password),
            )
            if not value
        ]
        if missing:
            raise MetabaseError(
                "missing required connection credentials: " + ", ".join(missing)
            )
        self.connection_name = str(connection_name).strip()
        self.baserow_api_client = baserow_api_client
        if self.baserow_api_client is None and str(baserow_api_url or "").strip():
            self.baserow_api_client = BaserowClient(
                str(baserow_api_url).strip(),
                email=baserow_api_email,
                password=baserow_api_password,
            )
        self.session = session
        self.timeout = int(timeout)
        self.sequence_retry_attempts = max(int(sequence_retry_attempts), 0)
        self.sleep = sleep
        self.session_token: str | None = None
        if hasattr(self.session, "headers"):
            self.session.headers.update({"Accept": "application/json"})

    def authenticate(self, *, force: bool = False) -> str:
        if self.session_token and not force:
            return self.session_token
        response = self.session.post(
            self._url("/api/session"),
            json={"username": self.username, "password": self.password},
            timeout=self.timeout,
        )
        status = getattr(response, "status_code", 200)
        if status >= 400:
            raise MetabaseError(f"Metabase authentication failed with HTTP {status}")
        try:
            token = str(response.json().get("id") or "").strip()
        except Exception as exc:
            raise MetabaseError("Metabase authentication returned invalid JSON") from exc
        if not token:
            raise MetabaseError("Metabase authentication response has no session id")
        self.session_token = token
        if hasattr(self.session, "headers"):
            self.session.headers.update({"X-Metabase-Session": token})
        return token

    def preflight(self) -> dict[str, Any]:
        self.authenticate()
        collections = _items(self._get("/api/collection"))
        databases = _items(self._get("/api/database"))
        try:
            runtime = self.runtime_info()
        except MetabaseError as exc:
            runtime = {"available": False, "error": str(exc)}
        return {
            "authenticated": True,
            "runtime": runtime,
            "collection_count": len(collections),
            "database_count": len(databases),
            "database_names": sorted(
                str(item.get("name")) for item in databases if item.get("name")
            ),
        }

    def runtime_info(self) -> dict[str, Any]:
        """Return a compact runtime fingerprint for endpoint compatibility probes."""
        properties = self._get("/api/session/properties")
        if not isinstance(properties, dict):
            raise MetabaseError("Metabase session properties returned an invalid object")
        version = properties.get("version")
        if isinstance(version, dict):
            public_version = {
                key: version[key]
                for key in ("tag", "date", "branch", "hash")
                if version.get(key) is not None
            }
        else:
            public_version = str(version or "unknown")
        return {"available": True, "version": public_version}

    def inspect_schema(
        self,
        table_names: list[str] | None = None,
        *,
        sync: bool = True,
        deadline_s: int = 90,
        poll_interval_s: float = 2.0,
    ) -> dict[str, Any]:
        database = self.ensure_baserow_database()
        if sync:
            self.sync_schema(database["id"])
        metadata = self.wait_for_metadata(
            database["id"],
            table_names=table_names,
            deadline_s=deadline_s,
            poll_interval_s=poll_interval_s,
        )
        tables = self._schema_tables(metadata)
        if table_names:
            selected = [self._resolve_table(tables, name) for name in table_names]
            public_tables = [_public_table(table) for table in selected]
        else:
            selected = tables[:200]
            public_tables = [_public_table_summary(table) for table in selected]
        return {
            "database": database,
            "table_count": len(tables),
            "tables_truncated": len(tables) > len(selected),
            "tables": public_tables,
        }

    def ensure_analytics(
        self,
        collection_name: str,
        questions: list[dict[str, Any]],
        dashboard: dict[str, Any],
        *,
        sync: bool = True,
        deadline_s: int = 90,
    ) -> dict[str, Any]:
        collection_name = _require_text(collection_name, "collection_name")
        questions = _records(questions, "questions")
        if not questions:
            raise MetabaseError("questions is required")
        dashboard = _record(dashboard, "dashboard")

        question_names = [_require_text(item.get("name"), "question name") for item in questions]
        if len(set(question_names)) != len(question_names):
            raise MetabaseError("question names must be unique")
        _require_text(dashboard.get("name"), "dashboard name")
        if "question_names" in dashboard:
            requested_dashboard_questions = dashboard["question_names"]
            if not isinstance(requested_dashboard_questions, list) or not all(
                isinstance(value, str) and value.strip()
                for value in requested_dashboard_questions
            ):
                raise MetabaseError("dashboard.question_names must be a list of names")
        else:
            requested_dashboard_questions = question_names
        if set(requested_dashboard_questions) != set(question_names):
            raise MetabaseError(
                "dashboard.question_names must match the ensured question names exactly"
            )

        source_tables = self._question_table_names(questions)
        database = self.ensure_baserow_database()
        if sync:
            self.sync_schema(database["id"])
        metadata = self.wait_for_metadata(
            database["id"],
            table_names=source_tables,
            deadline_s=deadline_s,
        )
        collection = self.ensure_collection(collection_name)

        ensured_questions = []
        for spec in questions:
            dataset_query, resolved = self.compile_question(
                database["id"], metadata, spec
            )
            question = self.ensure_question(
                collection["id"], spec, dataset_query
            )
            query_readback = self.query_question(question["id"])
            ensured_questions.append({
                **question,
                "display": spec.get("display"),
                "resolved": resolved,
                "query_readback": query_readback,
            })

        ensured_dashboard = self.ensure_dashboard(collection["id"], dashboard)
        card_ids = [item["id"] for item in ensured_questions]
        card_names = {item["id"]: item["name"] for item in ensured_questions}
        dashboard_readback = self.ensure_dashboard_cards(
            ensured_dashboard["id"], card_ids, card_names
        )

        return {
            "database": database,
            "collection": collection,
            "questions": ensured_questions,
            "dashboard": {
                **ensured_dashboard,
                "readback": dashboard_readback,
            },
        }

    def ensure_baserow_database(self) -> dict[str, Any]:
        self.authenticate()
        databases = _items(self._get("/api/database"))
        matches = [
            item for item in databases
            if str(item.get("name") or "").casefold() == self.connection_name.casefold()
        ]
        if len(matches) > 1:
            raise MetabaseError(
                f"multiple Metabase databases named {self.connection_name!r}"
            )

        payload = {
            "name": self.connection_name,
            "engine": "postgres",
            "details": {
                "host": self.baserow_host,
                "port": self.baserow_port,
                "dbname": self.baserow_database,
                "user": self.baserow_username,
                "password": self.baserow_password,
                "ssl": False,
                "tunnel-enabled": False,
            },
            "is_full_sync": True,
            "is_on_demand": False,
            "auto_run_queries": True,
        }
        if matches:
            database_id = _require_id(matches[0], "database")
            result = self._put(f"/api/database/{database_id}", payload)
            created = False
        else:
            result = self._post("/api/database", payload)
            database_id = _require_id(result, "created database")
            created = True

        return {
            "id": int(database_id),
            "name": self.connection_name,
            "engine": "postgres",
            "host": "host.docker.internal"
            if self.baserow_host == "host.docker.internal"
            else "configured-host",
            "port": self.baserow_port,
            "created": created,
            "updated": not created,
        }

    def sync_schema(self, database_id: int) -> dict[str, Any]:
        result = self._post(f"/api/database/{int(database_id)}/sync_schema", {})
        return {"database_id": int(database_id), "requested": True, "response": result}

    def wait_for_metadata(
        self,
        database_id: int,
        *,
        table_names: list[str] | None = None,
        deadline_s: int = 90,
        poll_interval_s: float = 2.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(int(deadline_s), 1)
        wanted = [str(name) for name in (table_names or [])]
        baserow_schema = self._baserow_schema_metadata()
        last_metadata: dict[str, Any] = {}
        last_names: list[str] = []
        while True:
            payload = self._get(f"/api/database/{int(database_id)}/metadata")
            if isinstance(payload, dict):
                last_metadata = payload
            self._apply_baserow_aliases(last_metadata, baserow_schema)
            tables = self._schema_tables(last_metadata)
            last_names = sorted({_table_label(table) for table in tables})
            if tables:
                try:
                    for name in wanted:
                        self._resolve_table(tables, name)
                except MetabaseError:
                    pass
                else:
                    return last_metadata
            if time.monotonic() >= deadline:
                raise MetabaseError(
                    "Metabase schema sync did not expose required tables "
                    f"{wanted}; available={last_names[:50]}"
                )
            self.sleep(max(float(poll_interval_s), 0.0))

    def _baserow_schema_metadata(self) -> list[dict[str, Any]]:
        if self.baserow_api_client is None:
            return []
        try:
            metadata = self.baserow_api_client.schema_metadata()
        except Exception as exc:
            raise MetabaseError(
                f"could not load Baserow table aliases: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(metadata, list):
            raise MetabaseError("Baserow table aliases returned an invalid payload")
        return [item for item in metadata if isinstance(item, dict)]

    def _apply_baserow_aliases(
        self,
        metadata: dict[str, Any],
        baserow_schema: list[dict[str, Any]],
    ) -> None:
        tables = self._schema_tables(metadata)
        for alias in baserow_schema:
            physical_table = str(alias.get("physical_table_name") or "").casefold()
            if not physical_table:
                continue
            matches = [
                table for table in tables
                if str(table.get("name") or "").casefold() == physical_table
            ]
            if len(matches) != 1:
                continue
            table = matches[0]
            table["_baserow_name"] = str(alias.get("table_name") or "")
            table["_baserow_database_name"] = str(
                alias.get("database_name") or ""
            )
            fields = table.get("fields") if isinstance(table.get("fields"), list) else []
            for field_alias in alias.get("fields") or []:
                physical_field = str(
                    field_alias.get("physical_field_name") or ""
                ).casefold()
                field_matches = [
                    field for field in fields
                    if isinstance(field, dict)
                    and str(field.get("name") or "").casefold() == physical_field
                ]
                if len(field_matches) == 1:
                    field_matches[0]["_baserow_name"] = str(
                        field_alias.get("field_name") or ""
                    )

    def ensure_collection(self, name: str) -> dict[str, Any]:
        name = _require_text(name, "collection name")
        collections = _items(self._get("/api/collection"))
        matches = [
            item for item in collections
            if str(item.get("name") or "").casefold() == name.casefold()
        ]
        if len(matches) > 1:
            raise MetabaseError(f"multiple Metabase collections named {name!r}")
        if matches:
            return {"id": int(_require_id(matches[0], "collection")), "name": name, "created": False}
        created = self._post("/api/collection", {
            "name": name,
            "color": "#509EE3",
            "parent_id": None,
        })
        return {"id": int(_require_id(created, "created collection")), "name": name, "created": True}

    def compile_question(
        self,
        database_id: int,
        metadata: dict[str, Any],
        spec: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        spec = _record(spec, "question")
        source_name = _require_text(spec.get("source_table"), "source_table")
        display = _require_text(spec.get("display"), "display").lower()
        if display not in _DISPLAYS:
            raise MetabaseError(f"unsupported display: {display}")

        tables = self._schema_tables(metadata)
        source = self._resolve_table(tables, source_name)
        join_aliases: dict[str, str] = {}
        query: dict[str, Any] = {"source-table": _require_id(source, "source table")}

        joins = _records(spec.get("joins") or [], "joins")
        compiled_joins = []
        used_aliases: set[str] = set()
        for join in joins:
            table_name = _require_text(join.get("table"), "join table")
            join_table = self._resolve_table(tables, table_name)
            alias = _require_text(join.get("alias") or table_name, "join alias")
            if alias.casefold() in used_aliases:
                raise MetabaseError(f"duplicate join alias: {alias!r}")

            source_table_name = str(join.get("source_table") or source_name)
            join_source = self._resolve_table(tables, source_table_name)
            if _require_id(join_source, "join source table") == _require_id(
                source, "source table"
            ):
                source_alias = None
            else:
                source_alias = join_aliases.get(source_table_name.casefold())
                if not source_alias:
                    raise MetabaseError(
                        f"join source table {source_table_name!r} must appear in an "
                        "earlier join"
                    )
            source_ref = self._field_ref(
                join_source,
                _require_text(join.get("source_field"), "join source_field"),
                join_alias=source_alias,
            )
            target_ref = self._field_ref(
                join_table,
                _require_text(join.get("target_field"), "join target_field"),
                join_alias=alias,
            )
            compiled_joins.append({
                "source-table": _require_id(join_table, "join table"),
                "alias": alias,
                "condition": ["=", source_ref, target_ref],
                "fields": "all",
            })
            join_aliases[table_name.casefold()] = alias
            used_aliases.add(alias.casefold())
        if compiled_joins:
            query["joins"] = compiled_joins

        columns = [str(value) for value in (spec.get("columns") or [])]
        if columns:
            query["fields"] = [
                self._named_field_ref(tables, source, value, join_aliases)
                for value in columns
            ]

        aggregation_specs = _records(spec.get("aggregations") or [], "aggregations")
        aggregations = []
        for aggregation in aggregation_specs:
            op = _require_text(aggregation.get("op"), "aggregation op").lower()
            if op not in _AGGREGATIONS:
                raise MetabaseError(f"unsupported aggregation: {op}")
            if op == "count" and not aggregation.get("field"):
                aggregations.append(["count"])
            else:
                field = _require_text(aggregation.get("field"), "aggregation field")
                aggregations.append([
                    op,
                    self._named_field_ref(tables, source, field, join_aliases),
                ])
        if aggregations:
            query["aggregation"] = aggregations

        breakouts = [str(value) for value in (spec.get("breakouts") or [])]
        if breakouts:
            query["breakout"] = [
                self._named_field_ref(tables, source, value, join_aliases)
                for value in breakouts
            ]

        filters = _records(spec.get("filters") or [], "filters")
        compiled_filters = []
        for item in filters:
            op = _require_text(item.get("op"), "filter op").lower()
            if op not in _FILTER_OPERATORS:
                raise MetabaseError(f"unsupported filter operator: {op}")
            ref = self._named_field_ref(
                tables,
                source,
                _require_text(item.get("field"), "filter field"),
                join_aliases,
            )
            value = item.get("value")
            if op == "in":
                if not isinstance(value, list) or not value:
                    raise MetabaseError("in filter value must be a non-empty list")
                compiled_filters.append(["=", ref, *value])
            else:
                compiled_filters.append([op, ref, value])
        if len(compiled_filters) == 1:
            query["filter"] = compiled_filters[0]
        elif compiled_filters:
            query["filter"] = ["and", *compiled_filters]

        order_by = []
        for item in _records(spec.get("order_by") or [], "order_by"):
            direction = str(item.get("direction") or "asc").lower()
            if direction not in {"asc", "desc"}:
                raise MetabaseError("order direction must be asc or desc")
            if item.get("aggregation_index") is not None:
                index = int(item["aggregation_index"])
                if index < 0 or index >= len(aggregations):
                    raise MetabaseError("order aggregation_index is out of range")
                ref = ["aggregation", index]
            else:
                ref = self._named_field_ref(
                    tables,
                    source,
                    _require_text(item.get("field"), "order field"),
                    join_aliases,
                )
            order_by.append([direction, ref])
        if order_by:
            query["order-by"] = order_by

        if spec.get("limit") is not None:
            limit = int(spec["limit"])
            if limit <= 0 or limit > 10_000:
                raise MetabaseError("limit must be between 1 and 10000")
            query["limit"] = limit

        dataset_query = {
            "database": int(database_id),
            "type": "query",
            "query": query,
        }
        resolved = {
            "source_table": _table_label(source),
            "source_table_id": int(_require_id(source, "source table")),
            "columns": columns,
            "breakouts": breakouts,
            "join_tables": [str(item.get("table")) for item in joins],
        }
        return dataset_query, resolved

    def ensure_question(
        self,
        collection_id: int,
        spec: dict[str, Any],
        dataset_query: dict[str, Any],
    ) -> dict[str, Any]:
        name = _require_text(spec.get("name"), "question name")
        display = _require_text(spec.get("display"), "display").lower()
        cards = [
            item for item in self._collection_items(collection_id, "card")
            if str(item.get("name") or "").casefold() == name.casefold()
        ]
        if len(cards) > 1:
            raise MetabaseError(
                f"multiple questions named {name!r} in collection {collection_id}"
            )
        payload = {
            "name": name,
            "collection_id": int(collection_id),
            "dataset_query": dataset_query,
            "display": display,
            "visualization_settings": self._visualization_settings(spec),
        }
        description = str(spec.get("description") or "").strip()
        if description:
            payload["description"] = description
        if cards:
            card_id = int(_require_id(cards[0], "question"))
            result = self._put(f"/api/card/{card_id}", payload)
            created = False
        else:
            result = self._post("/api/card", payload)
            card_id = int(_require_id(result, "created question"))
            created = True
        return {"id": card_id, "name": name, "created": created, "updated": not created}

    def query_question(self, card_id: int) -> dict[str, Any]:
        payload = self._post(f"/api/card/{int(card_id)}/query", {})
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        cols = data.get("cols") if isinstance(data.get("cols"), list) else []
        column_names = [
            str(col.get("display_name") or col.get("name") or "")
            for col in cols if isinstance(col, dict)
        ]
        sensitive_indexes = {
            index for index, name in enumerate(column_names)
            if re.search(r"(?i)password|passwd|secret|token|api[_-]?key", name)
        }
        return {
            "row_count": len(rows),
            "columns": column_names,
            "sample_rows": [
                [
                    "[redacted]" if index in sensitive_indexes else _compact_value(value)
                    for index, value in enumerate(row)
                ]
                if isinstance(row, list) else _compact_value(row)
                for row in rows[:3]
            ],
        }

    def ensure_dashboard(
        self,
        collection_id: int,
        spec: dict[str, Any],
    ) -> dict[str, Any]:
        name = _require_text(spec.get("name"), "dashboard name")
        description = str(spec.get("description") or "")
        dashboards = [
            item for item in self._collection_items(collection_id, "dashboard")
            if str(item.get("name") or "").casefold() == name.casefold()
        ]
        if len(dashboards) > 1:
            raise MetabaseError(
                f"multiple dashboards named {name!r} in collection {collection_id}"
            )
        payload = {
            "name": name,
            "description": description,
            "collection_id": int(collection_id),
        }
        if dashboards:
            dashboard_id = int(_require_id(dashboards[0], "dashboard"))
            self._put(f"/api/dashboard/{dashboard_id}", payload)
            created = False
        else:
            result = self._post("/api/dashboard", payload)
            dashboard_id = int(_require_id(result, "created dashboard"))
            created = True
        readback = self._get(f"/api/dashboard/{dashboard_id}")
        actual_name = str(readback.get("name") or "") if isinstance(readback, dict) else ""
        actual_description = (
            str(readback.get("description") or "") if isinstance(readback, dict) else ""
        )
        if actual_name != name or actual_description != description:
            raise MetabaseError(
                "dashboard readback mismatch: "
                f"name={actual_name!r}, description={actual_description!r}"
            )
        return {
            "id": dashboard_id,
            "name": name,
            "description": description,
            "created": created,
            "updated": not created,
            "readback_name": actual_name,
            "readback_description": actual_description,
        }

    def ensure_dashboard_cards(
        self,
        dashboard_id: int,
        card_ids: list[int],
        card_names: dict[int, str],
    ) -> dict[str, Any]:
        detail = self._get(f"/api/dashboard/{int(dashboard_id)}")
        dashcards = _dashboard_cards(detail)
        current = [_dashcard_card_id(item) for item in dashcards]

        for card_id in card_ids:
            if int(card_id) in current:
                continue
            self._add_dashboard_card(int(dashboard_id), int(card_id))

        detail = self._get(f"/api/dashboard/{int(dashboard_id)}")
        dashcards = _dashboard_cards(detail)
        actual_ids = [value for value in (_dashcard_card_id(item) for item in dashcards) if value]
        missing = sorted(set(map(int, card_ids)) - set(actual_ids))
        duplicates = sorted({card_id for card_id in card_ids if actual_ids.count(int(card_id)) > 1})
        if missing or duplicates:
            raise MetabaseError(
                f"dashboard card readback mismatch: missing={missing}, duplicates={duplicates}"
            )
        return {
            "card_ids": actual_ids,
            "target_card_names": [card_names[int(card_id)] for card_id in card_ids],
            "target_cards_present": True,
        }

    def _add_dashboard_card(self, dashboard_id: int, card_id: int) -> None:
        errors = []
        for payload in ({"cardId": card_id}, {"card_id": card_id}):
            try:
                self._post(f"/api/dashboard/{dashboard_id}/cards", payload)
                return
            except MetabaseError as exc:
                errors.append(str(exc))
        try:
            detail = self._get(f"/api/dashboard/{dashboard_id}")
            dashcards = list(_dashboard_cards(detail))
            dashcards.append({
                "id": -(len(dashcards) + 1),
                "card_id": int(card_id),
                "row": 0,
                "col": 0,
                "size_x": 4,
                "size_y": 4,
                "parameter_mappings": [],
                "series": [],
                "visualization_settings": {},
            })
            self._put(f"/api/dashboard/{dashboard_id}", {"dashcards": dashcards})
            return
        except MetabaseError as exc:
            errors.append(f"dashboard PUT fallback: {exc}")
        raise MetabaseError(
            f"could not add card {card_id} to dashboard {dashboard_id}: "
            + " | ".join(errors)
        )

    def _collection_items(self, collection_id: int, model: str) -> list[dict[str, Any]]:
        payload = self._get(
            f"/api/collection/{int(collection_id)}/items?models={model}"
        )
        return [
            item for item in _items(payload)
            if str(item.get("model") or model).lower() == model
        ]

    def _schema_tables(self, metadata: dict[str, Any]) -> list[dict[str, Any]]:
        tables = metadata.get("tables") if isinstance(metadata, dict) else None
        if not isinstance(tables, list):
            return []
        return [item for item in tables if isinstance(item, dict)]

    def _resolve_table(
        self,
        tables: list[dict[str, Any]],
        name: str,
    ) -> dict[str, Any]:
        wanted = _require_text(name, "table name").casefold()
        matches = [
            table for table in tables
            if wanted in {
                str(table.get("name") or "").casefold(),
                str(table.get("display_name") or "").casefold(),
                str(table.get("_baserow_name") or "").casefold(),
            }
        ]
        if len(matches) != 1:
            labels = sorted({_table_label(table) for table in tables})
            raise MetabaseError(
                f"expected exactly one table named {name!r}, found {len(matches)}; "
                f"available={labels[:50]}"
            )
        return matches[0]

    def _field_ref(
        self,
        table: dict[str, Any],
        field_name: str,
        *,
        join_alias: str | None = None,
    ) -> list[Any]:
        fields = table.get("fields") if isinstance(table.get("fields"), list) else []
        wanted = _require_text(field_name, "field name").casefold()
        matches = [
            field for field in fields
            if isinstance(field, dict) and wanted in {
                str(field.get("name") or "").casefold(),
                str(field.get("display_name") or "").casefold(),
                str(field.get("_baserow_name") or "").casefold(),
            }
        ]
        if len(matches) != 1:
            available = sorted({
                str(field.get("display_name") or field.get("name") or "")
                for field in fields if isinstance(field, dict)
            })
            raise MetabaseError(
                f"expected exactly one field {field_name!r} on {_table_label(table)!r}, "
                f"found {len(matches)}; available={available[:100]}"
            )
        options = {"join-alias": join_alias} if join_alias else None
        return ["field", _require_id(matches[0], "field"), options]

    def _named_field_ref(
        self,
        tables: list[dict[str, Any]],
        source: dict[str, Any],
        value: str,
        join_aliases: dict[str, str],
    ) -> list[Any]:
        text = _require_text(value, "field reference")
        if "." not in text:
            return self._field_ref(source, text)
        table_name, field_name = text.split(".", 1)
        table = self._resolve_table(tables, table_name)
        if _require_id(table, "field table") == _require_id(source, "source table"):
            return self._field_ref(table, field_name)
        alias = join_aliases.get(table_name.casefold())
        if not alias:
            raise MetabaseError(
                f"field {value!r} refers to a table that is not in joins"
            )
        return self._field_ref(table, field_name, join_alias=alias)

    def _question_table_names(self, questions: list[dict[str, Any]]) -> list[str]:
        names = []
        for question in questions:
            names.append(_require_text(question.get("source_table"), "source_table"))
            for join in _records(question.get("joins") or [], "joins"):
                names.append(_require_text(join.get("table"), "join table"))
        return list(dict.fromkeys(names))

    def _visualization_settings(self, spec: dict[str, Any]) -> dict[str, Any]:
        visualization = spec.get("visualization") or {}
        if not isinstance(visualization, dict):
            raise MetabaseError("visualization must be an object")
        settings: dict[str, Any] = {}
        if visualization.get("x_axis"):
            settings["graph.dimensions"] = [str(visualization["x_axis"])]
        if visualization.get("y_axes"):
            if not isinstance(visualization["y_axes"], list):
                raise MetabaseError("visualization.y_axes must be a list")
            settings["graph.metrics"] = [str(value) for value in visualization["y_axes"]]
        return settings

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.authenticate()
        kwargs.setdefault("timeout", self.timeout)
        auth_retried = False
        sequence_retries = 0
        while True:
            response = getattr(self.session, method.lower())(self._url(path), **kwargs)
            status = getattr(response, "status_code", 200)
            if status == 401 and not auth_retried:
                self.authenticate(force=True)
                auth_retried = True
                continue
            raw_text = str(getattr(response, "text", ""))
            if (
                method.lower() == "post"
                and status >= 500
                and sequence_retries < self.sequence_retry_attempts
                and _is_metabase_sequence_collision(raw_text)
            ):
                sequence_retries += 1
                self.sleep(min(0.05 * sequence_retries, 0.5))
                continue
            break
        if status >= 400:
            text = _SECRET_PATTERN.sub("[redacted]", raw_text)
            for secret in (self.password, self.baserow_password):
                if secret:
                    text = text.replace(secret, "[redacted]")
            raise MetabaseError(
                f"{method.upper()} {path} failed with HTTP {status}: {text[:500]}"
            )
        if status == 204 or not str(getattr(response, "text", "")):
            return {}
        try:
            return response.json()
        except Exception as exc:
            raise MetabaseError(
                f"{method.upper()} {path} returned invalid JSON"
            ) from exc

    def _get(self, path: str) -> Any:
        return self._request("get", path)

    def _post(self, path: str, payload: Any) -> Any:
        return self._request("post", path, json=payload)

    def _put(self, path: str, payload: Any) -> Any:
        return self._request("put", path, json=payload)


def _is_metabase_sequence_collision(text: str) -> bool:
    lowered = str(text or "").casefold()
    return (
        "unique index or primary key violation" in lowered
        and "primary key on public." in lowered
    )


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _records(value: Any, name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise MetabaseError(f"{name} must be a list of objects")
    return value


def _record(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MetabaseError(f"{name} must be an object")
    return value


def _require_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise MetabaseError(f"{name} is required")
    return text


def _require_id(value: dict[str, Any], name: str) -> int:
    try:
        return int(value["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MetabaseError(f"{name} has no valid id") from exc


def _table_label(table: dict[str, Any]) -> str:
    return str(
        table.get("_baserow_name")
        or table.get("display_name")
        or table.get("name")
        or ""
    )


def _public_table(table: dict[str, Any]) -> dict[str, Any]:
    fields = table.get("fields") if isinstance(table.get("fields"), list) else []
    return {
        "id": _require_id(table, "table"),
        "name": table.get("name"),
        "display_name": _table_label(table),
        "schema": table.get("schema"),
        "fields": [{
            "id": _require_id(field, "field"),
            "name": field.get("name"),
            "display_name": (
                field.get("_baserow_name")
                or field.get("display_name")
                or field.get("name")
            ),
            "base_type": field.get("base_type"),
            "semantic_type": field.get("semantic_type"),
        } for field in fields if isinstance(field, dict) and field.get("id") is not None],
    }


def _public_table_summary(table: dict[str, Any]) -> dict[str, Any]:
    fields = table.get("fields") if isinstance(table.get("fields"), list) else []
    return {
        "id": _require_id(table, "table"),
        "name": table.get("name"),
        "display_name": _table_label(table),
        "schema": table.get("schema"),
        "field_count": len(fields),
    }


def _dashboard_cards(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    cards = payload.get("dashcards")
    if not isinstance(cards, list) or not cards:
        cards = payload.get("ordered_cards")
    return [item for item in (cards or []) if isinstance(item, dict)]


def _dashcard_card_id(item: dict[str, Any]) -> int | None:
    card = item.get("card")
    value = card.get("id") if isinstance(card, dict) else item.get("card_id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _compact_value(value: Any) -> Any:
    if isinstance(value, str):
        value = _SECRET_PATTERN.sub("[redacted]", value)
        return value if len(value) <= 120 else value[:117] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:120]
