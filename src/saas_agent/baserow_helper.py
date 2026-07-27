"""Small REST helper for durable Baserow table operations."""

from __future__ import annotations

import json
import os
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover - exercised only in minimal envs.
    requests = None


class BaserowError(RuntimeError):
    """Raised when a Baserow API operation cannot be completed."""


class BaserowClient:
    """Create and update Baserow database/table/field/row objects via REST."""

    _SELECT_COLORS = [
        "blue",
        "green",
        "yellow",
        "orange",
        "red",
        "purple",
        "pink",
        "gray",
    ]

    def __init__(
        self,
        base_url: str,
        *,
        email: str | None = None,
        password: str | None = None,
        session: Any | None = None,
        timeout: int = 20,
    ) -> None:
        if not base_url or not str(base_url).strip():
            raise BaserowError("base_url is required")
        self.base_url = str(base_url).rstrip("/")
        self.email = str(
            email or os.environ.get("SAAS_AGENT_BASEROW_EMAIL") or ""
        ).strip()
        self.password = str(
            password or os.environ.get("SAAS_AGENT_BASEROW_PASSWORD") or ""
        )
        if not self.email or not self.password:
            raise BaserowError(
                "Baserow credentials are required; pass email/password in tool "
                "context or set SAAS_AGENT_BASEROW_EMAIL and "
                "SAAS_AGENT_BASEROW_PASSWORD"
            )
        self.timeout = timeout
        if session is None:
            if requests is None:
                raise BaserowError("requests is required when no session is supplied")
            session = requests.Session()
        self.session = session
        self._token: str | None = None
        self._auth_scheme = "Token"

    def ensure_table(
        self,
        database_name: str,
        table_name: str,
        fields: list[dict[str, Any]] | None = None,
        rows: list[dict[str, Any]] | None = None,
        views: list[dict[str, Any]] | None = None,
        replace_rows: bool = False,
    ) -> dict[str, Any]:
        """Ensure a table exists with the requested fields and rows."""

        database_name = _require_text(database_name, "database_name")
        table_name = _require_text(table_name, "table_name")
        rows = _coerce_record_list(rows, "rows")
        fields = _normalise_fields(_coerce_record_list(fields, "fields"), rows)
        views = _coerce_record_list(views, "views")

        self._authenticate()
        database, created_database = self._ensure_database(database_name)
        table, created_table = self._ensure_table(database["id"], table_name)
        field_result = self._ensure_fields(database["id"], table["id"], fields)
        deleted_blank_rows_before = self._delete_blank_rows(
            table["id"],
            field_result["field_names"],
            primary_field=field_result["primary_field"],
        )
        deleted_rows = self._delete_all_rows(table["id"]) if replace_rows else 0
        row_result = self._upsert_rows(
            table["id"],
            rows,
            primary_field=field_result["primary_field"],
            fields_by_name=field_result["fields_by_name"],
        )
        deleted_blank_rows_after = self._delete_blank_rows(
            table["id"],
            field_result["field_names"],
            primary_field=field_result["primary_field"],
        )
        view_result = self._ensure_views(
            table["id"],
            views,
            fields_by_name=field_result["fields_by_name"],
        )
        readback = self._get_rows(table["id"])

        return {
            "database_id": database["id"],
            "database_name": database.get("name", database_name),
            "table_id": table["id"],
            "table_name": table.get("name", table_name),
            "created_database": created_database,
            "created_table": created_table,
            "created_fields": field_result["created_fields"],
            "renamed_fields": field_result["renamed_fields"],
            "field_names": field_result["field_names"],
            "created_rows": row_result["created_rows"],
            "updated_rows": row_result["updated_rows"],
            "deleted_blank_rows": deleted_blank_rows_before + deleted_blank_rows_after,
            "deleted_blank_rows_before": deleted_blank_rows_before,
            "deleted_blank_rows_after": deleted_blank_rows_after,
            "deleted_rows": deleted_rows,
            "row_count": len(readback),
            "created_views": view_result["created_views"],
            "view_names": view_result["view_names"],
        }

    def schema_metadata(self) -> list[dict[str, Any]]:
        """Return human-to-physical table and field mappings for analytics tools."""
        self._authenticate()
        result: list[dict[str, Any]] = []
        for application in _items(self._get("/api/applications/")):
            if application.get("type") != "database":
                continue
            database_id = int(application["id"])
            tables = application.get("tables")
            if not isinstance(tables, list) or not tables:
                tables = _items(
                    self._get(f"/api/database/tables/database/{database_id}/")
                )
            for table in tables:
                table_id = int(table["id"])
                fields = _items(
                    self._get(f"/api/database/fields/table/{table_id}/")
                )
                result.append({
                    "database_id": database_id,
                    "database_name": str(application.get("name") or ""),
                    "table_id": table_id,
                    "table_name": str(table.get("name") or ""),
                    "physical_table_name": f"database_table_{table_id}",
                    "fields": [
                        {
                            "field_id": int(field["id"]),
                            "field_name": str(field.get("name") or ""),
                            "physical_field_name": f"field_{int(field['id'])}",
                            "primary": bool(field.get("primary")),
                        }
                        for field in fields
                        if field.get("id") is not None
                    ],
                })
        return result

    def _url(self, path: str) -> str:
        path = path.lstrip("/")
        return f"{self.base_url}/{path}"

    def _authenticate(self) -> None:
        if self._token:
            return
        response = self.session.post(
            self._url("/api/user/token-auth/"),
            json={"email": self.email, "password": self.password},
            timeout=self.timeout,
        )
        _raise(response, "authenticate")
        payload = _json(response, "authenticate")
        token = payload.get("access_token") or payload.get("token")
        if not token:
            raise BaserowError("Baserow auth response did not include a token")
        self._token = token
        self._auth_scheme = "JWT" if payload.get("access_token") else "Token"

    def _headers(self) -> dict[str, str]:
        if not self._token:
            self._authenticate()
        return {"Authorization": f"{self._auth_scheme} {self._token}"}

    def _request(self, method: str, path: str, *, protected: bool = True, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        if protected:
            headers.update(self._headers())
        kwargs.setdefault("timeout", self.timeout)
        if headers:
            kwargs["headers"] = headers

        func = getattr(self.session, method.lower())
        response = func(self._url(path), **kwargs)
        if protected and getattr(response, "status_code", 200) in {401, 403}:
            # Different Baserow releases accept different auth prefixes for the
            # same token-auth response. Retry once with the
            # alternate scheme before surfacing the error.
            self._auth_scheme = "JWT" if self._auth_scheme == "Token" else "Token"
            headers.update(self._headers())
            kwargs["headers"] = headers
            response = func(self._url(path), **kwargs)
        _raise(response, f"{method} {path}")
        return _json(response, f"{method} {path}")

    def _get(self, path: str, **kwargs: Any) -> Any:
        return self._request("get", path, **kwargs)

    def _post(self, path: str, **kwargs: Any) -> Any:
        return self._request("post", path, **kwargs)

    def _patch(self, path: str, **kwargs: Any) -> Any:
        return self._request("patch", path, **kwargs)

    def _delete(self, path: str, **kwargs: Any) -> Any:
        return self._request("delete", path, **kwargs)

    def _ensure_database(self, database_name: str) -> tuple[dict[str, Any], bool]:
        for app in _items(self._get("/api/applications/")):
            if app.get("name") == database_name and app.get("type") == "database":
                return app, False

        workspace_id = self._first_container_id()
        payload = {"name": database_name, "type": "database"}
        attempts = [
            f"/api/applications/group/{workspace_id}/",
            f"/api/applications/workspace/{workspace_id}/",
        ]
        errors: list[str] = []
        for path in attempts:
            try:
                return self._post(path, json=payload), True
            except Exception as exc:
                errors.append(f"{path}: {exc}")
        raise BaserowError("could not create database: " + "; ".join(errors))

    def _first_container_id(self) -> int:
        for path in ("/api/groups/", "/api/workspaces/"):
            try:
                containers = _items(self._get(path))
            except Exception:
                continue
            if containers:
                return int(containers[0]["id"])
        raise BaserowError("could not find a Baserow group/workspace")

    def _ensure_table(self, database_id: int, table_name: str) -> tuple[dict[str, Any], bool]:
        tables = _items(self._get(f"/api/database/tables/database/{database_id}/"))
        for table in tables:
            if table.get("name") == table_name:
                return table, False
        table = self._post(
            f"/api/database/tables/database/{database_id}/",
            json={"name": table_name},
        )
        return table, True

    def _ensure_fields(
        self,
        database_id: int,
        table_id: int,
        fields: list[dict[str, Any]],
    ) -> dict[str, Any]:
        existing = _items(self._get(f"/api/database/fields/table/{table_id}/"))
        existing_by_name = {field.get("name"): field for field in existing}
        created: list[str] = []
        renamed: list[dict[str, str]] = []

        primary_field = _primary_field_name(fields, existing)
        requested_primary = next(
            (field for field in fields if field["name"] == primary_field),
            fields[0] if fields else None,
        )
        if requested_primary and requested_primary["name"] not in existing_by_name:
            default_primary = next(
                (field for field in existing if field.get("primary")),
                existing[0] if existing else None,
            )
            if default_primary and _is_textish(requested_primary["type"]):
                patched = self._patch(
                    f"/api/database/fields/{default_primary['id']}/",
                    json={"name": requested_primary["name"]},
                )
                old_name = default_primary.get("name", "")
                default_primary.update(patched)
                existing_by_name.pop(old_name, None)
                existing_by_name[requested_primary["name"]] = default_primary
                renamed.append({"from": old_name, "to": requested_primary["name"]})

        for field in fields:
            if field["name"] in existing_by_name:
                continue
            payload = _field_payload(field)
            if payload["type"] == "link_row":
                payload["link_row_table_id"] = self._resolve_link_row_table_id(
                    database_id,
                    field,
                )
            created_field = self._post(
                f"/api/database/fields/table/{table_id}/",
                json=payload,
            )
            existing_by_name[created_field.get("name", field["name"])] = created_field
            created.append(field["name"])

        final_fields = _items(self._get(f"/api/database/fields/table/{table_id}/"))
        requested_names = {field["name"] for field in fields}
        fields_by_name = {
            str(field.get("name")): field
            for field in final_fields
            if field.get("name") in requested_names
        }
        return {
            "primary_field": primary_field,
            "created_fields": created,
            "renamed_fields": renamed,
            "field_names": [
                field.get("name")
                for field in final_fields
                if field.get("name") in requested_names
            ],
            "fields_by_name": fields_by_name,
        }

    def _delete_blank_rows(
        self,
        table_id: int,
        field_names: list[str],
        *,
        primary_field: str | None = None,
    ) -> int:
        deleted = 0
        for row in self._get_rows(table_id):
            if not row.get("id"):
                continue
            if _is_blank_row(row, field_names, primary_field=primary_field):
                self._delete(
                    f"/api/database/rows/table/{table_id}/{row['id']}/",
                    params={"user_field_names": "true"},
                )
                deleted += 1
        return deleted

    def _delete_all_rows(self, table_id: int) -> int:
        deleted = 0
        for row in self._get_rows(table_id):
            if not row.get("id"):
                continue
            self._delete(
                f"/api/database/rows/table/{table_id}/{row['id']}/",
                params={"user_field_names": "true"},
            )
            deleted += 1
        return deleted

    def _upsert_rows(
        self,
        table_id: int,
        rows: list[dict[str, Any]],
        *,
        primary_field: str,
        fields_by_name: dict[str, dict[str, Any]],
    ) -> dict[str, int]:
        existing = self._get_rows(table_id)
        existing_by_key: dict[Any, dict[str, Any]] = {}
        for row in existing:
            key = row.get(primary_field)
            if key not in (None, ""):
                existing_by_key[key] = row

        created = 0
        updated = 0
        for row in rows:
            clean = self._coerce_row_values(
                {str(k): v for k, v in dict(row).items()},
                fields_by_name,
            )
            key = clean.get(primary_field)
            existing_row = existing_by_key.get(key) if key not in (None, "") else None
            if existing_row and existing_row.get("id"):
                self._patch(
                    f"/api/database/rows/table/{table_id}/{existing_row['id']}/",
                    params={"user_field_names": "true"},
                    json=clean,
                )
                updated += 1
            else:
                self._post(
                    f"/api/database/rows/table/{table_id}/",
                    params={"user_field_names": "true"},
                    json=clean,
                )
                created += 1
        return {"created_rows": created, "updated_rows": updated}

    def _coerce_row_values(
        self,
        row: dict[str, Any],
        fields_by_name: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        clean = dict(row)
        for name, field in fields_by_name.items():
            if field.get("type") != "link_row" or name not in clean:
                continue
            target_table_id = _link_row_table_id_from_field(field)
            if not target_table_id:
                continue
            clean[name] = self._coerce_link_row_value(int(target_table_id), clean[name])
        return clean

    def _coerce_link_row_value(self, target_table_id: int, value: Any) -> list[int]:
        if value in (None, ""):
            return []
        if isinstance(value, list):
            result: list[int] = []
            for item in value:
                result.extend(self._coerce_link_row_value(target_table_id, item))
            return result
        if isinstance(value, dict):
            if value.get("id") is not None:
                return [int(value["id"])]
            if value.get("value") is not None:
                return self._coerce_link_row_value(target_table_id, value["value"])
        if isinstance(value, int):
            return [value]
        return [self._lookup_link_row_id(target_table_id, str(value))]

    def _lookup_link_row_id(self, target_table_id: int, primary_value: str) -> int:
        primary_field = self._primary_field_for_table(target_table_id)
        for row in self._get_rows(target_table_id):
            if str(row.get(primary_field, "")) == primary_value:
                return int(row["id"])
        raise BaserowError(
            f"linked row not found in table {target_table_id}: {primary_value}"
        )

    def _primary_field_for_table(self, table_id: int) -> str:
        fields = _items(self._get(f"/api/database/fields/table/{table_id}/"))
        for field in fields:
            if field.get("primary"):
                return str(field.get("name") or "Name")
        return str(fields[0].get("name") or "Name") if fields else "Name"

    def _resolve_link_row_table_id(self, database_id: int, field: dict[str, Any]) -> int:
        explicit = field.get("link_row_table_id") or field.get("link_table_id")
        if explicit is not None:
            return int(explicit)
        target = (
            field.get("link_row_table")
            or field.get("link_table")
            or field.get("target_table")
            or field.get("target")
        )
        target_id = _link_row_table_id_from_value(target)
        if target_id is not None:
            return target_id
        target_name = target
        target_name = _require_text(target_name, f"{field['name']}.link_row_table")
        tables = _items(self._get(f"/api/database/tables/database/{database_id}/"))
        for table in tables:
            if table.get("name") == target_name:
                return int(table["id"])
        raise BaserowError(f"link_row target table not found: {target_name}")

    def _get_rows(self, table_id: int) -> list[dict[str, Any]]:
        payload = self._get(
            f"/api/database/rows/table/{table_id}/",
            params={"user_field_names": "true", "size": 200},
        )
        return _items(payload)

    def _ensure_views(
        self,
        table_id: int,
        views: list[dict[str, Any]],
        *,
        fields_by_name: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        existing = _items(self._get(f"/api/database/views/table/{table_id}/"))
        existing_by_name = {view.get("name"): view for view in existing}
        created: list[str] = []

        for view_spec in views:
            name = _require_text(view_spec.get("name"), "view.name")
            view_type = _normalise_view_type(str(view_spec.get("type") or "grid"))
            view = existing_by_name.get(name)
            if not view:
                view = self._post(
                    f"/api/database/views/table/{table_id}/",
                    json={"name": name, "type": view_type},
                )
                existing.append(view)
                existing_by_name[name] = view
                created.append(name)
            elif view.get("type") and view.get("type") != view_type:
                view = self._patch(
                    f"/api/database/views/{view['id']}/",
                    json={"type": view_type},
                )
                existing_by_name[name] = view

            view_id = view.get("id")
            if not view_id:
                continue
            existing_filters = _items(
                self._get(f"/api/database/views/{view_id}/filters/")
            )
            for filter_spec in _coerce_record_list(view_spec.get("filters"), "view.filters"):
                payload = _view_filter_payload(filter_spec, fields_by_name)
                if any(_same_view_filter(item, payload) for item in existing_filters):
                    continue
                self._post(
                    f"/api/database/views/{view_id}/filters/",
                    json=payload,
                )
                existing_filters.append(payload)
            sorts = view_spec.get("sorts", view_spec.get("sortings"))
            existing_sorts = _items(
                self._get(f"/api/database/views/{view_id}/sortings/")
            )
            for sort_spec in _coerce_record_list(sorts, "view.sorts"):
                payload = _view_sort_payload(sort_spec, fields_by_name)
                if any(_same_view_sort(item, payload) for item in existing_sorts):
                    continue
                self._post(
                    f"/api/database/views/{view_id}/sortings/",
                    json=payload,
                )
                existing_sorts.append(payload)

        readback = _items(self._get(f"/api/database/views/table/{table_id}/"))
        return {
            "created_views": created,
            "view_names": [view.get("name") for view in readback if view.get("name")],
        }


def _coerce_record_list(value: Any, name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise BaserowError(f"{name} must be a JSON array, got invalid JSON string") from exc
    if not isinstance(value, list):
        raise BaserowError(f"{name} must be a list of objects")
    records: list[dict[str, Any]] = []
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            raise BaserowError(f"{name}[{idx}] must be an object")
        records.append(dict(item))
    return records


def _items(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        for key in ("results", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return list(value)
        return []
    if isinstance(payload, list):
        return list(payload)
    return []


def _normalise_fields(
    fields: list[dict[str, Any]] | None,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not fields:
        first_row = rows[0] if rows else {}
        fields = [{"name": str(name), "type": "text"} for name in first_row.keys()]
    result: list[dict[str, Any]] = []
    for idx, field in enumerate(fields or []):
        name = _require_text(field.get("name"), "field.name")
        field_type = str(field.get("type") or "text").strip().lower()
        normalised = dict(field)
        normalised["name"] = name
        normalised["type"] = _normalise_field_type(field_type)
        if normalised["type"] == "number" and "decimal_places" not in normalised:
            normalised["decimal_places"] = _infer_decimal_places(name, rows)
        if field.get("primary") or idx == 0:
            normalised["primary"] = bool(field.get("primary", idx == 0))
        result.append(normalised)
    return result


def _normalise_field_type(field_type: str) -> str:
    aliases = {
        "string": "text",
        "varchar": "text",
        "textarea": "long_text",
        "longtext": "long_text",
        "select": "single_select",
        "single-select": "single_select",
        "dropdown": "single_select",
        "bool": "boolean",
        "checkbox": "boolean",
        "integer": "number",
        "float": "number",
        "decimal": "number",
        "datetime": "date",
    }
    return aliases.get(field_type, field_type)


def _field_payload(field: dict[str, Any]) -> dict[str, Any]:
    field_type = _normalise_field_type(str(field.get("type") or "text"))
    payload: dict[str, Any] = {"name": field["name"], "type": field_type}
    if field_type in {"single_select", "multiple_select"}:
        options = field.get("options") or field.get("select_options") or []
        payload["select_options"] = _select_options(options)
    elif field_type == "number":
        payload.setdefault("number_decimal_places", int(field.get("decimal_places", 0)))
        payload.setdefault("number_negative", bool(field.get("negative", True)))
    elif field_type == "date":
        payload.setdefault("date_format", field.get("date_format", "ISO"))
        payload.setdefault("date_include_time", bool(field.get("include_time", False)))
    return payload


def _normalise_view_type(view_type: str) -> str:
    aliases = {
        "grid_view": "grid",
        "table": "grid",
        "kanban_view": "kanban",
    }
    return aliases.get(view_type.strip().lower(), view_type.strip().lower())


def _view_filter_payload(
    filter_spec: dict[str, Any],
    fields_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    field = _resolve_field(filter_spec.get("field"), fields_by_name)
    filter_type = str(filter_spec.get("type") or "equal")
    if field.get("type") == "single_select":
        filter_type = {
            "equal": "single_select_equal",
            "not_equal": "single_select_not_equal",
        }.get(filter_type, filter_type)
    payload = {
        "field": int(field["id"]),
        "type": filter_type,
        "value": _view_filter_value(filter_spec.get("value")),
    }
    if "preload_values" in filter_spec:
        payload["preload_values"] = filter_spec["preload_values"]
    return payload


def _view_filter_value(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return ""
    return str(value)


def _view_sort_payload(
    sort_spec: dict[str, Any],
    fields_by_name: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    order = str(sort_spec.get("order") or sort_spec.get("direction") or "ASC").upper()
    if order == "DESCENDING":
        order = "DESC"
    elif order == "ASCENDING":
        order = "ASC"
    if order not in {"ASC", "DESC"}:
        raise BaserowError(f"unsupported sort order: {order}")
    return {
        "field": _resolve_field_id(sort_spec.get("field"), fields_by_name),
        "order": order,
    }


def _resolve_field_id(value: Any, fields_by_name: dict[str, dict[str, Any]]) -> int:
    return int(_resolve_field(value, fields_by_name)["id"])


def _resolve_field(value: Any, fields_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if isinstance(value, int):
        for field in fields_by_name.values():
            if int(field.get("id") or 0) == value:
                return field
        return {"id": value}
    name = _require_text(value, "view field")
    field = fields_by_name.get(name)
    if not field or not field.get("id"):
        raise BaserowError(f"view field not found: {name}")
    return field


def _same_view_filter(existing: dict[str, Any], requested: dict[str, Any]) -> bool:
    return (
        int(existing.get("field") or existing.get("field_id") or 0)
        == int(requested["field"])
        and str(existing.get("type") or "") == str(requested["type"])
        and str(existing.get("value") or "") == str(requested["value"])
    )


def _same_view_sort(existing: dict[str, Any], requested: dict[str, Any]) -> bool:
    return (
        int(existing.get("field") or existing.get("field_id") or 0)
        == int(requested["field"])
        and str(existing.get("order") or "").upper()
        == str(requested["order"]).upper()
    )


def _link_row_table_id_from_field(field: dict[str, Any]) -> int | None:
    return _link_row_table_id_from_value(
        field.get("link_row_table_id")
        or field.get("link_table_id")
        or field.get("link_row_table")
        or field.get("link_table")
    )


def _link_row_table_id_from_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    if isinstance(value, dict):
        for key in ("id", "table_id", "link_row_table_id"):
            nested = _link_row_table_id_from_value(value.get(key))
            if nested is not None:
                return nested
    return None


def _is_blank_row(
    row: dict[str, Any],
    field_names: list[str],
    *,
    primary_field: str | None = None,
) -> bool:
    names = field_names or [
        key for key in row.keys()
        if key not in {"id", "order", "created_on", "updated_on"}
    ]
    if not names:
        return False
    if primary_field and not _is_blank_value(row.get(primary_field)):
        return False
    return all(_is_blank_value(row.get(name)) for name in names)


def _is_blank_value(value: Any) -> bool:
    if value is None:
        return True
    if value is False:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _infer_decimal_places(field_name: str, rows: list[dict[str, Any]]) -> int:
    max_places = 0
    for row in rows:
        value = row.get(field_name)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if "e" in text.lower():
            continue
        if "." in text:
            places = len(text.split(".", 1)[1].rstrip("%"))
            max_places = max(max_places, places)
    return min(max_places, 10)


def _select_options(options: list[Any]) -> list[dict[str, str]]:
    result = []
    for idx, option in enumerate(options):
        if isinstance(option, dict):
            value = option.get("value") or option.get("name")
            color = option.get("color")
        else:
            value = option
            color = None
        value = _require_text(value, "select option")
        result.append({
            "value": value,
            "color": str(color or BaserowClient._SELECT_COLORS[idx % len(BaserowClient._SELECT_COLORS)]),
        })
    return result


def _primary_field_name(fields: list[dict[str, Any]], existing: list[dict[str, Any]]) -> str:
    for field in fields:
        if field.get("primary"):
            return field["name"]
    if fields:
        return fields[0]["name"]
    for field in existing:
        if field.get("primary"):
            return field.get("name", "Name")
    return existing[0].get("name", "Name") if existing else "Name"


def _is_textish(field_type: str) -> bool:
    return _normalise_field_type(field_type) in {"text", "long_text"}


def _require_text(value: Any, name: str) -> str:
    if value is None or not str(value).strip():
        raise BaserowError(f"{name} is required")
    return str(value).strip()


def _json(response: Any, operation: str) -> Any:
    if getattr(response, "status_code", None) == 204:
        return {}
    try:
        return response.json()
    except Exception as exc:
        raise BaserowError(f"{operation} returned non-JSON response") from exc


def _raise(response: Any, operation: str) -> None:
    try:
        response.raise_for_status()
    except Exception as exc:
        status = getattr(response, "status_code", "?")
        text = getattr(response, "text", "")
        raise BaserowError(f"{operation} failed with HTTP {status}: {text}") from exc
