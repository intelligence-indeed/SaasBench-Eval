import pytest

from saas_agent.baserow_helper import BaserowClient, BaserowError


@pytest.fixture(autouse=True)
def _baserow_credentials(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_BASEROW_EMAIL", "agent@example.test")
    monkeypatch.setenv("SAAS_AGENT_BASEROW_PASSWORD", "test-password")


def test_credentials_are_required(monkeypatch):
    monkeypatch.delenv("SAAS_AGENT_BASEROW_EMAIL")
    monkeypatch.delenv("SAAS_AGENT_BASEROW_PASSWORD")

    with pytest.raises(BaserowError, match="credentials are required"):
        BaserowClient("http://baserow.test", session=FakeSession())


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


class FakeSession:
    def __init__(self):
        self.calls = []
        self.apps = [{"id": 10, "name": "Existing DB", "type": "database"}]
        self.tables = {10: [{"id": 20, "name": "Existing Table"}]}
        self.fields = {
            20: [{"id": 1, "name": "Name", "type": "text", "primary": True}]
        }
        self.rows = {20: []}
        self.views = {20: [{"id": 70, "name": "Grid", "type": "grid"}]}
        self.view_filters = {}
        self.view_sortings = {}
        self.next_db = 30
        self.next_table = 40
        self.next_field = 50
        self.next_view = 80

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        payload = kwargs.get("json") or {}
        if url.endswith("/api/user/token-auth/"):
            return FakeResponse(payload={"token": "tok"})
        if "/api/applications/group/" in url or "/api/applications/workspace/" in url:
            app = {"id": self.next_db, "name": payload["name"], "type": payload["type"]}
            self.next_db += 1
            self.apps.append(app)
            self.tables[app["id"]] = []
            return FakeResponse(payload=app)
        if "/api/database/tables/database/" in url:
            db_id = int(url.rstrip("/").split("/")[-1])
            table = {"id": self.next_table, "name": payload["name"]}
            self.next_table += 1
            self.tables.setdefault(db_id, []).append(table)
            self.fields[table["id"]] = [
                {"id": self.next_field, "name": "Name", "type": "text", "primary": True}
            ]
            self.next_field += 1
            self.rows[table["id"]] = []
            self.views[table["id"]] = [{"id": self.next_view, "name": "Grid", "type": "grid"}]
            self.next_view += 1
            return FakeResponse(payload=table)
        if "/api/database/fields/table/" in url:
            table_id = int(url.rstrip("/").split("/")[-1])
            field = {"id": self.next_field, **payload}
            self.next_field += 1
            self.fields.setdefault(table_id, []).append(field)
            return FakeResponse(payload=field)
        if "/api/database/views/table/" in url:
            table_id = int(url.rstrip("/").split("/")[-1])
            view = {"id": self.next_view, **payload}
            self.next_view += 1
            self.views.setdefault(table_id, []).append(view)
            return FakeResponse(payload=view)
        if "/api/database/views/" in url and url.rstrip("/").endswith("/filters"):
            view_id = int(url.split("/api/database/views/")[1].split("/")[0])
            self.view_filters.setdefault(view_id, []).append(payload)
            return FakeResponse(payload={"id": len(self.view_filters[view_id]), **payload})
        if "/api/database/views/" in url and url.rstrip("/").endswith("/sortings"):
            view_id = int(url.split("/api/database/views/")[1].split("/")[0])
            self.view_sortings.setdefault(view_id, []).append(payload)
            return FakeResponse(payload={"id": len(self.view_sortings[view_id]), **payload})
        if "/api/database/rows/table/" in url:
            table_id = int(url.split("/api/database/rows/table/")[1].split("/")[0])
            row = dict(payload)
            row["id"] = len(self.rows.setdefault(table_id, [])) + 1
            self.rows[table_id].append(row)
            return FakeResponse(payload=row)
        if url.endswith("/api/groups/"):
            return FakeResponse(payload=[{"id": 1}])
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/api/applications/"):
            return FakeResponse(payload=list(self.apps))
        if url.endswith("/api/groups/"):
            return FakeResponse(payload=[{"id": 1, "name": "Default"}])
        if "/api/database/tables/database/" in url:
            db_id = int(url.rstrip("/").split("/")[-1])
            return FakeResponse(payload=list(self.tables.get(db_id, [])))
        if "/api/database/fields/table/" in url:
            table_id = int(url.rstrip("/").split("/")[-1])
            return FakeResponse(payload=list(self.fields.get(table_id, [])))
        if "/api/database/views/table/" in url:
            table_id = int(url.rstrip("/").split("/")[-1])
            return FakeResponse(payload=list(self.views.get(table_id, [])))
        if "/api/database/views/" in url and url.rstrip("/").endswith("/filters"):
            view_id = int(url.split("/api/database/views/")[1].split("/")[0])
            return FakeResponse(payload=list(self.view_filters.get(view_id, [])))
        if "/api/database/views/" in url and url.rstrip("/").endswith("/sortings"):
            view_id = int(url.split("/api/database/views/")[1].split("/")[0])
            return FakeResponse(payload=list(self.view_sortings.get(view_id, [])))
        if "/api/database/rows/table/" in url:
            table_id = int(url.split("/api/database/rows/table/")[1].split("/")[0])
            return FakeResponse(payload={"results": list(self.rows.get(table_id, []))})
        raise AssertionError(f"unexpected GET {url}")

    def patch(self, url, **kwargs):
        self.calls.append(("PATCH", url, kwargs))
        if "/api/database/rows/table/" in url:
            parts = url.rstrip("/").split("/")
            row_id = int(parts[-1])
            table_id = int(parts[-2])
            payload = kwargs.get("json") or {}
            for row in self.rows.get(table_id, []):
                if row.get("id") == row_id:
                    row.update(payload)
                    return FakeResponse(payload=row)
            return FakeResponse(404, text="row not found")
        if "/api/database/views/" in url:
            view_id = int(url.rstrip("/").split("/")[-1])
            payload = kwargs.get("json") or {}
            for views in self.views.values():
                for view in views:
                    if view["id"] == view_id:
                        view.update(payload)
                        return FakeResponse(payload=view)
            return FakeResponse(404, text="view not found")
        field_id = int(url.rstrip("/").split("/")[-1])
        payload = kwargs.get("json") or {}
        for fields in self.fields.values():
            for field in fields:
                if field["id"] == field_id:
                    field.update(payload)
                    return FakeResponse(payload=field)
        return FakeResponse(404, text="not found")

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        if "/api/database/rows/table/" in url:
            parts = url.rstrip("/").split("/")
            row_id = int(parts[-1])
            table_id = int(parts[-2])
            before = len(self.rows.get(table_id, []))
            self.rows[table_id] = [
                row for row in self.rows.get(table_id, [])
                if row.get("id") != row_id
            ]
            if len(self.rows[table_id]) == before:
                return FakeResponse(404, text="row not found")
            return FakeResponse(status_code=204, payload={})
        raise AssertionError(f"unexpected DELETE {url}")


def test_ensure_table_creates_database_table_fields_rows_and_readback():
    session = FakeSession()
    client = BaserowClient("http://baserow.test", session=session)

    result = client.ensure_table(
        database_name="Endpoint Governance",
        table_name="API Endpoint Registry",
        fields=[
            {"name": "Endpoint ID", "type": "text", "primary": True},
            {"name": "Method", "type": "single_select", "options": ["GET", "POST"]},
            {"name": "Deprecated", "type": "boolean"},
        ],
        rows=[
            {"Endpoint ID": "EP-001", "Method": "GET", "Deprecated": False},
            {"Endpoint ID": "EP-002", "Method": "POST", "Deprecated": True},
        ],
    )

    assert result["database_name"] == "Endpoint Governance"
    assert result["table_name"] == "API Endpoint Registry"
    assert result["created_rows"] == 2
    assert result["row_count"] == 2
    assert result["field_names"] == ["Endpoint ID", "Method", "Deprecated"]
    field_posts = [
        call for call in session.calls
        if call[0] == "POST" and "/api/database/fields/table/" in call[1]
    ]
    assert field_posts[0][2]["json"]["select_options"][0]["value"] == "GET"


def test_existing_table_is_reused_and_missing_field_is_added():
    session = FakeSession()
    client = BaserowClient("http://baserow.test", session=session)

    result = client.ensure_table(
        database_name="Existing DB",
        table_name="Existing Table",
        fields=[
            {"name": "Name", "type": "text", "primary": True},
            {"name": "Owner", "type": "text"},
        ],
        rows=[{"Name": "todo-api", "Owner": "Backend"}],
    )

    assert result["database_id"] == 10
    assert result["table_id"] == 20
    assert result["created_fields"] == ["Owner"]
    assert result["created_rows"] == 1


def test_schema_metadata_maps_human_names_to_dynamic_postgres_names():
    session = FakeSession()
    client = BaserowClient("http://baserow.test", session=session)

    metadata = client.schema_metadata()

    assert metadata == [{
        "database_id": 10,
        "database_name": "Existing DB",
        "table_id": 20,
        "table_name": "Existing Table",
        "physical_table_name": "database_table_20",
        "fields": [{
            "field_id": 1,
            "field_name": "Name",
            "physical_field_name": "field_1",
            "primary": True,
        }],
    }]


def test_existing_row_is_updated_by_primary_field():
    session = FakeSession()
    session.rows[20].append({"id": 7, "Name": "todo-api", "Owner": "Old"})
    client = BaserowClient("http://baserow.test", session=session)

    result = client.ensure_table(
        database_name="Existing DB",
        table_name="Existing Table",
        fields=[
            {"name": "Name", "type": "text", "primary": True},
            {"name": "Owner", "type": "text"},
        ],
        rows=[{"Name": "todo-api", "Owner": "Backend"}],
    )

    assert result["created_rows"] == 0
    assert result["updated_rows"] == 1
    assert session.rows[20][0]["Owner"] == "Backend"


def test_json_string_arguments_are_accepted():
    session = FakeSession()
    client = BaserowClient("http://baserow.test", session=session)

    result = client.ensure_table(
        database_name="Existing DB",
        table_name="Existing Table",
        fields='[{"name": "Name", "type": "text", "primary": true}]',
        rows='[{"Name": "todo-api"}]',
    )

    assert result["created_rows"] == 1


def test_blank_default_rows_are_removed_before_readback():
    session = FakeSession()
    session.rows[20] = [
        {"id": 1, "Name": "", "Owner": None},
        {"id": 2, "Name": None, "Owner": ""},
    ]
    client = BaserowClient("http://baserow.test", session=session)

    result = client.ensure_table(
        database_name="Existing DB",
        table_name="Existing Table",
        fields=[
            {"name": "Name", "type": "text", "primary": True},
            {"name": "Owner", "type": "text"},
        ],
        rows=[{"Name": "todo-api", "Owner": "Backend"}],
    )

    assert result["deleted_blank_rows"] == 2
    assert result["row_count"] == 1
    assert [row["Name"] for row in session.rows[20]] == ["todo-api"]


def test_number_decimal_places_are_inferred_from_row_values():
    session = FakeSession()
    client = BaserowClient("http://baserow.test", session=session)

    client.ensure_table(
        database_name="Coverage Audit",
        table_name="Coverage By Module",
        fields=[
            {"name": "Module", "type": "text", "primary": True},
            {"name": "Coverage Pct", "type": "number"},
        ],
        rows=[
            {"Module": "src/api.js", "Coverage Pct": "0.00"},
            {"Module": "src/ui.js", "Coverage Pct": 87.5},
        ],
    )

    field_posts = [
        call for call in session.calls
        if call[0] == "POST" and "/api/database/fields/table/" in call[1]
    ]
    coverage_field = next(call for call in field_posts if call[2]["json"]["name"] == "Coverage Pct")
    assert coverage_field[2]["json"]["number_decimal_places"] == 2


def test_named_views_are_created_with_filters_and_sortings():
    session = FakeSession()
    client = BaserowClient("http://baserow.test", session=session)

    result = client.ensure_table(
        database_name="Coverage Audit",
        table_name="Coverage By Module",
        fields=[
            {"name": "Module", "type": "text", "primary": True},
            {"name": "Coverage Pct", "type": "number"},
            {"name": "Below Threshold", "type": "boolean"},
        ],
        rows=[{"Module": "src/api.js", "Coverage Pct": "0.00", "Below Threshold": True}],
        views=[
            {
                "name": "Remediation Queue",
                "type": "grid",
                "filters": [{"field": "Below Threshold", "type": "equal", "value": True}],
                "sorts": [{"field": "Coverage Pct", "order": "asc"}],
            }
        ],
    )

    assert result["view_names"] == ["Grid", "Remediation Queue"]
    created_view = next(view for view in session.views[result["table_id"]] if view["name"] == "Remediation Queue")
    assert session.view_filters[created_view["id"]][0]["field"] == 52
    assert session.view_filters[created_view["id"]][0]["value"] == "true"
    assert session.view_sortings[created_view["id"]][0] == {"field": 51, "order": "ASC"}


def test_single_select_filter_is_normalized_and_view_options_are_idempotent():
    session = FakeSession()
    client = BaserowClient("http://baserow.test", session=session)
    fields = [
        {"name": "Name", "type": "text", "primary": True},
        {"name": "Status", "type": "single_select", "options": ["High", "Low"]},
    ]
    views = [{
        "name": "High Only",
        "type": "grid",
        "filters": [{"field": "Status", "type": "equal", "value": "High"}],
        "sorts": [{"field": "Name", "order": "asc"}],
    }]

    client.ensure_table(
        database_name="Existing DB",
        table_name="Existing Table",
        fields=fields,
        rows=[{"Name": "one", "Status": "High"}],
        views=views,
    )
    client.ensure_table(
        database_name="Existing DB",
        table_name="Existing Table",
        fields=fields,
        rows=[{"Name": "one", "Status": "High"}],
        views=views,
    )

    view = next(item for item in session.views[20] if item["name"] == "High Only")
    assert session.view_filters[view["id"]] == [{
        "field": 50,
        "type": "single_select_equal",
        "value": "High",
    }]
    assert session.view_sortings[view["id"]] == [{"field": 1, "order": "ASC"}]


def test_false_boolean_default_row_is_removed_but_real_zero_row_is_kept():
    session = FakeSession()
    session.fields[20] = [
        {"id": 1, "name": "Name", "type": "text", "primary": True},
        {"id": 2, "name": "Score", "type": "number"},
        {"id": 3, "name": "Below Threshold", "type": "boolean"},
    ]
    session.rows[20] = [
        {"id": 1, "Name": "", "Score": None, "Below Threshold": False},
        {"id": 2, "Name": "tabler", "Score": 0, "Below Threshold": True},
    ]
    client = BaserowClient("http://baserow.test", session=session)

    result = client.ensure_table(
        database_name="Existing DB",
        table_name="Existing Table",
        fields=[
            {"name": "Name", "type": "text", "primary": True},
            {"name": "Score", "type": "number"},
            {"name": "Below Threshold", "type": "boolean"},
        ],
        rows=[],
    )

    assert result["deleted_blank_rows"] == 1
    assert result["row_count"] == 1
    assert session.rows[20][0]["Name"] == "tabler"
    assert session.rows[20][0]["Score"] == 0


def test_replace_rows_deletes_existing_non_blank_rows_before_insert():
    session = FakeSession()
    session.rows[20] = [
        {"id": 1, "Name": "old", "Owner": "Old"},
        {"id": 2, "Name": "stale", "Owner": "Stale"},
    ]
    client = BaserowClient("http://baserow.test", session=session)

    result = client.ensure_table(
        database_name="Existing DB",
        table_name="Existing Table",
        fields=[
            {"name": "Name", "type": "text", "primary": True},
            {"name": "Owner", "type": "text"},
        ],
        rows=[{"Name": "todo-api", "Owner": "Backend"}],
        replace_rows=True,
    )

    assert result["deleted_rows"] == 2
    assert result["created_rows"] == 1
    assert result["updated_rows"] == 0
    assert [row["Name"] for row in session.rows[20]] == ["todo-api"]


def test_link_row_field_resolves_target_table_and_row_values():
    session = FakeSession()
    session.tables[10].append({"id": 21, "name": "Dependency Edges"})
    session.fields[21] = [{"id": 60, "name": "Name", "type": "text", "primary": True}]
    session.rows[21] = []
    session.rows[20] = [
        {"id": 7, "Name": "tabler", "Owner": "Frontend"},
        {"id": 8, "Name": "todo-api", "Owner": "Backend"},
    ]
    client = BaserowClient("http://baserow.test", session=session)

    result = client.ensure_table(
        database_name="Existing DB",
        table_name="Dependency Edges",
        fields=[
            {"name": "Edge ID", "type": "text", "primary": True},
            {"name": "Source Project", "type": "link_row", "link_row_table": "Existing Table"},
            {"name": "Target Project", "type": "link_row", "link_row_table": "Existing Table"},
        ],
        rows=[
            {
                "Edge ID": "DE-001",
                "Source Project": "tabler",
                "Target Project": "todo-api",
            }
        ],
    )

    field_posts = [
        call for call in session.calls
        if call[0] == "POST" and "/api/database/fields/table/21" in call[1]
    ]
    link_payloads = [call[2]["json"] for call in field_posts if call[2]["json"]["type"] == "link_row"]
    assert link_payloads[0]["link_row_table_id"] == 20
    assert session.rows[result["table_id"]][0]["Source Project"] == [7]
    assert session.rows[result["table_id"]][0]["Target Project"] == [8]


def test_paginated_workspace_response_is_supported():
    class PagedSession(FakeSession):
        def get(self, url, **kwargs):
            if url.endswith("/api/applications/"):
                return FakeResponse(payload={"results": list(self.apps)})
            if url.endswith("/api/groups/"):
                return FakeResponse(payload={"results": [{"id": 1, "name": "Default"}]})
            return super().get(url, **kwargs)

    session = PagedSession()
    client = BaserowClient("http://baserow.test", session=session)

    result = client.ensure_table(
        database_name="New DB",
        table_name="New Table",
        fields=[{"name": "Name", "type": "text"}],
        rows=[],
    )

    assert result["database_name"] == "New DB"
    assert result["table_name"] == "New Table"


def test_empty_base_url_is_rejected():
    with pytest.raises(BaserowError, match="base_url"):
        BaserowClient("")
