from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from saas_agent.twenty_write_helper import TwentyWriteClient, TwentyWriteError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class MemoryReadClient:
    def __init__(self):
        self.records = {
            "companies": [],
            "people": [],
            "opportunities": [],
            "tasks": [],
            "notes": [],
        }
        self.targets = {"tasks": {}, "notes": {}}
        self.favorites = {}

    def query_by_business_key(self, entity, key, exact_values, limit=200):
        columns = {
            ("companies", "name"): "name",
            ("people", "email"): "emailsPrimaryEmail",
            ("opportunities", "name"): "name",
            ("tasks", "title"): "title",
            ("notes", "title"): "title",
        }
        column = columns[(entity, key)]
        rows = [row.copy() for row in self.records[entity] if row.get(column) in exact_values]
        return {"records": rows[:limit]}

    def query_activity_targets(self, entity, activity_id):
        return [row.copy() for row in self.targets[entity].get(activity_id, [])]

    def query_company_favorites(self, company_id):
        return [row.copy() for row in self.favorites.get(company_id, [])]


class MemorySession:
    def __init__(self, read_client, *, persist=True, fail_company=False):
        self.read = read_client
        self.persist = persist
        self.fail_company = fail_company
        self.headers = {}
        self.calls = []
        self.next_id = 1

    def _id(self):
        value = f"00000000-0000-4000-8000-{self.next_id:012d}"
        self.next_id += 1
        return value

    def post(self, url, json=None, timeout=None):
        self.calls.append(("post", url, json))
        path = urlparse(url).path
        if path == "/graphql":
            query = (json or {}).get("query", "")
            if "signIn" in query and "accessOrWorkspaceAgnosticToken" in query:
                return FakeResponse(payload={
                    "data": {"signIn": {"tokens": {
                        "accessOrWorkspaceAgnosticToken": {"token": "access-token"}
                    }}}
                })
            return FakeResponse(payload={"data": {}})
        if path == "/rest/companies" and self.fail_company:
            return FakeResponse(500, {"message": "company write failed"})
        plural = path.rsplit("/", 1)[-1]
        singular = {
            "companies": "Company",
            "people": "Person",
            "opportunities": "Opportunity",
            "tasks": "Task",
            "notes": "Note",
            "taskTargets": "TaskTarget",
            "noteTargets": "NoteTarget",
            "favorites": "Favorite",
        }[plural]
        record = {"id": self._id(), **(json or {})}
        if self.persist:
            self._persist(plural, record)
        return FakeResponse(201, {"data": {f"create{singular}": record}})

    def get(self, url, params=None, timeout=None):
        self.calls.append(("get", url, params))
        return FakeResponse(200, {"data": {"companies": []}})

    def patch(self, url, json=None, timeout=None):
        self.calls.append(("patch", url, json))
        parts = urlparse(url).path.strip("/").split("/")
        plural, record_id = parts[-2], parts[-1]
        singular = plural[:-3] + "y" if plural.endswith("ies") else plural[:-1]
        singular = singular[:1].upper() + singular[1:]
        rows = self.read.records[plural]
        row = next(item for item in rows if item["id"] == record_id)
        self._apply(row, plural, json or {})
        return FakeResponse(200, {"data": {f"update{singular}": row.copy()}})

    def _persist(self, plural, record):
        if plural in self.read.records:
            flat = {"id": record["id"]}
            self._apply(flat, plural, record)
            self.read.records[plural].append(flat)
        elif plural in {"taskTargets", "noteTargets"}:
            entity = "tasks" if plural == "taskTargets" else "notes"
            activity_id = record["taskId" if entity == "tasks" else "noteId"]
            self.read.targets[entity].setdefault(activity_id, []).append(record.copy())
        elif plural == "favorites":
            self.read.favorites.setdefault(record["companyId"], []).append(record.copy())

    @staticmethod
    def _apply(row, plural, payload):
        if "name" in payload and plural != "people":
            row["name"] = payload["name"]
        if plural == "companies":
            if "domainName" in payload:
                row["domainNamePrimaryLinkUrl"] = payload["domainName"].get("primaryLinkUrl")
            if "employees" in payload:
                row["employees"] = payload["employees"]
        elif plural == "people":
            if "name" in payload:
                row["nameFirstName"] = payload["name"].get("firstName", row.get("nameFirstName"))
                row["nameLastName"] = payload["name"].get("lastName", row.get("nameLastName"))
            if "emails" in payload:
                row["emailsPrimaryEmail"] = payload["emails"].get("primaryEmail")
            if "phones" in payload:
                row["phonesPrimaryPhoneNumber"] = payload["phones"].get("primaryPhoneNumber")
            for api, db in (("jobTitle", "jobTitle"), ("companyId", "companyId")):
                if api in payload:
                    row[db] = payload[api]
        elif plural == "opportunities":
            for field in ("companyId", "pointOfContactId", "stage", "closeDate"):
                if field in payload:
                    row[field] = payload[field]
            if "amount" in payload:
                row["amountAmountMicros"] = payload["amount"].get("amountMicros")
        elif plural == "tasks":
            for field in ("title", "status", "dueAt"):
                if field in payload:
                    row[field] = payload[field]
            if "bodyV2" in payload:
                row["bodyV2Markdown"] = payload["bodyV2"].get("markdown")
        elif plural == "notes":
            if "title" in payload:
                row["title"] = payload["title"]
            if "bodyV2" in payload:
                row["bodyV2Markdown"] = payload["bodyV2"].get("markdown")


