import re

import pytest

from saas_agent.bigcapital_helper import BigCapitalClient, BigCapitalError


@pytest.fixture(autouse=True)
def _bigcapital_credentials(monkeypatch):
    monkeypatch.setenv("SAAS_AGENT_BIGCAPITAL_EMAIL", "agent@example.test")
    monkeypatch.setenv("SAAS_AGENT_BIGCAPITAL_PASSWORD", "test-password")


def test_credentials_are_required(monkeypatch):
    monkeypatch.delenv("SAAS_AGENT_BIGCAPITAL_EMAIL")
    monkeypatch.delenv("SAAS_AGENT_BIGCAPITAL_PASSWORD")

    with pytest.raises(BigCapitalError, match="credentials are required"):
        BigCapitalClient("http://bigcapital.test", session=Session())


class Response:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = str(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Session:
    def __init__(self):
        self.headers = {}
        self.customers = []
        self.writes = []
        self.list_calls = []

    def post(self, url, json, timeout):
        if url.endswith("/api/auth/signin"):
            return Response({"access_token": "token", "organization_id": "org"})
        self.writes.append(("post", url, dict(json)))
        customer = dict(json)
        customer["id"] = max([item["id"] for item in self.customers] or [0]) + 1
        self.customers.append(customer)
        return Response(customer, 201)

    def patch(self, url, json, timeout):
        self.writes.append(("patch", url, dict(json)))
        customer = self._find(url)
        if customer is None:
            return Response({"error": "not found"}, 404)
        customer.update(json)
        return Response(customer)

    def put(self, url, json, timeout):
        self.writes.append(("put", url, dict(json)))
        customer = self._find(url)
        if customer is None:
            return Response({"error": "not found"}, 404)
        customer.clear()
        customer.update(json)
        customer["id"] = int(url.rsplit("/", 1)[-1])
        return Response(customer)

    def get(self, url, params, timeout):
        match = re.search(r"/api/customers/(\d+)$", url)
        if match:
            customer = self._find(url)
            if customer is None:
                return Response({"error": "not found"}, 404)
            return Response({"data": {"customer": dict(customer)}})
        page = int((params or {}).get("page", 1))
        page_size = int((params or {}).get("page_size", 200))
        self.list_calls.append((page, page_size))
        start = (page - 1) * page_size
        rows = list(self.customers[start:start + page_size])
        return Response({
            "data": {
                "customers": rows,
                "pagination": {"page": page, "total": len(self.customers)},
            }
        })

    def _find(self, url):
        customer_id = int(url.rsplit("/", 1)[-1])
        return next((item for item in self.customers if item["id"] == customer_id), None)


class SnakeCaseSession(Session):
    def post(self, url, json, timeout):
        if url.endswith("/api/auth/signin"):
            return super().post(url, json, timeout)
        if "displayName" in json:
            self.writes.append(("rejected_camel", url, dict(json)))
            return Response({"error": "display_name is required"}, 422)
        return super().post(url, json, timeout)


class PatchUnsupportedSession(Session):
    def patch(self, url, json, timeout):
        self.writes.append(("rejected_patch", url, dict(json)))
        return Response({"error": "method not allowed"}, 405)


def test_ensure_customers_is_idempotent_and_reads_back():
    session = Session()
    client = BigCapitalClient("http://localhost:30005", session=session)
    customers = [{
        "display_name": "Horizon Media Group",
        "company_name": "Horizon Media Group",
        "email": "info@horizon.test",
    }]

    first = client.ensure_customers(customers)
    second = client.ensure_customers(customers)

    assert first["created"] == ["Horizon Media Group"]
    assert first["mismatches"] == []
    assert second["unchanged"] == ["Horizon Media Group"]
    assert len(session.writes) == 1
    assert session.writes[0][2]["displayName"] == "Horizon Media Group"
    assert session.headers["organization-id"] == "org"


def test_query_customers_reports_missing_exact_names():
    session = Session()
    session.customers = [{"id": 4, "displayName": "Existing"}]
    client = BigCapitalClient("http://localhost:30005", session=session)

    result = client.query_customers(["Existing", "Missing"])

    assert result["matched_count"] == 1
    assert result["missing_exact_names"] == ["Missing"]
    assert result["duplicate_exact_names"] == []


def test_ensure_customers_falls_back_to_legacy_snake_case_once():
    session = SnakeCaseSession()
    client = BigCapitalClient("http://localhost:30005", session=session)

    result = client.ensure_customers([{"display_name": "Legacy Customer"}])

    assert result["created"] == ["Legacy Customer"]
    assert [call[0] for call in session.writes] == ["rejected_camel", "post"]
    assert session.writes[-1][2]["display_name"] == "Legacy Customer"


def test_existing_name_only_does_not_apply_create_defaults():
    session = Session()
    session.customers = [{
        "id": 9,
        "displayName": "Existing Individual",
        "customerType": "individual",
        "active": False,
        "currencyCode": "EUR",
    }]
    client = BigCapitalClient("http://localhost:30005", session=session)

    result = client.ensure_customers([{"display_name": "Existing Individual"}])

    assert result["unchanged"] == ["Existing Individual"]
    assert session.writes == []
    assert session.customers[0]["customerType"] == "individual"
    assert session.customers[0]["active"] is False
    assert session.customers[0]["currencyCode"] == "EUR"


def test_explicit_update_uses_patch_and_preserves_other_fields():
    session = Session()
    session.customers = [{
        "id": 2,
        "displayName": "Acme",
        "email": "old@acme.test",
        "companyName": "Acme Holdings",
        "note": "preserve me",
        "currencyCode": "EUR",
    }]
    client = BigCapitalClient("http://localhost:30005", session=session)

    result = client.ensure_customers([{
        "display_name": "Acme",
        "email": "new@acme.test",
    }])

    assert result["updated"] == ["Acme"]
    assert [call[0] for call in session.writes] == ["patch"]
    assert session.writes[0][2] == {"email": "new@acme.test"}
    assert session.customers[0]["companyName"] == "Acme Holdings"
    assert session.customers[0]["note"] == "preserve me"
    assert session.customers[0]["currencyCode"] == "EUR"


def test_explicit_currency_is_updated_and_verified():
    session = Session()
    session.customers = [{
        "id": 3,
        "displayName": "Currency Customer",
        "currencyCode": "EUR",
    }]
    client = BigCapitalClient("http://localhost:30005", session=session)

    result = client.ensure_customers(
        [{"display_name": "Currency Customer"}], currency_code="usd"
    )

    assert result["updated"] == ["Currency Customer"]
    assert result["mismatches"] == []
    assert session.customers[0]["currencyCode"] == "USD"


def test_currency_is_not_changed_when_not_explicit():
    session = Session()
    session.customers = [{
        "id": 3,
        "displayName": "Currency Customer",
        "currencyCode": "EUR",
    }]
    client = BigCapitalClient("http://localhost:30005", session=session)

    result = client.ensure_customers([{"display_name": "Currency Customer"}])

    assert result["unchanged"] == ["Currency Customer"]
    assert session.writes == []
    assert session.customers[0]["currencyCode"] == "EUR"


def test_duplicate_existing_exact_names_fail_closed():
    session = Session()
    session.customers = [
        {"id": 1, "displayName": "Duplicate"},
        {"id": 2, "displayName": "Duplicate"},
    ]
    client = BigCapitalClient("http://localhost:30005", session=session)

    with pytest.raises(BigCapitalError, match="multiple existing customers"):
        client.ensure_customers([{"display_name": "Duplicate"}])
    assert session.writes == []


def test_exact_query_scans_later_pages():
    session = Session()
    session.customers = [
        {"id": index, "displayName": f"Customer {index:03d}"}
        for index in range(1, 251)
    ]
    client = BigCapitalClient("http://localhost:30005", session=session)

    result = client.query_customers(["Customer 250"])

    assert result["missing_exact_names"] == []
    assert result["customers"][0]["id"] == 250
    assert session.list_calls[:2] == [(1, 200), (2, 200)]


def test_put_fallback_merges_detail_instead_of_clearing_fields():
    session = PatchUnsupportedSession()
    session.customers = [{
        "id": 11,
        "displayName": "Merge Customer",
        "email": "old@example.test",
        "companyName": "Keep This Company",
        "note": "Keep this note",
        "active": False,
        "currencyCode": "GBP",
    }]
    client = BigCapitalClient("http://localhost:30005", session=session)

    result = client.ensure_customers([{
        "display_name": "Merge Customer",
        "email": "new@example.test",
    }])

    assert result["updated"] == ["Merge Customer"]
    assert [call[0] for call in session.writes] == ["rejected_patch", "put"]
    stored = session.customers[0]
    assert stored["email"] == "new@example.test"
    assert stored["companyName"] == "Keep This Company"
    assert stored["note"] == "Keep this note"
    assert stored["active"] is False
    assert stored["currencyCode"] == "GBP"


def test_non_boolean_active_is_rejected_before_authentication():
    session = Session()
    client = BigCapitalClient("http://localhost:30005", session=session)

    with pytest.raises(BigCapitalError, match="active must be a boolean"):
        client.ensure_customers([{"display_name": "Bad", "active": "yes"}])
    assert session.headers == {}
