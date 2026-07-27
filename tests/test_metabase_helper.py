import json
from urllib.parse import parse_qs, urlparse

import pytest

from saas_agent.metabase_helper import MetabaseClient, MetabaseError


@pytest.fixture(autouse=True)
def _metabase_credentials(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_METABASE_USERNAME", "agent@example.test")
    monkeypatch.setenv("SAAS_AGENT_METABASE_PASSWORD", "test-password")
    monkeypatch.setenv("SAAS_AGENT_BASEROW_PG_DATABASE", "app_database")
    monkeypatch.setenv("SAAS_AGENT_BASEROW_PG_USERNAME", "app_user")
    monkeypatch.setenv("SAAS_AGENT_BASEROW_PG_PASSWORD", "pg-test-password")


def test_connection_credentials_are_required(monkeypatch):
    for name in (
        "SAAS_AGENT_METABASE_USERNAME",
        "SAAS_AGENT_METABASE_PASSWORD",
        "SAAS_AGENT_BASEROW_PG_DATABASE",
        "SAAS_AGENT_BASEROW_PG_USERNAME",
        "SAAS_AGENT_BASEROW_PG_PASSWORD",
    ):
        monkeypatch.delenv(name)

    with pytest.raises(MetabaseError, match="missing required connection credentials"):
        MetabaseClient(
            "http://metabase.test",
            baserow_host="database.test",
            baserow_port=5432,
            session=FakeMetabaseSession(),
        )


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=None):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if text is None and payload is not None else (text or "")

    def json(self):
        return self.payload


def metadata_fixture():
    return {
        "tables": [
            {
                "id": 101,
                "name": "events",
                "display_name": "Events",
                "schema": "public",
                "fields": [
                    {"id": 1001, "name": "event_id", "display_name": "Event ID", "base_type": "type/Text"},
                    {"id": 1002, "name": "owner_id", "display_name": "Owner ID", "base_type": "type/Text"},
                    {"id": 1003, "name": "category", "display_name": "Category", "base_type": "type/Text"},
                    {"id": 1004, "name": "duration", "display_name": "Duration", "base_type": "type/Float"},
                    {"id": 1005, "name": "active", "display_name": "Active", "base_type": "type/Boolean"},
                ],
            },
            {
                "id": 102,
                "name": "owners",
                "display_name": "Owners",
                "schema": "public",
                "fields": [
                    {"id": 2001, "name": "owner_id", "display_name": "Owner ID", "base_type": "type/Text"},
                    {"id": 2002, "name": "team", "display_name": "Team", "base_type": "type/Text"},
                ],
            },
        ]
    }


