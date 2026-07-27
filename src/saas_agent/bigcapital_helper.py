"""Deterministic BigCapital customer helper using the seeded REST API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover
    requests = None


_SCHEMA_REJECTION_STATUSES = {400, 415, 422}
_PATCH_UNSUPPORTED_STATUSES = {404, 405, 501}
_DEFAULT_PAGE_SIZE = 200
_MAX_SCAN_RECORDS = 10_000


class BigCapitalError(RuntimeError):
    """Raised when a BigCapital operation cannot be completed exactly."""


@dataclass(frozen=True)
class _CustomerRequest:
    values: dict[str, Any]
    explicit_fields: frozenset[str]

    @property
    def display_name(self) -> str:
        return str(self.values["display_name"])


class BigCapitalClient:
    _FIELDS = {
        "customer_type", "display_name", "company_name", "first_name",
        "last_name", "email", "work_phone", "personal_phone", "website",
        "note", "active", "code",
    }

    def __init__(
        self,
        base_url: str,
        *,
        email: str | None = None,
        password: str | None = None,
        session: Any | None = None,
        timeout: int = 30,
    ) -> None:
        if not str(base_url or "").strip():
            raise BigCapitalError("base_url is required")
        if session is None:
            if requests is None:
                raise BigCapitalError("requests is required when no session is supplied")
            session = requests.Session()
        self.base_url = str(base_url).rstrip("/")
        self.email = str(
            email or os.environ.get("SAAS_AGENT_BIGCAPITAL_EMAIL") or ""
        ).strip()
        self.password = str(
            password or os.environ.get("SAAS_AGENT_BIGCAPITAL_PASSWORD") or ""
        )
        if not self.email or not self.password:
            raise BigCapitalError(
                "BigCapital credentials are required; pass email/password in tool "
                "context or set SAAS_AGENT_BIGCAPITAL_EMAIL and "
                "SAAS_AGENT_BIGCAPITAL_PASSWORD"
            )
        self.session = session
        self.timeout = int(timeout)
        self._authenticated = False

    def query_customers(
        self,
        exact_names: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        requested = [_required_text(value, "exact name") for value in exact_names or []]
        if len(set(requested)) != len(requested):
            raise BigCapitalError("exact_names must be unique")
        limit = _bounded_int(limit, "limit", minimum=1, maximum=_MAX_SCAN_RECORDS)
        customers = self._list_customers(
            max_records=None if requested else limit,
        )
        matches = [
            customer for customer in customers
            if not requested or customer.get("display_name") in requested
        ][:limit]
        grouped = _group_by_name(matches)
        return {
            "requested_exact_names": requested,
            "matched_count": len(matches),
            "missing_exact_names": [name for name in requested if name not in grouped],
            "duplicate_exact_names": [
                name for name in requested if len(grouped.get(name, [])) > 1
            ],
            "customers": matches,
        }

    def ensure_customers(
        self,
        customers: list[dict[str, Any]],
        currency_code: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(customers, list) or not customers:
            raise BigCapitalError("customers must be a non-empty list")
        normalized = [self._normalize_request(item) for item in customers]
        names = [item.display_name for item in normalized]
        if len(set(names)) != len(names):
            raise BigCapitalError("customer display_name values must be unique")
        explicit_currency = (
            _required_text(currency_code, "currency_code").upper()
            if currency_code is not None else None
        )

        existing_rows = self._list_customers(max_records=None)
        existing = _group_by_name(existing_rows)
        duplicates = [name for name in names if len(existing.get(name, [])) > 1]
        if duplicates:
            raise BigCapitalError(
                "multiple existing customers have the requested display name: "
                + ", ".join(duplicates)
            )

        created: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        expected_by_name: dict[str, dict[str, Any]] = {}

        for request in normalized:
            current = (existing.get(request.display_name) or [None])[0]
            expected = {
                key: request.values[key]
                for key in request.explicit_fields
                if request.values.get(key) is not None
            }
            if explicit_currency is not None:
                expected["currency_code"] = explicit_currency
            expected_by_name[request.display_name] = expected

            if current is None:
                create_payload = {
                    "customer_type": "business",
                    "active": True,
                    "currency_code": explicit_currency or "USD",
                    **request.values,
                }
                self._write_with_field_fallback("post", "/api/customers", create_payload)
                created.append(request.display_name)
                continue

            mutable_fields = request.explicit_fields - {"display_name"}
            if explicit_currency is None and not mutable_fields:
                unchanged.append(request.display_name)
                continue

            customer_id = current.get("id")
            if customer_id in (None, ""):
                raise BigCapitalError(
                    f"existing customer {request.display_name!r} has no id"
                )
            detail = self._get_customer(customer_id)
            changes = {
                key: request.values[key]
                for key in mutable_fields
                if detail.get(key) != request.values.get(key)
            }
            if (
                explicit_currency is not None
                and detail.get("currency_code") != explicit_currency
            ):
                changes["currency_code"] = explicit_currency
            if not changes:
                unchanged.append(request.display_name)
                continue

            merged = {
                key: value
                for key, value in detail.items()
                if key != "id" and value is not None
            }
            merged.update(changes)
            merged["display_name"] = request.display_name
            self._update_customer(customer_id, changes, merged)
            updated.append(request.display_name)

        readback = self.query_customers(names, limit=max(len(names) * 2, 200))
        if readback["missing_exact_names"]:
            raise BigCapitalError(
                "customer write succeeded but readback is missing: "
                + ", ".join(readback["missing_exact_names"])
            )
        if readback["duplicate_exact_names"]:
            raise BigCapitalError(
                "customer write produced ambiguous duplicates: "
                + ", ".join(readback["duplicate_exact_names"])
            )

        mismatches = []
        by_name = {
            item.get("display_name"): item for item in readback["customers"]
        }
        for name, expected in expected_by_name.items():
            actual = by_name[name]
            wrong = {
                key: {"expected": value, "actual": actual.get(key)}
                for key, value in expected.items()
                if actual.get(key) != value
            }
            if wrong:
                mismatches.append({"display_name": name, "fields": wrong})
        return {
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "duplicates": [],
            "mismatches": mismatches,
            "readback": readback,
        }

    def _authenticate(self) -> None:
        if self._authenticated:
            return
        response = self.session.post(
            f"{self.base_url}/api/auth/signin",
            json={"email": self.email, "password": self.password},
            timeout=self.timeout,
        )
        _raise_for_status(response, "BigCapital sign-in")
        data = response.json()
        token = data.get("access_token")
        organization_id = data.get("organization_id")
        if not token or organization_id in (None, ""):
            raise BigCapitalError("BigCapital sign-in omitted token or organization_id")
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "organization-id": str(organization_id),
            "Content-Type": "application/json",
        })
        self._authenticated = True

    def _list_customers(self, *, max_records: int | None) -> list[dict[str, Any]]:
        if max_records is not None:
            max_records = _bounded_int(
                max_records, "max_records", minimum=1, maximum=_MAX_SCAN_RECORDS
            )
        self._authenticate()
        customers: list[dict[str, Any]] = []
        seen_pages: set[tuple[Any, ...]] = set()
        page = 1
        while True:
            remaining = (
                _MAX_SCAN_RECORDS - len(customers)
                if max_records is None else max_records - len(customers)
            )
            if remaining <= 0:
                if max_records is None:
                    raise BigCapitalError(
                        f"customer scan exceeded safety cap {_MAX_SCAN_RECORDS}"
                    )
                break
            page_size = min(_DEFAULT_PAGE_SIZE, remaining)
            response = self.session.get(
                f"{self.base_url}/api/customers",
                params={"page": page, "page_size": page_size},
                timeout=self.timeout,
            )
            _raise_for_status(response, "BigCapital customer query")
            rows = _extract_rows(response.json(), "customers")
            if not rows:
                break
            signature = tuple(
                (_pick(row, "id"), _pick(row, "display_name", "displayName"))
                for row in rows
            )
            if signature in seen_pages:
                raise BigCapitalError("BigCapital customer pagination repeated a page")
            seen_pages.add(signature)
            customers.extend(self._normalize_response(row) for row in rows)
            if max_records is not None and len(customers) >= max_records:
                break
            if len(rows) < page_size or not _has_next_page(response.json(), page, len(customers)):
                break
            page += 1
        return customers[:max_records] if max_records is not None else customers

    def _get_customer(self, customer_id: Any) -> dict[str, Any]:
        self._authenticate()
        response = self.session.get(
            f"{self.base_url}/api/customers/{customer_id}",
            params=None,
            timeout=self.timeout,
        )
        _raise_for_status(response, f"BigCapital customer detail {customer_id}")
        item = _extract_object(response.json(), "customer")
        if item is None:
            raise BigCapitalError(
                f"BigCapital customer detail {customer_id} returned no customer"
            )
        normalized = self._normalize_response(item)
        if normalized.get("id") in (None, ""):
            normalized["id"] = customer_id
        return normalized

    def _update_customer(
        self,
        customer_id: Any,
        changes: dict[str, Any],
        merged: dict[str, Any],
    ) -> None:
        path = f"/api/customers/{customer_id}"
        patch = getattr(self.session, "patch", None)
        if callable(patch):
            response = patch(
                f"{self.base_url}{path}",
                json=_camel_payload(changes),
                timeout=self.timeout,
            )
            if getattr(response, "status_code", 500) in _SCHEMA_REJECTION_STATUSES:
                response = patch(
                    f"{self.base_url}{path}", json=changes, timeout=self.timeout
                )
            if getattr(response, "status_code", 500) not in _PATCH_UNSUPPORTED_STATUSES:
                _raise_for_status(response, f"BigCapital PATCH {path}")
                return
        self._write_with_field_fallback("put", path, merged)

    def _write_with_field_fallback(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
    ) -> Any:
        self._authenticate()
        sender = getattr(self.session, method)
        response = sender(
            f"{self.base_url}{path}",
            json=_camel_payload(payload),
            timeout=self.timeout,
        )
        if getattr(response, "status_code", 500) in _SCHEMA_REJECTION_STATUSES:
            response = sender(
                f"{self.base_url}{path}", json=_snake_payload(payload), timeout=self.timeout
            )
        _raise_for_status(response, f"BigCapital {method.upper()} {path}")
        return response

    def _normalize_request(self, item: dict[str, Any]) -> _CustomerRequest:
        if not isinstance(item, dict):
            raise BigCapitalError("each customer must be an object")
        unknown = sorted(set(item) - self._FIELDS)
        if unknown:
            raise BigCapitalError(f"unsupported customer fields: {unknown}")
        values = {
            key: value for key, value in item.items()
            if key in self._FIELDS and value is not None
        }
        values["display_name"] = _required_text(
            values.get("display_name"), "display_name"
        )
        if "customer_type" in values:
            values["customer_type"] = _required_text(
                values["customer_type"], "customer_type"
            )
        if "active" in values and not isinstance(values["active"], bool):
            raise BigCapitalError("active must be a boolean")
        return _CustomerRequest(values, frozenset(values))

    @staticmethod
    def _normalize_response(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": _pick(item, "id"),
            "customer_type": _pick(item, "customer_type", "customerType", "contact_type"),
            "display_name": _pick(item, "display_name", "displayName"),
            "company_name": _pick(item, "company_name", "companyName"),
            "first_name": _pick(item, "first_name", "firstName"),
            "last_name": _pick(item, "last_name", "lastName"),
            "email": _pick(item, "email"),
            "work_phone": _pick(item, "work_phone", "workPhone"),
            "personal_phone": _pick(item, "personal_phone", "personalPhone"),
            "website": _pick(item, "website"),
            "note": _pick(item, "note"),
            "active": _pick(item, "active"),
            "code": _pick(item, "code"),
            "currency_code": _pick(item, "currency_code", "currencyCode"),
        }


def _extract_rows(data: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    if isinstance(data.get(key), list):
        return data[key]
    nested = data.get("data")
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict)]
    if isinstance(nested, dict):
        return _extract_rows(nested, key)
    return []


def _extract_object(data: Any, key: str) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    if isinstance(data.get(key), dict):
        return data[key]
    nested = data.get("data")
    if isinstance(nested, dict):
        candidate = _extract_object(nested, key)
        if candidate is not None:
            return candidate
        if any(field in nested for field in ("id", "displayName", "display_name")):
            return nested
    if any(field in data for field in ("id", "displayName", "display_name")):
        return data
    return None


def _has_next_page(data: Any, page: int, collected: int) -> bool:
    if not isinstance(data, dict):
        return True
    candidates = [data]
    for key in ("meta", "pagination", "data"):
        value = data.get(key)
        if isinstance(value, dict):
            candidates.append(value)
            for nested_key in ("meta", "pagination"):
                nested = value.get(nested_key)
                if isinstance(nested, dict):
                    candidates.append(nested)
    for candidate in candidates:
        total = _pick(candidate, "total", "total_count", "totalCount")
        if total is not None:
            try:
                return collected < int(total)
            except (TypeError, ValueError):
                pass
        last_page = _pick(candidate, "last_page", "lastPage", "pages", "total_pages")
        if last_page is not None:
            try:
                return page < int(last_page)
            except (TypeError, ValueError):
                pass
    return True


def _group_by_name(customers: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for customer in customers:
        name = customer.get("display_name")
        if name not in (None, ""):
            grouped.setdefault(str(name), []).append(customer)
    return grouped


def _camel_payload(payload: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "customer_type": "customerType", "display_name": "displayName",
        "company_name": "companyName", "first_name": "firstName",
        "last_name": "lastName", "work_phone": "workPhone",
        "personal_phone": "personalPhone", "currency_code": "currencyCode",
    }
    return {mapping.get(key, key): value for key, value in payload.items() if value is not None}


def _snake_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _pick(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item:
            return item[key]
    return None


def _raise_for_status(response: Any, operation: str) -> None:
    try:
        response.raise_for_status()
    except Exception as exc:
        text = str(getattr(response, "text", ""))[:1000]
        raise BigCapitalError(f"{operation} failed: {text or exc}") from exc


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise BigCapitalError(f"{label} is required")
    return text


def _bounded_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BigCapitalError(f"{label} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise BigCapitalError(f"{label} must be between {minimum} and {maximum}")
    return parsed
