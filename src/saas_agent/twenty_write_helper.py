"""Deterministic Twenty writes through the application API with SQL readback."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover - minimal environments inject a session.
    requests = None

from saas_agent.twenty_helper import TwentyClient, TwentyError


class TwentyWriteError(RuntimeError):
    """Raised when a Twenty write cannot be planned or executed safely."""


_ENTITY_LIMIT = 50
_TOTAL_LIMIT = 100
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_ALLOWED_FIELDS = {
    "companies": {"name", "domain_name", "employees", "favorite"},
    "people": {
        "first_name", "last_name", "email", "job_title", "phone", "company_name",
    },
    "opportunities": {
        "name", "amount", "stage", "close_date", "company_name",
        "point_of_contact_email",
    },
    "tasks": {"title", "status", "due_at", "body", "company_name"},
    "notes": {"title", "body", "company_name"},
}


class TwentyWriteClient:
    """Ensure a bounded CRM object graph through Twenty's authenticated REST API."""

    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        read_client: TwentyClient | None = None,
        *,
        session: Any | None = None,
        timeout: int = 30,
    ) -> None:
        self.base_url = _required_text(base_url, "base_url").rstrip("/")
        self.email = _required_text(email, "email")
        self.password = _required_text(password, "password")
        if session is None:
            if requests is None:
                raise TwentyWriteError("requests is required when no session is supplied")
            session = requests.Session()
        self.session = session
        self.timeout = int(timeout)
        self.read_client = read_client
        self._authenticated = False
        self._auth_variant: str | None = None
        self._enum_values: dict[str, set[str]] = {}
        self._protocol_events: list[dict[str, Any]] = []

    def preflight(self) -> dict[str, Any]:
        """Confirm login and generated REST CRUD without exposing credentials."""

        self._authenticate()
        confirmed_endpoints = []
        for endpoint in (
            "/rest/companies", "/rest/people", "/rest/opportunities",
            "/rest/tasks", "/rest/notes",
        ):
            payload = self._rest("get", endpoint, params={"limit": 1, "depth": 0})
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                raise TwentyWriteError(
                    f"Twenty REST preflight returned an unexpected shape for {endpoint}"
                )
            confirmed_endpoints.append(endpoint)
        self._enum_values = {
            "opportunity.stage": self._discover_enum_values(
                "createOpportunity", "stage"
            ),
            "task.status": self._discover_enum_values("createTask", "status"),
        }
        return {
            "auth_variant": self._auth_variant,
            "rest_base": "/rest",
            "confirmed_endpoints": confirmed_endpoints,
            "enum_values": {
                key: sorted(values) for key, values in self._enum_values.items() if values
            },
            "protocol_events": list(self._protocol_events),
        }

    def ensure_records(
        self,
        companies: list[dict[str, Any]] | None = None,
        people: list[dict[str, Any]] | None = None,
        opportunities: list[dict[str, Any]] | None = None,
        tasks: list[dict[str, Any]] | None = None,
        notes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Validate, upsert, relate, and read back one bounded object graph."""

        if self.read_client is None:
            raise TwentyWriteError("read_client is required for verified writes")
        bundle = {
            "companies": self._normalize_companies(companies),
            "people": self._normalize_people(people),
            "opportunities": self._normalize_opportunities(opportunities),
            "tasks": self._normalize_tasks(tasks),
            "notes": self._normalize_notes(notes),
        }
        total = sum(len(records) for records in bundle.values())
        if total < 1:
            raise TwentyWriteError("at least one Twenty record is required")
        if total > _TOTAL_LIMIT:
            raise TwentyWriteError(f"record bundle exceeds total limit {_TOTAL_LIMIT}")

        contract = self.preflight()
        self._validate_enum_values(bundle)
        state, duplicates = self._load_existing(bundle)
        result: dict[str, Any] = {
            "created": {entity: [] for entity in bundle},
            "updated": {entity: [] for entity in bundle},
            "unchanged": {entity: [] for entity in bundle},
            "blocked": [],
            "duplicates": duplicates,
            "mismatches": [],
            "readback": {},
            "contract": contract,
        }
        if duplicates:
            result["blocked"].append({
                "entity": "bundle",
                "key": "preflight",
                "reason": "ambiguous existing business keys",
            })
            return result

        company_ids = {
            key: rows[0]["id"]
            for key, rows in state["companies"].items() if len(rows) == 1
        }
        person_ids = {
            key: rows[0]["id"]
            for key, rows in state["people"].items() if len(rows) == 1
        }

        for spec in bundle["companies"]:
            key = spec["name"]
            try:
                record, disposition = self._ensure_company(
                    spec, (state["companies"].get(key) or [None])[0]
                )
                if record.get("id"):
                    company_ids[key] = record["id"]
                result[disposition]["companies"].append(key)
            except Exception as exc:
                _block(result, "companies", key, exc)
                company_ids.pop(key, None)

        for spec in bundle["people"]:
            key = spec["email"]
            company_id = self._resolve_dependency(
                result, "people", key, "company", spec.get("company_name"), company_ids
            )
            if spec.get("company_name") and company_id is None:
                continue
            try:
                record, disposition = self._ensure_person(
                    spec, (state["people"].get(key) or [None])[0], company_id
                )
                if record.get("id"):
                    person_ids[key] = record["id"]
                result[disposition]["people"].append(key)
            except Exception as exc:
                _block(result, "people", key, exc)
                person_ids.pop(key, None)

        for spec in bundle["opportunities"]:
            key = self._opportunity_key(spec)
            company_id = self._resolve_dependency(
                result, "opportunities", key, "company", spec["company_name"], company_ids
            )
            if company_id is None:
                continue
            person_id = self._resolve_dependency(
                result,
                "opportunities",
                key,
                "person",
                spec.get("point_of_contact_email"),
                person_ids,
            )
            if spec.get("point_of_contact_email") and person_id is None:
                continue
            try:
                record, disposition = self._ensure_opportunity(
                    spec, (state["opportunities"].get(key) or [None])[0],
                    company_id, person_id,
                )
                result[disposition]["opportunities"].append(key)
            except Exception as exc:
                _block(result, "opportunities", key, exc)

        for entity in ("tasks", "notes"):
            for spec in bundle[entity]:
                key = self._task_key(spec) if entity == "tasks" else spec["title"]
                company_id = self._resolve_dependency(
                    result, entity, key, "company", spec.get("company_name"), company_ids
                )
                if spec.get("company_name") and company_id is None:
                    continue
                try:
                    current = (state[entity].get(key) or [None])[0]
                    if entity == "tasks":
                        _, disposition = self._ensure_task(spec, current, company_id)
                    else:
                        _, disposition = self._ensure_note(spec, current, company_id)
                    result[disposition][entity].append(key)
                except Exception as exc:
                    _block(result, entity, key, exc)

        result["readback"], result["mismatches"] = self._readback(bundle)
        result["protocol_events"] = list(self._protocol_events)
        return result

    def _normalize_companies(
        self, records: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        normalized = self._records("companies", records)
        for spec in normalized:
            spec["name"] = _required_text(spec.get("name"), "company name")
            if "domain_name" in spec:
                domain = _required_text(spec["domain_name"], "company domain_name")
                spec["domain_name"] = domain
            if "employees" in spec:
                spec["employees"] = _bounded_int(
                    spec["employees"], "company employees", minimum=0, maximum=10**9
                )
            if "favorite" in spec and not isinstance(spec["favorite"], bool):
                raise TwentyWriteError("company favorite must be true or false")
        _unique(normalized, lambda item: item["name"], "company names")
        return normalized

    def _normalize_people(
        self, records: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        normalized = self._records("people", records)
        for spec in normalized:
            spec["email"] = _email(spec.get("email"), "person email")
            for field in ("first_name", "last_name", "job_title", "phone", "company_name"):
                if field in spec:
                    spec[field] = _required_text(spec[field], f"person {field}")
        _unique(normalized, lambda item: item["email"], "person emails")
        return normalized

    def _normalize_opportunities(
        self, records: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        normalized = self._records("opportunities", records)
        for spec in normalized:
            spec["name"] = _required_text(spec.get("name"), "opportunity name")
            spec["company_name"] = _required_text(
                spec.get("company_name"), "opportunity company_name"
            )
            if "point_of_contact_email" in spec:
                spec["point_of_contact_email"] = _email(
                    spec["point_of_contact_email"], "opportunity point_of_contact_email"
                )
            if "amount" in spec:
                spec["amount_micros"] = _amount_micros(spec.pop("amount"))
            if "stage" in spec:
                spec["stage"] = _enum(spec["stage"], "opportunity stage")
            if "close_date" in spec:
                spec["close_date"] = _date(spec["close_date"], "opportunity close_date")
        _unique(normalized, self._opportunity_key, "opportunity name/company keys")
        return normalized

    def _normalize_tasks(
        self, records: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        normalized = self._records("tasks", records)
        for spec in normalized:
            spec["title"] = _required_text(spec.get("title"), "task title")
            spec["due_at"] = _date(spec.get("due_at"), "task due_at")
            for field in ("body", "company_name"):
                if field in spec:
                    spec[field] = _required_text(spec[field], f"task {field}")
            if "status" in spec:
                spec["status"] = _enum(spec["status"], "task status")
        _unique(normalized, self._task_key, "task title/due/company keys")
        return normalized

    def _normalize_notes(
        self, records: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        normalized = self._records("notes", records)
        for spec in normalized:
            spec["title"] = _required_text(spec.get("title"), "note title")
            for field in ("body", "company_name"):
                if field in spec:
                    spec[field] = _required_text(spec[field], f"note {field}")
        _unique(normalized, lambda item: item["title"], "note titles")
        return normalized

    def _records(
        self, entity: str, records: list[dict[str, Any]] | None
    ) -> list[dict[str, Any]]:
        if records is None:
            return []
        if not isinstance(records, list):
            raise TwentyWriteError(f"{entity} must be a list")
        if len(records) > _ENTITY_LIMIT:
            raise TwentyWriteError(f"{entity} exceeds per-entity limit {_ENTITY_LIMIT}")
        normalized = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise TwentyWriteError(f"{entity}[{index}] must be an object")
            unknown = set(record) - _ALLOWED_FIELDS[entity]
            if unknown:
                raise TwentyWriteError(
                    f"unsupported {entity} fields: {', '.join(sorted(unknown))}"
                )
            normalized.append(dict(record))
        return normalized

    def _validate_enum_values(self, bundle: dict[str, list[dict[str, Any]]]) -> None:
        checks = (
            ("opportunity.stage", bundle["opportunities"], "stage"),
            ("task.status", bundle["tasks"], "status"),
        )
        for enum_name, records, field in checks:
            allowed = self._enum_values.get(enum_name) or set()
            if not allowed:
                continue
            invalid = sorted({item[field] for item in records if field in item} - allowed)
            if invalid:
                raise TwentyWriteError(
                    f"unsupported {enum_name} values {invalid}; available={sorted(allowed)}"
                )

    def _load_existing(
        self, bundle: dict[str, list[dict[str, Any]]]
    ) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], list[dict[str, Any]]]:
        assert self.read_client is not None
        state: dict[str, dict[str, list[dict[str, Any]]]] = {
            entity: {} for entity in bundle
        }
        duplicates: list[dict[str, Any]] = []

        company_names = _values(bundle["companies"], "name") | {
            item["company_name"] for entity in ("people", "opportunities", "tasks", "notes")
            for item in bundle[entity] if item.get("company_name")
        }
        company_rows = self._query("companies", "name", company_names)
        company_by_name = _group(company_rows, lambda row: str(row.get("name") or ""))
        state["companies"] = company_by_name
        company_ids = {
            name: rows[0].get("id") for name, rows in company_by_name.items() if len(rows) == 1
        }

        person_emails = _values(bundle["people"], "email") | {
            item["point_of_contact_email"] for item in bundle["opportunities"]
            if item.get("point_of_contact_email")
        }
        person_rows = self._query("people", "email", person_emails)
        state["people"] = _group(
            person_rows, lambda row: str(row.get("emailsPrimaryEmail") or "").casefold()
        )

        opportunity_rows = self._query(
            "opportunities", "name", _values(bundle["opportunities"], "name")
        )
        for spec in bundle["opportunities"]:
            key = self._opportunity_key(spec)
            company_id = company_ids.get(spec["company_name"])
            state["opportunities"][key] = [
                row for row in opportunity_rows
                if row.get("name") == spec["name"]
                and company_id is not None and row.get("companyId") == company_id
            ]

        task_rows = self._query("tasks", "title", _values(bundle["tasks"], "title"))
        for spec in bundle["tasks"]:
            key = self._task_key(spec)
            company_id = company_ids.get(spec.get("company_name"))
            matches = []
            for row in task_rows:
                if row.get("title") != spec["title"] or not _same_date(
                    row.get("dueAt"), spec["due_at"]
                ):
                    continue
                targets = self.read_client.query_activity_targets("tasks", row["id"])
                linked_ids = {target.get("targetCompanyId") for target in targets}
                if (company_id in linked_ids) if company_id else not any(linked_ids):
                    matches.append(row)
            state["tasks"][key] = matches

        note_rows = self._query("notes", "title", _values(bundle["notes"], "title"))
        state["notes"] = _group(note_rows, lambda row: str(row.get("title") or ""))

        requested_keys = {
            "companies": company_names,
            "people": person_emails,
            "opportunities": {self._opportunity_key(item) for item in bundle["opportunities"]},
            "tasks": {self._task_key(item) for item in bundle["tasks"]},
            "notes": _values(bundle["notes"], "title"),
        }
        for entity, keys in requested_keys.items():
            for key in sorted(keys):
                count = len(state[entity].get(key, []))
                if count > 1:
                    duplicates.append({"entity": entity, "key": key, "count": count})
        return state, duplicates

    def _query(self, entity: str, key: str, values: set[str]) -> list[dict[str, Any]]:
        if not values:
            return []
        assert self.read_client is not None
        result = self.read_client.query_by_business_key(
            entity, key, sorted(values), limit=min(1000, max(200, len(values) * 4))
        )
        return list(result["records"])

    def _ensure_company(
        self, spec: dict[str, Any], current: dict[str, Any] | None
    ) -> tuple[dict[str, Any], str]:
        payload = {"name": spec["name"]}
        if "domain_name" in spec:
            payload["domainName"] = _domain_payload(spec["domain_name"])
        if "employees" in spec:
            payload["employees"] = spec["employees"]
        record, disposition = self._upsert(
            "companies", "Company", current, payload,
            self._company_changes(spec, current) if current else payload,
        )
        if spec.get("favorite"):
            favorite_created = self._ensure_favorite(
                record.get("id") or (current or {}).get("id")
            )
            if favorite_created and disposition == "unchanged":
                disposition = "updated"
        return record, disposition

    def _company_changes(
        self, spec: dict[str, Any], current: dict[str, Any] | None
    ) -> dict[str, Any]:
        if current is None:
            return {}
        changes = {}
        if "domain_name" in spec and not _same_domain(
            current.get("domainNamePrimaryLinkUrl"), spec["domain_name"]
        ):
            changes["domainName"] = _domain_payload(spec["domain_name"])
        if "employees" in spec and _as_int(current.get("employees")) != spec["employees"]:
            changes["employees"] = spec["employees"]
        return changes

    def _ensure_favorite(self, company_id: Any) -> bool:
        company_id = _required_id(company_id, "favorite company")
        assert self.read_client is not None
        if self.read_client.query_company_favorites(company_id):
            return False
        self._create("favorites", "Favorite", {"companyId": company_id})
        return True

    def _ensure_person(
        self,
        spec: dict[str, Any],
        current: dict[str, Any] | None,
        company_id: str | None,
    ) -> tuple[dict[str, Any], str]:
        payload: dict[str, Any] = {"emails": {"primaryEmail": spec["email"]}}
        name = {
            key: spec[source] for key, source in (
                ("firstName", "first_name"), ("lastName", "last_name")
            ) if source in spec
        }
        if name:
            payload["name"] = name
        if "job_title" in spec:
            payload["jobTitle"] = spec["job_title"]
        if "phone" in spec:
            payload["phones"] = {"primaryPhoneNumber": spec["phone"]}
        if company_id:
            payload["companyId"] = company_id
        changes = payload if current is None else self._person_changes(spec, current, company_id)
        return self._upsert("people", "Person", current, payload, changes)

    def _person_changes(
        self, spec: dict[str, Any], current: dict[str, Any], company_id: str | None
    ) -> dict[str, Any]:
        changes = {}
        name = {}
        if "first_name" in spec and current.get("nameFirstName") != spec["first_name"]:
            name["firstName"] = spec["first_name"]
        if "last_name" in spec and current.get("nameLastName") != spec["last_name"]:
            name["lastName"] = spec["last_name"]
        if name:
            name = {
                "firstName": name.get("firstName", current.get("nameFirstName")),
                "lastName": name.get("lastName", current.get("nameLastName")),
            }
            changes["name"] = name
        if str(current.get("emailsPrimaryEmail") or "").casefold() != spec["email"]:
            changes["emails"] = {"primaryEmail": spec["email"]}
        if "job_title" in spec and current.get("jobTitle") != spec["job_title"]:
            changes["jobTitle"] = spec["job_title"]
        if "phone" in spec and current.get("phonesPrimaryPhoneNumber") != spec["phone"]:
            changes["phones"] = {
                "primaryPhoneNumber": spec["phone"],
                "primaryPhoneCountryCode": current.get("phonesPrimaryPhoneCountryCode"),
                "primaryPhoneCallingCode": current.get("phonesPrimaryPhoneCallingCode"),
            }
            changes["phones"] = {
                key: value for key, value in changes["phones"].items()
                if value is not None
            }
        if company_id and current.get("companyId") != company_id:
            changes["companyId"] = company_id
        return changes

    def _ensure_opportunity(
        self,
        spec: dict[str, Any],
        current: dict[str, Any] | None,
        company_id: str,
        person_id: str | None,
    ) -> tuple[dict[str, Any], str]:
        payload: dict[str, Any] = {"name": spec["name"], "companyId": company_id}
        if "amount_micros" in spec:
            payload["amount"] = {
                "amountMicros": spec["amount_micros"], "currencyCode": "USD"
            }
        if "stage" in spec:
            payload["stage"] = spec["stage"]
        if "close_date" in spec:
            payload["closeDate"] = spec["close_date"]
        if person_id:
            payload["pointOfContactId"] = person_id
        changes = payload if current is None else self._opportunity_changes(
            spec, current, company_id, person_id
        )
        return self._upsert("opportunities", "Opportunity", current, payload, changes)

    def _opportunity_changes(
        self,
        spec: dict[str, Any],
        current: dict[str, Any],
        company_id: str,
        person_id: str | None,
    ) -> dict[str, Any]:
        changes = {}
        if current.get("companyId") != company_id:
            changes["companyId"] = company_id
        if person_id and current.get("pointOfContactId") != person_id:
            changes["pointOfContactId"] = person_id
        if "amount_micros" in spec and _as_int(
            current.get("amountAmountMicros")
        ) != spec["amount_micros"]:
            changes["amount"] = {
                "amountMicros": spec["amount_micros"], "currencyCode": "USD"
            }
        if "stage" in spec and str(current.get("stage") or "") != spec["stage"]:
            changes["stage"] = spec["stage"]
        if "close_date" in spec and not _same_date(
            current.get("closeDate"), spec["close_date"]
        ):
            changes["closeDate"] = spec["close_date"]
        return changes

    def _ensure_task(
        self, spec: dict[str, Any], current: dict[str, Any] | None, company_id: str | None
    ) -> tuple[dict[str, Any], str]:
        payload: dict[str, Any] = {
            "title": spec["title"],
            "dueAt": f"{spec['due_at']}T00:00:00.000Z",
        }
        if "status" in spec:
            payload["status"] = spec["status"]
        if "body" in spec:
            payload["bodyV2"] = {"markdown": spec["body"]}
        changes = payload if current is None else self._task_changes(spec, current)
        record, disposition = self._upsert("tasks", "Task", current, payload, changes)
        if company_id:
            target_created = self._ensure_activity_target("task", record["id"], company_id)
            if target_created and disposition == "unchanged":
                disposition = "updated"
        return record, disposition

    def _task_changes(
        self, spec: dict[str, Any], current: dict[str, Any]
    ) -> dict[str, Any]:
        changes = {}
        if not _same_date(current.get("dueAt"), spec["due_at"]):
            changes["dueAt"] = f"{spec['due_at']}T00:00:00.000Z"
        if "status" in spec and current.get("status") != spec["status"]:
            changes["status"] = spec["status"]
        if "body" in spec and current.get("bodyV2Markdown") != spec["body"]:
            changes["bodyV2"] = {"markdown": spec["body"]}
        return changes

    def _ensure_note(
        self, spec: dict[str, Any], current: dict[str, Any] | None, company_id: str | None
    ) -> tuple[dict[str, Any], str]:
        payload: dict[str, Any] = {"title": spec["title"]}
        if "body" in spec:
            payload["bodyV2"] = {"markdown": spec["body"]}
        changes = payload if current is None else (
            {"bodyV2": {"markdown": spec["body"]}}
            if "body" in spec and current.get("bodyV2Markdown") != spec["body"] else {}
        )
        record, disposition = self._upsert("notes", "Note", current, payload, changes)
        if company_id:
            target_created = self._ensure_activity_target("note", record["id"], company_id)
            if target_created and disposition == "unchanged":
                disposition = "updated"
        return record, disposition

    def _ensure_activity_target(
        self, activity: str, activity_id: str, company_id: str
    ) -> bool:
        assert self.read_client is not None
        entity = "tasks" if activity == "task" else "notes"
        targets = self.read_client.query_activity_targets(entity, activity_id)
        if any(target.get("targetCompanyId") == company_id for target in targets):
            return False
        self._create(
            f"{activity}Targets",
            f"{activity.title()}Target",
            {f"{activity}Id": activity_id, "targetCompanyId": company_id},
        )
        return True

    def _upsert(
        self,
        plural: str,
        singular: str,
        current: dict[str, Any] | None,
        create_payload: dict[str, Any],
        changes: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        if current is None:
            return self._create(plural, singular, create_payload), "created"
        if not changes:
            return current, "unchanged"
        record_id = _required_id(current.get("id"), f"existing {singular}")
        payload = self._rest("patch", f"/rest/{plural}/{record_id}", json_body=changes)
        return _extract_record(payload, f"update{singular}"), "updated"

    def _create(
        self, plural: str, singular: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._rest("post", f"/rest/{plural}", json_body=payload)
        return _extract_record(response, f"create{singular}")

    def _readback(
        self, bundle: dict[str, list[dict[str, Any]]]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        state, duplicates = self._load_existing(bundle)
        readback: dict[str, Any] = {}
        mismatches: list[dict[str, Any]] = [
            {**item, "fields": {"business_key": "duplicate"}} for item in duplicates
        ]
        for entity, records in bundle.items():
            readback[entity] = {}
            for spec in records:
                key = (
                    spec["name"] if entity == "companies" else
                    spec["email"] if entity == "people" else
                    self._opportunity_key(spec) if entity == "opportunities" else
                    self._task_key(spec) if entity == "tasks" else spec["title"]
                )
                rows = state[entity].get(key, [])
                readback[entity][key] = rows
                if len(rows) != 1:
                    mismatches.append({
                        "entity": entity, "key": key,
                        "fields": {"record_count": {"expected": 1, "actual": len(rows)}},
                    })
                    continue
                wrong = self._field_mismatches(entity, spec, rows[0], state)
                if wrong:
                    mismatches.append({"entity": entity, "key": key, "fields": wrong})
        return readback, mismatches

    def _field_mismatches(
        self,
        entity: str,
        spec: dict[str, Any],
        row: dict[str, Any],
        state: dict[str, dict[str, list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        wrong: dict[str, Any] = {}

        def expect(field: str, actual: Any, expected: Any) -> None:
            if actual != expected:
                wrong[field] = {"expected": expected, "actual": actual}

        if entity == "companies":
            if "domain_name" in spec and not _same_domain(
                row.get("domainNamePrimaryLinkUrl"), spec["domain_name"]
            ):
                expect("domain_name", row.get("domainNamePrimaryLinkUrl"), spec["domain_name"])
            if "employees" in spec:
                expect("employees", _as_int(row.get("employees")), spec["employees"])
            if spec.get("favorite"):
                assert self.read_client is not None
                if not self.read_client.query_company_favorites(row["id"]):
                    expect("favorite", False, True)
        elif entity == "people":
            for source, column in (
                ("first_name", "nameFirstName"), ("last_name", "nameLastName"),
                ("job_title", "jobTitle"), ("phone", "phonesPrimaryPhoneNumber"),
            ):
                if source in spec:
                    expect(source, row.get(column), spec[source])
            if spec.get("company_name"):
                company = state["companies"].get(spec["company_name"], [])
                expect("company_name", row.get("companyId"), company[0].get("id") if len(company) == 1 else None)
        elif entity == "opportunities":
            if "amount_micros" in spec:
                expect("amount", _as_int(row.get("amountAmountMicros")), spec["amount_micros"])
            if "stage" in spec:
                expect("stage", row.get("stage"), spec["stage"])
            if "close_date" in spec and not _same_date(row.get("closeDate"), spec["close_date"]):
                expect("close_date", str(row.get("closeDate") or "")[:10], spec["close_date"])
            company = state["companies"].get(spec["company_name"], [])
            expect(
                "company_name",
                row.get("companyId"),
                company[0].get("id") if len(company) == 1 else None,
            )
            if spec.get("point_of_contact_email"):
                people = state["people"].get(spec["point_of_contact_email"], [])
                expect(
                    "point_of_contact_email",
                    row.get("pointOfContactId"),
                    people[0].get("id") if len(people) == 1 else None,
                )
        elif entity == "tasks":
            if "body" in spec:
                expect("body", row.get("bodyV2Markdown"), spec["body"])
            if "status" in spec:
                expect("status", row.get("status"), spec["status"])
        elif entity == "notes":
            if "body" in spec:
                expect("body", row.get("bodyV2Markdown"), spec["body"])
            if spec.get("company_name"):
                company = state["companies"].get(spec["company_name"], [])
                expected_company_id = (
                    company[0].get("id") if len(company) == 1 else None
                )
                assert self.read_client is not None
                target_ids = {
                    target.get("targetCompanyId")
                    for target in self.read_client.query_activity_targets("notes", row["id"])
                }
                if expected_company_id not in target_ids:
                    expect("company_name", sorted(value for value in target_ids if value), expected_company_id)
        return wrong

    def _resolve_dependency(
        self,
        result: dict[str, Any],
        entity: str,
        key: str,
        dependency_type: str,
        dependency_key: str | None,
        ids: dict[str, str],
    ) -> str | None:
        if not dependency_key:
            return None
        value = ids.get(dependency_key)
        if value:
            return value
        result["blocked"].append({
            "entity": entity,
            "key": key,
            "blocked_by": f"{dependency_type}:{dependency_key}",
            "reason": "required relation could not be resolved uniquely",
        })
        return None

    def _opportunity_key(self, spec: dict[str, Any]) -> str:
        return f"{spec['name']} | company={spec['company_name']}"

    def _task_key(self, spec: dict[str, Any]) -> str:
        return (
            f"{spec['title']} | due={spec['due_at']} | "
            f"company={spec.get('company_name') or '<none>'}"
        )

    def _authenticate(self) -> None:
        if self._authenticated:
            return
        direct_queries = (
            (
                "signIn_access_or_workspace",
                "mutation SignIn($email:String!,$password:String!){"
                "signIn(email:$email,password:$password){tokens{"
                "accessOrWorkspaceAgnosticToken{token}}}}",
            ),
            (
                "signIn_access_token",
                "mutation SignIn($email:String!,$password:String!){"
                "signIn(email:$email,password:$password){tokens{accessToken{token}}}}",
            ),
        )
        variables = {"email": self.email, "password": self.password}
        errors = []
        for variant, query in direct_queries:
            payload = self._graphql(query, variables, allow_errors=True)
            token = _find_token(payload)
            if token:
                self._set_token(token, variant)
                return
            errors.append(_graphql_error_summary(payload))

        login_query = (
            "mutation Login($email:String!,$password:String!,$origin:String!){"
            "getLoginTokenFromCredentials(email:$email,password:$password,origin:$origin){"
            "loginToken{token}}}"
        )
        login_payload = self._graphql(
            login_query, {**variables, "origin": self.base_url}, allow_errors=True
        )
        login_token = _find_token(login_payload)
        if login_token:
            for field in ("accessOrWorkspaceAgnosticToken", "accessToken"):
                access_query = (
                    "mutation Access($loginToken:String!,$origin:String!){"
                    "getAuthTokensFromLoginToken(loginToken:$loginToken,origin:$origin){"
                    "tokens{" + field + "{token}}}}"
                )
                payload = self._graphql(
                    access_query,
                    {"loginToken": login_token, "origin": self.base_url},
                    allow_errors=True,
                )
                token = _find_token(payload, exclude=login_token)
                if token:
                    self._set_token(token, f"two_step_{field}")
                    return
                errors.append(_graphql_error_summary(payload))
        else:
            errors.append(_graphql_error_summary(login_payload))
        detail = " | ".join(filter(None, errors))[:700]
        detail = detail.replace(self.email, "<email>").replace(
            self.password, "<redacted>"
        )
        raise TwentyWriteError(
            "Twenty authentication contract was not recognized: " + detail
        )

    def _set_token(self, token: str, variant: str) -> None:
        if not hasattr(self.session, "headers"):
            raise TwentyWriteError("Twenty session does not expose mutable headers")
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self._authenticated = True
        self._auth_variant = variant

    def _discover_enum_values(self, mutation_name: str, field_name: str) -> set[str]:
        try:
            schema_query = """
query MutationInputs {
  __schema {
    mutationType {
      fields {
        name
        args { name type { kind name ofType { kind name ofType { kind name } } } }
      }
    }
  }
}
"""
            payload = self._graphql(schema_query, {}, allow_errors=True)
            fields = (((payload.get("data") or {}).get("__schema") or {}).get(
                "mutationType"
            ) or {}).get("fields") or []
            mutation = next(item for item in fields if item.get("name") == mutation_name)
            data_arg = next(item for item in mutation.get("args") or [] if item.get("name") in {"data", "input"})
            input_type = _base_type_name(data_arg.get("type") or {})
            if not input_type:
                return set()
            input_payload = self._graphql(
                "query Input($name:String!){__type(name:$name){inputFields{"
                "name type{kind name ofType{kind name ofType{kind name}}}}}}",
                {"name": input_type},
                allow_errors=True,
            )
            input_fields = (((input_payload.get("data") or {}).get("__type") or {}).get(
                "inputFields"
            ) or [])
            field = next(item for item in input_fields if item.get("name") == field_name)
            enum_type = _base_type_name(field.get("type") or {})
            enum_payload = self._graphql(
                "query Enum($name:String!){__type(name:$name){enumValues{name}}}",
                {"name": enum_type},
                allow_errors=True,
            )
            values = (((enum_payload.get("data") or {}).get("__type") or {}).get(
                "enumValues"
            ) or [])
            return {str(item.get("name")) for item in values if item.get("name")}
        except (StopIteration, TypeError, AttributeError):
            return set()

    def _graphql(
        self, query: str, variables: dict[str, Any], *, allow_errors: bool
    ) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/graphql",
            json={"query": query, "variables": variables},
            timeout=self.timeout,
        )
        self._protocol_events.append({
            "method": "POST",
            "endpoint": "/graphql",
            "status": getattr(response, "status_code", None),
        })
        if getattr(response, "status_code", 500) >= 400:
            if allow_errors:
                return {"errors": [{"message": f"HTTP {response.status_code}"}]}
            self._raise_http(response, "Twenty GraphQL")
        try:
            payload = response.json()
        except Exception as exc:
            raise TwentyWriteError("Twenty GraphQL returned non-JSON data") from exc
        if not isinstance(payload, dict):
            raise TwentyWriteError("Twenty GraphQL returned an invalid JSON shape")
        if payload.get("errors") and not allow_errors:
            raise TwentyWriteError("Twenty GraphQL error: " + _graphql_error_summary(payload))
        return payload

    def _rest(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._authenticate()
        request = getattr(self.session, method)
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if params is not None:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        response = request(f"{self.base_url}{path}", **kwargs)
        self._protocol_events.append({
            "method": method.upper(),
            "endpoint": _redacted_endpoint(path),
            "status": getattr(response, "status_code", None),
            "request_fields": sorted(json_body) if json_body is not None else [],
        })
        if getattr(response, "status_code", 500) >= 400:
            self._raise_http(response, f"Twenty {method.upper()} {path}")
        try:
            payload = response.json()
        except Exception as exc:
            raise TwentyWriteError(f"Twenty {path} returned non-JSON data") from exc
        if not isinstance(payload, dict):
            raise TwentyWriteError(f"Twenty {path} returned an invalid JSON shape")
        return payload

    def _raise_http(self, response: Any, label: str) -> None:
        try:
            body = json.dumps(response.json(), ensure_ascii=True, sort_keys=True)
        except Exception:
            body = str(getattr(response, "text", ""))
        body = body.replace(self.email, "<email>").replace(self.password, "<redacted>")
        raise TwentyWriteError(
            f"{label} failed with HTTP {getattr(response, 'status_code', '?')}: {body[:500]}"
        )


def _extract_record(payload: dict[str, Any], key: str) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    record = data.get(key) if isinstance(data, dict) else None
    if not isinstance(record, dict) or not record.get("id"):
        raise TwentyWriteError(f"Twenty response omitted data.{key}.id")
    return record


def _find_token(payload: dict[str, Any], *, exclude: str | None = None) -> str | None:
    def walk(value: Any) -> str | None:
        if isinstance(value, dict):
            token = value.get("token")
            if isinstance(token, str) and token and token != exclude:
                return token
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return None

    return walk(payload.get("data")) if isinstance(payload, dict) else None


def _graphql_error_summary(payload: dict[str, Any]) -> str:
    messages = []
    for error in payload.get("errors") or []:
        if isinstance(error, dict) and error.get("message"):
            messages.append(" ".join(str(error["message"]).split())[:180])
    return "; ".join(messages)


def _base_type_name(type_ref: dict[str, Any]) -> str | None:
    current = type_ref
    while isinstance(current, dict):
        if current.get("name"):
            return str(current["name"])
        current = current.get("ofType")
    return None


def _block(result: dict[str, Any], entity: str, key: str, exc: Exception) -> None:
    reason = " ".join(str(exc).split())[:500]
    result["blocked"].append({"entity": entity, "key": key, "reason": reason})


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise TwentyWriteError(f"{label} is required")
    return text


def _required_id(value: Any, label: str) -> str:
    text = _required_text(value, f"{label} id")
    if not _UUID_RE.fullmatch(text):
        raise TwentyWriteError(f"{label} returned an invalid internal id")
    return text


def _email(value: Any, label: str) -> str:
    text = _required_text(value, label).casefold()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", text):
        raise TwentyWriteError(f"{label} is invalid")
    return text


def _bounded_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise TwentyWriteError(f"{label} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise TwentyWriteError(f"{label} must be an integer") from exc
    if number < minimum or number > maximum:
        raise TwentyWriteError(f"{label} must be between {minimum} and {maximum}")
    return number


def _amount_micros(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TwentyWriteError("opportunity amount must be numeric") from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000000000"):
        raise TwentyWriteError("opportunity amount is outside the supported range")
    micros = amount * Decimal(1_000_000)
    if micros != micros.to_integral_value():
        raise TwentyWriteError("opportunity amount supports at most 6 decimal places")
    return int(micros)


def _enum(value: Any, label: str) -> str:
    text = _required_text(value, label).upper().replace(" ", "_").replace("-", "_")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", text):
        raise TwentyWriteError(f"{label} is invalid")
    return text


def _date(value: Any, label: str) -> str:
    text = _required_text(value, label)
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError as exc:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            raise TwentyWriteError(f"{label} must be an ISO date") from exc
    return parsed.isoformat()


def _unique(
    records: list[dict[str, Any]], key_func: Any, label: str
) -> None:
    keys = [key_func(item) for item in records]
    if len(keys) != len(set(keys)):
        raise TwentyWriteError(f"{label} must be unique within one call")


def _values(records: list[dict[str, Any]], field: str) -> set[str]:
    return {str(item[field]) for item in records if item.get(field)}


def _group(records: list[dict[str, Any]], key_func: Any) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(key_func(record), []).append(record)
    return grouped


def _domain_payload(value: str) -> dict[str, str]:
    raw = value.strip()
    url = raw if re.match(r"^https?://", raw, re.IGNORECASE) else f"https://{raw}"
    label = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE).rstrip("/")
    return {"primaryLinkUrl": url, "primaryLinkLabel": label}


def _same_domain(actual: Any, expected: Any) -> bool:
    def clean(value: Any) -> str:
        return re.sub(r"^https?://", "", str(value or "").strip(), flags=re.IGNORECASE).rstrip("/").casefold()

    return clean(actual) == clean(expected)


def _same_date(actual: Any, expected: Any) -> bool:
    return str(actual or "")[:10] == str(expected or "")[:10]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _redacted_endpoint(path: str) -> str:
    return re.sub(
        r"/[0-9a-f]{8}-[0-9a-f-]{27,36}(?=/|$)",
        "/<id>",
        str(path),
        flags=re.IGNORECASE,
    )