class FakeMetabaseSession:
    def __init__(self, *, session_properties_status=200):
        self.headers = {}
        self.calls = []
        self.session_properties_status = session_properties_status
        self.collections = []
        self.databases = []
        self.cards = []
        self.dashboards = []
        self.metadata = metadata_fixture()
        self.next_collection_id = 20
        self.next_database_id = 10
        self.next_card_id = 30
        self.next_dashboard_id = 40

    def _request_info(self, url, kwargs):
        parsed = urlparse(url)
        return parsed.path, parse_qs(parsed.query), kwargs.get("json")

    def get(self, url, **kwargs):
        path, query, payload = self._request_info(url, kwargs)
        self.calls.append(("GET", path, query, payload, dict(self.headers)))
        if path == "/api/session/properties":
            return FakeResponse(
                {"version": {"tag": "v0.test", "branch": "mock"}},
                status_code=self.session_properties_status,
            )
        if path == "/api/collection":
            return FakeResponse(self.collections)
        if path == "/api/database":
            return FakeResponse(self.databases)
        if path.startswith("/api/database/") and path.endswith("/metadata"):
            return FakeResponse(self.metadata)
        if path.startswith("/api/collection/") and path.endswith("/items"):
            collection_id = int(path.split("/")[3])
            models = set(query.get("models", []))
            items = []
            if not models or "card" in models:
                items.extend(
                    {**card, "model": "card"}
                    for card in self.cards
                    if card.get("collection_id") == collection_id
                )
            if not models or "dashboard" in models:
                items.extend(
                    {**dashboard, "model": "dashboard"}
                    for dashboard in self.dashboards
                    if dashboard.get("collection_id") == collection_id
                )
            return FakeResponse({"data": items})
        if path.startswith("/api/dashboard/"):
            dashboard_id = int(path.rsplit("/", 1)[1])
            return FakeResponse(next(item for item in self.dashboards if item["id"] == dashboard_id))
        raise AssertionError(f"unexpected GET {path}")

    def post(self, url, **kwargs):
        path, query, payload = self._request_info(url, kwargs)
        self.calls.append(("POST", path, query, payload, dict(self.headers)))
        if path == "/api/session":
            return FakeResponse({"id": "session-token"})
        if path == "/api/database":
            database = {**payload, "id": self.next_database_id}
            self.next_database_id += 1
            self.databases.append(database)
            return FakeResponse(database, 201)
        if path.endswith("/sync_schema"):
            return FakeResponse({}, 200)
        if path == "/api/collection":
            collection = {**payload, "id": self.next_collection_id}
            self.next_collection_id += 1
            self.collections.append(collection)
            return FakeResponse(collection, 201)
        if path == "/api/card":
            card = {**payload, "id": self.next_card_id, "model": "card"}
            self.next_card_id += 1
            self.cards.append(card)
            return FakeResponse(card, 201)
        if path.startswith("/api/card/") and path.endswith("/query"):
            return FakeResponse({
                "data": {
                    "cols": [{"name": "category", "display_name": "Category"}, {"name": "count", "display_name": "Count"}],
                    "rows": [["A", 3]],
                }
            })
        if path == "/api/dashboard":
            dashboard = {
                **payload,
                "id": self.next_dashboard_id,
                "model": "dashboard",
                "dashcards": [],
            }
            self.next_dashboard_id += 1
            self.dashboards.append(dashboard)
            return FakeResponse(dashboard, 201)
        if path.startswith("/api/dashboard/") and path.endswith("/cards"):
            dashboard_id = int(path.split("/")[3])
            card_id = payload.get("cardId", payload.get("card_id"))
            card = next(item for item in self.cards if item["id"] == card_id)
            dashboard = next(item for item in self.dashboards if item["id"] == dashboard_id)
            dashboard["dashcards"].append({
                "id": len(dashboard["dashcards"]) + 1,
                "card": {"id": card_id, "name": card["name"]},
            })
            return FakeResponse(dashboard["dashcards"][-1], 201)
        raise AssertionError(f"unexpected POST {path}")

    def put(self, url, **kwargs):
        path, query, payload = self._request_info(url, kwargs)
        self.calls.append(("PUT", path, query, payload, dict(self.headers)))
        if path.startswith("/api/database/"):
            item_id = int(path.rsplit("/", 1)[1])
            item = next(item for item in self.databases if item["id"] == item_id)
        elif path.startswith("/api/card/"):
            item_id = int(path.rsplit("/", 1)[1])
            item = next(item for item in self.cards if item["id"] == item_id)
        elif path.startswith("/api/dashboard/"):
            item_id = int(path.rsplit("/", 1)[1])
            item = next(item for item in self.dashboards if item["id"] == item_id)
        else:
            raise AssertionError(f"unexpected PUT {path}")
        item.update(payload)
        return FakeResponse(item)


def make_client(session=None):
    return MetabaseClient(
        "http://localhost:32002",
        baserow_host="host.docker.internal",
        baserow_port=32018,
        session=session or FakeMetabaseSession(),
        sleep=lambda _seconds: None,
    )


def question_specs():
    return [
        {
            "name": "Events by Category",
            "source_table": "Events",
            "display": "bar",
            "aggregations": [{"op": "count"}],
            "breakouts": ["Category"],
        },
        {
            "name": "Average Duration by Team",
            "source_table": "Events",
            "display": "table",
            "aggregations": [{"op": "avg", "field": "Duration"}],
            "breakouts": ["Owners.Team"],
            "joins": [{
                "table": "Owners",
                "source_field": "Owner ID",
                "target_field": "Owner ID",
            }],
            "filters": [{"field": "Active", "op": "=", "value": True}],
            "order_by": [{"aggregation_index": 0, "direction": "desc"}],
            "limit": 10,
        },
    ]


def test_preflight_authenticates_without_returning_credentials():
    session = FakeMetabaseSession()
    client = make_client(session)

    result = client.preflight()

    assert result["authenticated"] is True
    assert result["runtime"] == {
        "available": True,
        "version": {"tag": "v0.test", "branch": "mock"},
    }
    assert session.headers["X-Metabase-Session"] == "session-token"
    assert "password" not in json.dumps(result).lower()