def _client(*, persist=True, fail_company=False):
    read = MemoryReadClient()
    session = MemorySession(read, persist=persist, fail_company=fail_company)
    return TwentyWriteClient(
        "http://localhost:3000",
        "jony.ive@apple.dev",
        "tim@apple.dev",
        read,
        session=session,
    ), read, session


def _bundle():
    return {
        "companies": [{
            "name": "Acme",
            "domain_name": "acme.example",
            "employees": 12,
            "favorite": True,
        }],
        "people": [{
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@acme.example",
            "job_title": "Engineer",
            "phone": "+1-555-0100",
            "company_name": "Acme",
        }],
        "opportunities": [{
            "name": "Acme Renewal",
            "amount": 1250,
            "stage": "proposal",
            "close_date": "2027-01-31",
            "company_name": "Acme",
            "point_of_contact_email": "ada@acme.example",
        }],
        "tasks": [{
            "title": "Send renewal",
            "status": "todo",
            "due_at": "2027-01-15",
            "body": "Send the renewal proposal.",
            "company_name": "Acme",
        }],
        "notes": [{
            "title": "Renewal context",
            "body": "Customer requested annual billing.",
            "company_name": "Acme",
        }],
    }


def test_full_bundle_is_idempotent_and_relationships_use_resolved_ids():
    client, read, session = _client()

    first = client.ensure_records(**_bundle())
    second = client.ensure_records(**_bundle())

    assert first["mismatches"] == []
    assert all(first["created"][entity] for entity in _bundle())
    assert all(second["created"][entity] == [] for entity in _bundle())
    assert all(second["unchanged"][entity] for entity in _bundle())
    assert len(read.records["companies"]) == 1
    assert len(read.records["people"]) == 1
    assert len(read.records["opportunities"]) == 1
    assert len(read.records["tasks"]) == 1
    assert len(read.records["notes"]) == 1
    person_payload = next(
        call[2] for call in session.calls
        if call[0] == "post" and urlparse(call[1]).path == "/rest/people"
    )
    assert person_payload["companyId"] == read.records["companies"][0]["id"]
    assert "id" not in _bundle()["people"][0]


def test_unknown_uuid_fields_are_rejected_before_any_http_request():
    client, _, session = _client()

    with pytest.raises(TwentyWriteError, match="unsupported companies fields: id"):
        client.ensure_records(companies=[{"id": "user-picked-id", "name": "Acme"}])

    assert session.calls == []


def test_api_success_without_database_visibility_returns_mismatch():
    client, _, _ = _client(persist=False)

    result = client.ensure_records(companies=[{"name": "Missing Readback"}])

    assert result["created"]["companies"] == ["Missing Readback"]
    assert result["mismatches"] == [{
        "entity": "companies",
        "key": "Missing Readback",
        "fields": {"record_count": {"expected": 1, "actual": 0}},
    }]


def test_failed_parent_blocks_dependent_person():
    client, _, _ = _client(fail_company=True)

    result = client.ensure_records(
        companies=[{"name": "Broken Parent"}],
        people=[{
            "first_name": "Pat",
            "last_name": "Lee",
            "email": "pat@broken.example",
            "company_name": "Broken Parent",
        }],
    )

    assert any(item["entity"] == "companies" for item in result["blocked"])
    person_block = next(item for item in result["blocked"] if item["entity"] == "people")
    assert person_block["blocked_by"] == "company:Broken Parent"