def test_preflight_keeps_runtime_version_probe_non_fatal():
    client = make_client(FakeMetabaseSession(session_properties_status=404))

    result = client.preflight()

    assert result["authenticated"] is True
    assert result["runtime"]["available"] is False
    assert "HTTP 404" in result["runtime"]["error"]


def test_inspect_schema_creates_then_reuses_baserow_connection():
    session = FakeMetabaseSession()
    client = make_client(session)

    first = client.inspect_schema(["Events"], poll_interval_s=0)
    second = client.inspect_schema(["Events"], poll_interval_s=0)

    assert len(session.databases) == 1
    assert first["database"]["created"] is True
    assert second["database"]["updated"] is True
    assert first["tables"][0]["display_name"] == "Events"
    assert "password" not in json.dumps(first).lower()


def test_dynamic_baserow_table_and_field_names_are_aliased_from_rest_metadata():
    session = FakeMetabaseSession()
    session.metadata = {
        "tables": [{
            "id": 201,
            "name": "database_table_20",
            "display_name": "Database Table 20",
            "schema": "public",
            "fields": [{
                "id": 2001,
                "name": "field_1",
                "display_name": "Field 1",
                "base_type": "type/Text",
            }],
        }]
    }

    class FakeBaserowSchemaClient:
        def schema_metadata(self):
            return [{
                "database_name": "Analytics",
                "table_name": "Open Positions",
                "physical_table_name": "database_table_20",
                "fields": [{
                    "field_name": "Position ID",
                    "physical_field_name": "field_1",
                }],
            }]

    client = MetabaseClient(
        "http://localhost:32002",
        baserow_host="host.docker.internal",
        baserow_port=32018,
        baserow_api_client=FakeBaserowSchemaClient(),
        session=session,
        sleep=lambda _seconds: None,
    )

    inspected = client.inspect_schema(["Open Positions"], poll_interval_s=0)
    dataset_query, resolved = client.compile_question(
        10,
        session.metadata,
        {
            "name": "Positions",
            "source_table": "Open Positions",
            "display": "table",
            "columns": ["Position ID"],
        },
    )

    assert inspected["tables"][0]["display_name"] == "Open Positions"
    assert inspected["tables"][0]["fields"][0]["display_name"] == "Position ID"
    assert dataset_query["query"]["source-table"] == 201
    assert dataset_query["query"]["fields"] == [["field", 2001, None]]
    assert resolved["source_table"] == "Open Positions"


def test_compile_question_supports_join_filter_aggregation_sort_and_limit():
    client = make_client()

    dataset_query, resolved = client.compile_question(
        10,
        metadata_fixture(),
        question_specs()[1],
    )

    query = dataset_query["query"]
    assert dataset_query["database"] == 10
    assert query["source-table"] == 101
    assert query["joins"][0]["source-table"] == 102
    assert query["joins"][0]["alias"] == "Owners"
    assert query["aggregation"] == [["avg", ["field", 1004, None]]]
    assert query["breakout"] == [["field", 2002, {"join-alias": "Owners"}]]
    assert query["filter"] == ["=", ["field", 1005, None], True]
    assert query["order-by"] == [["desc", ["aggregation", 0]]]
    assert query["limit"] == 10
    assert resolved["join_tables"] == ["Owners"]


def test_compile_question_supports_ordered_chained_joins():
    metadata = metadata_fixture()
    metadata["tables"][1]["fields"].append(
        {"id": 2003, "name": "team_id", "display_name": "Team ID", "base_type": "type/Text"}
    )
    metadata["tables"].append({
        "id": 103,
        "name": "teams",
        "display_name": "Teams",
        "schema": "public",
        "fields": [
            {"id": 3001, "name": "team_id", "display_name": "Team ID", "base_type": "type/Text"},
            {"id": 3002, "name": "name", "display_name": "Name", "base_type": "type/Text"},
        ],
    })
    client = make_client()

    dataset_query, resolved = client.compile_question(10, metadata, {
        "name": "Events by Team",
        "source_table": "Events",
        "display": "table",
        "aggregations": [{"op": "count"}],
        "breakouts": ["Teams.Name"],
        "joins": [
            {
                "table": "Owners",
                "source_field": "Owner ID",
                "target_field": "Owner ID",
            },
            {
                "table": "Teams",
                "source_table": "Owners",
                "source_field": "Team ID",
                "target_field": "Team ID",
            },
        ],
    })

    joins = dataset_query["query"]["joins"]
    assert joins[1]["condition"] == [
        "=",
        ["field", 2003, {"join-alias": "Owners"}],
        ["field", 3001, {"join-alias": "Teams"}],
    ]
    assert dataset_query["query"]["breakout"] == [
        ["field", 3002, {"join-alias": "Teams"}]
    ]
    assert resolved["join_tables"] == ["Owners", "Teams"]


def test_question_omits_blank_description_rejected_by_metabase():
    session = FakeMetabaseSession()
    client = make_client(session)

    client.ensure_question(
        20,
        {"name": "Events", "display": "table"},
        {"database": 10, "type": "query", "query": {"source-table": 101}},
    )

    assert "description" not in session.cards[0]


def test_post_retries_known_metabase_internal_sequence_collisions():
    class SequenceCollisionSession(FakeMetabaseSession):
        def __init__(self):
            super().__init__()
            self.collisions_remaining = 2

        def post(self, url, **kwargs):
            path, query, payload = self._request_info(url, kwargs)
            if path == "/api/collection" and self.collisions_remaining:
                self.calls.append(("POST", path, query, payload, dict(self.headers)))
                self.collisions_remaining -= 1
                return FakeResponse(
                    status_code=500,
                    text="Unique index or primary key violation: PRIMARY KEY ON PUBLIC.COLLECTION",
                )
            return super().post(url, **kwargs)

    session = SequenceCollisionSession()
    client = make_client(session)

    result = client.ensure_collection("Recovered Collection")

    assert result["created"] is True
    assert len([
        call for call in session.calls
        if call[0] == "POST" and call[1] == "/api/collection"
    ]) == 3


def test_dashboard_card_add_falls_back_to_dashboard_put():
    class LegacyDashboardSession(FakeMetabaseSession):
        def post(self, url, **kwargs):
            path, query, payload = self._request_info(url, kwargs)
            if path.startswith("/api/dashboard/") and path.endswith("/cards"):
                self.calls.append(("POST", path, query, payload, dict(self.headers)))
                return FakeResponse(status_code=404, text="not found")
            return super().post(url, **kwargs)

    session = LegacyDashboardSession()
    client = make_client(session)

    result = client.ensure_analytics(
        "Event Analytics",
        question_specs(),
        {
            "name": "Event Overview",
            "description": "Exact overview description",
            "question_names": ["Events by Category", "Average Duration by Team"],
        },
    )

    assert result["dashboard"]["readback"]["target_cards_present"] is True
    assert any(
        call[0] == "PUT" and call[1].startswith("/api/dashboard/")
        and "dashcards" in call[3]
        for call in session.calls
    )


def test_ensure_analytics_is_idempotent_and_reads_queries_and_dashboard():
    session = FakeMetabaseSession()
    client = make_client(session)
    dashboard = {
        "name": "Event Overview",
        "description": "Exact overview description",
        "question_names": ["Events by Category", "Average Duration by Team"],
    }

    first = client.ensure_analytics("Event Analytics", question_specs(), dashboard)
    second = client.ensure_analytics("Event Analytics", question_specs(), dashboard)

    assert len(session.databases) == 1
    assert len(session.collections) == 1
    assert len(session.cards) == 2
    assert len(session.dashboards) == 1
    assert len(session.dashboards[0]["dashcards"]) == 2
    assert first["dashboard"]["readback"]["target_cards_present"] is True
    assert first["questions"][0]["query_readback"] == {
        "row_count": 1,
        "columns": ["Category", "Count"],
        "sample_rows": [["A", 3]],
    }
    assert second["collection"]["created"] is False
    assert all(question["updated"] for question in second["questions"])


def test_ambiguous_table_or_field_is_rejected():
    metadata = metadata_fixture()
    metadata["tables"].append({
        **metadata["tables"][0],
        "id": 999,
        "name": "Events",
    })
    client = make_client()

    with pytest.raises(MetabaseError, match="expected exactly one table"):
        client.compile_question(10, metadata, question_specs()[0])


def test_dashboard_question_names_must_match_ensured_questions():
    client = make_client()

    with pytest.raises(MetabaseError, match="must match"):
        client.ensure_analytics(
            "Event Analytics",
            question_specs(),
            {"name": "Event Overview", "question_names": ["Events by Category"]},
        )
