"""Deterministic OpenProject API helper for persisted work packages."""

from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

try:
    import requests
except Exception:  # pragma: no cover - exercised only in minimal envs.
    requests = None


class OpenProjectError(RuntimeError):
    """Raised when an OpenProject operation cannot be completed exactly."""


class _LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self.csrf_token: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta" and values.get("name") == "csrf-token":
            self.csrf_token = values.get("content") or None
        elif tag == "form":
            self._current = {"action": values.get("action") or "/login", "inputs": []}
        elif tag == "input" and self._current is not None and values.get("name"):
            self._current["inputs"].append(values)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None


class OpenProjectClient:
    """Create users, memberships, and exact work packages through API v3."""

    def __init__(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        session: Any | None = None,
        timeout: int = 30,
    ) -> None:
        if not base_url or not str(base_url).strip():
            raise OpenProjectError("base_url is required")
        if session is None:
            if requests is None:
                raise OpenProjectError("requests is required when no session is supplied")
            session = requests.Session()
        self.base_url = str(base_url).rstrip("/")
        self.username = (
            username
            or os.environ.get("SAAS_AGENT_OPENPROJECT_USERNAME")
            or os.environ.get("OPENPROJECT_USERNAME")
        )
        self.password = (
            password
            or os.environ.get("SAAS_AGENT_OPENPROJECT_PASSWORD")
            or os.environ.get("OPENPROJECT_PASSWORD")
        )
        self.api_key = (
            api_key
            or os.environ.get("SAAS_AGENT_OPENPROJECT_API_KEY")
            or os.environ.get("OPENPROJECT_API_KEY")
        )
        self.session = session
        self.timeout = timeout
        self.auth_mode: str | None = None
        self._auth_preflight_done = False
        self._session_csrf_token: str | None = None
        self.session.auth = None
        if hasattr(self.session, "headers"):
            self.session.headers.update({"Accept": "application/hal+json"})

    def preflight(self) -> dict[str, Any]:
        """Validate authentication and return a non-sensitive service summary."""

        self._ensure_authenticated()
        projects = self._collection("/api/v3/projects")
        return {
            "auth_mode": self.auth_mode,
            "project_count": len(projects),
            "project_names": sorted(
                str(project.get("name") or "")
                for project in projects
                if project.get("name")
            ),
        }

    def ensure_work_packages(
        self,
        project_name: str,
        work_packages: list[dict[str, Any]],
        users: list[dict[str, Any]] | None = None,
        exact_subject_set: bool = False,
    ) -> dict[str, Any]:
        """Ensure one exact work package per requested subject."""

        project_name = _require_text(project_name, "project_name")
        work_packages = _records(work_packages, "work_packages")
        users = _records(users, "users")
        if not work_packages:
            raise OpenProjectError("work_packages is required")

        subjects = [_require_text(item.get("subject"), "work package subject") for item in work_packages]
        if len(set(subjects)) != len(subjects):
            raise OpenProjectError("work package subjects must be unique")

        project = self._find_project(
            self._collection("/api/v3/projects"),
            project_name,
        )
        project_href = _href(project, f"/api/v3/projects/{project['id']}")
        project_id = project.get("id") or project_href.rstrip("/").rsplit("/", 1)[-1]
        types = self._collection(f"/api/v3/projects/{project_id}/types")
        priorities = self._collection("/api/v3/priorities")
        statuses = (
            self._collection("/api/v3/statuses")
            if any(item.get("status") for item in work_packages)
            else []
        )
        roles = self._collection("/api/v3/roles")

        all_users = self._collection("/api/v3/users")
        users_by_login = {
            str(user.get("login") or "").casefold(): user
            for user in all_users
            if user.get("login")
        }
        ensured_users: list[dict[str, Any]] = []
        for spec in users:
            user, created = self._ensure_user(spec, users_by_login)
            self._ensure_membership(project_href, user, spec.get("roles") or ["Member"], roles)
            ensured_users.append({
                "id": user.get("id"),
                "login": user.get("login"),
                "created": created,
            })

        for spec in work_packages:
            assignee = self._resolve_assignee(spec, users_by_login)
            if assignee is not None:
                self._ensure_membership(
                    project_href,
                    assignee,
                    spec.get("assignee_roles") or ["Member"],
                    roles,
                )

        existing = self._collection(f"/api/v3/projects/{project_id}/work_packages")
        ensured_by_subject: dict[str, dict[str, Any]] = {}
        created_ids: list[int] = []
        updated_ids: list[int] = []
        deleted_duplicate_ids: list[int] = []
        pending = list(work_packages)

        while pending:
            progress = False
            for spec in list(pending):
                parent_subject = spec.get("parent_subject")
                parent = None
                if parent_subject:
                    parent = ensured_by_subject.get(str(parent_subject))
                    if parent is None:
                        parent = next(
                            (wp for wp in existing if wp.get("subject") == parent_subject),
                            None,
                        )
                    if parent is None and parent_subject in subjects:
                        continue
                    if parent is None:
                        raise OpenProjectError(
                            f"parent work package not found: {parent_subject}"
                        )

                work_package, created, duplicates = self._ensure_work_package(
                    project_id=str(project_id),
                    spec=spec,
                    existing=existing,
                    types=types,
                    priorities=priorities,
                    statuses=statuses,
                    users_by_login=users_by_login,
                    parent=parent,
                )
                ensured_by_subject[spec["subject"]] = work_package
                existing = [
                    wp for wp in existing
                    if wp.get("id") not in duplicates and wp.get("id") != work_package.get("id")
                ] + [work_package]
                deleted_duplicate_ids.extend(duplicates)
                target_ids = created_ids if created else updated_ids
                target_ids.append(int(work_package["id"]))
                pending.remove(spec)
                progress = True
            if not progress:
                unresolved = [str(item.get("subject")) for item in pending]
                raise OpenProjectError(
                    "could not resolve work package parent order: " + ", ".join(unresolved)
                )

        readback = [
            _compact_work_package(ensured_by_subject[subject])
            for subject in subjects
        ]
        exact_counts = {
            subject: sum(1 for wp in existing if wp.get("subject") == subject)
            for subject in subjects
        }
        return {
            "project_id": project.get("id"),
            "project_name": project.get("name", project_name),
            "auth_mode": self.auth_mode,
            "users": ensured_users,
            "created_ids": created_ids,
            "updated_ids": updated_ids,
            "deleted_duplicate_ids": deleted_duplicate_ids,
            "work_packages": readback,
            "subject_counts": exact_counts,
            "exact_subject_set_requested": bool(exact_subject_set),
            "exact_subject_set_satisfied": all(count == 1 for count in exact_counts.values()),
        }

    def query_work_packages(
        self,
        project_name: str,
        version_name: str | None = None,
        status_name: str | None = None,
        max_items: int = 200,
    ) -> dict[str, Any]:
        """Return normalized work packages without changing OpenProject state."""

        project_name = _require_text(project_name, "project_name")
        try:
            max_items = int(max_items)
        except (TypeError, ValueError) as exc:
            raise OpenProjectError("max_items must be a positive integer") from exc
        if max_items <= 0:
            raise OpenProjectError("max_items must be a positive integer")

        project = self._find_project(
            self._collection("/api/v3/projects"),
            project_name,
        )
        project_href = _href(project, f"/api/v3/projects/{project['id']}")
        project_id = project.get("id") or project_href.rstrip("/").rsplit("/", 1)[-1]
        work_packages = self._collection(
            f"/api/v3/projects/{project_id}/work_packages"
        )

        wanted_version = str(version_name or "").strip().casefold()
        wanted_status = str(status_name or "").strip().casefold()
        normalized: list[dict[str, Any]] = []
        for item in work_packages:
            links = item.get("_links") or {}
            version = _link_title(links.get("version"))
            status = _link_title(links.get("status"))
            if wanted_version and version.casefold() != wanted_version:
                continue
            if wanted_status and status.casefold() != wanted_status:
                continue
            description = _description_raw(item.get("description"))
            row = {
                "id": item.get("id"),
                "subject": str(item.get("subject") or ""),
                "type": _link_title(links.get("type")),
                "status": status,
                "version": version,
                "assignee": _link_title(links.get("assignee")),
                "estimated_hours": _parse_iso_hours(item.get("estimatedTime")),
                "closed": _work_package_closed(item, status),
                "description": description[:1000],
            }
            if len(description) > 1000:
                row["description_truncated"] = True
                row["description_length"] = len(description)
            normalized.append(row)

        normalized.sort(key=lambda item: _sortable_id(item.get("id")))
        normalized = normalized[:max_items]
        return {
            "project_id": project.get("id"),
            "project_name": project.get("name", project_name),
            "project_identifier": project.get("identifier"),
            "auth_mode": self.auth_mode,
            "version_name": version_name,
            "status_name": status_name,
            "count": len(normalized),
            "work_packages": normalized,
        }

    def _ensure_user(
        self,
        spec: dict[str, Any],
        users_by_login: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        login = _require_text(spec.get("login"), "user login")
        existing = users_by_login.get(login.casefold())
        if existing:
            return existing, False
        payload = {
            "login": login,
            "email": _require_text(spec.get("email"), "user email"),
            "firstName": _require_text(spec.get("first_name"), "user first_name"),
            "lastName": _require_text(spec.get("last_name"), "user last_name"),
            "admin": bool(spec.get("admin", False)),
            "status": spec.get("status") or "active",
            "language": spec.get("language") or "en",
        }
        if payload["status"] == "active":
            payload["password"] = _require_text(
                spec.get("password"), "active user password"
            )
        user = self._post("/api/v3/users", payload)
        users_by_login[login.casefold()] = user
        return user, True

    def _ensure_membership(
        self,
        project_href: str,
        user: dict[str, Any],
        role_names: list[str],
        roles: list[dict[str, Any]],
    ) -> None:
        principal_href = _href(user, f"/api/v3/users/{user['id']}")
        memberships = self._collection("/api/v3/memberships")
        for membership in memberships:
            links = membership.get("_links") or {}
            if (
                (links.get("project") or {}).get("href") == project_href
                and (links.get("principal") or {}).get("href") == principal_href
            ):
                return
        selected_roles = [self._find_named(roles, name, "role") for name in role_names]
        self._post("/api/v3/memberships", {
            "_links": {
                "project": {"href": project_href},
                "principal": {"href": principal_href},
                "roles": [{"href": _href(role)} for role in selected_roles],
            }
        })

    def _ensure_work_package(
        self,
        *,
        project_id: str,
        spec: dict[str, Any],
        existing: list[dict[str, Any]],
        types: list[dict[str, Any]],
        priorities: list[dict[str, Any]],
        statuses: list[dict[str, Any]],
        users_by_login: dict[str, dict[str, Any]],
        parent: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], bool, list[int]]:
        subject = _require_text(spec.get("subject"), "work package subject")
        type_obj = self._find_named(types, _require_text(spec.get("type"), "work package type"), "type")
        priority_obj = self._find_named(
            priorities,
            _require_text(spec.get("priority"), "work package priority"),
            "priority",
        )
        assignee = self._resolve_assignee(spec, users_by_login)

        links: dict[str, Any] = {
            "type": {"href": _href(type_obj)},
            "priority": {"href": _href(priority_obj)},
        }
        if assignee is not None:
            links["assignee"] = {"href": _href(assignee)}
        if parent:
            links["parent"] = {"href": _href(parent)}
        if spec.get("status"):
            status = self._find_named(statuses, str(spec["status"]), "status")
            links["status"] = {"href": _href(status)}

        payload: dict[str, Any] = {
            "subject": subject,
            "description": {
                "format": "plain",
                "raw": str(spec.get("description") or ""),
            },
            "_links": links,
        }
        if spec.get("estimated_hours") is not None:
            payload["estimatedTime"] = f"PT{float(spec['estimated_hours']):g}H"
        for source, target in (("start_date", "startDate"), ("due_date", "dueDate")):
            if spec.get(source):
                payload[target] = str(spec[source])

        matches = [wp for wp in existing if wp.get("subject") == subject]
        duplicate_ids: list[int] = []
        if matches:
            target = matches[0]
            payload["lockVersion"] = target.get("lockVersion", 0)
            target_href = _href(target, f"/api/v3/work_packages/{target['id']}")
            work_package = self._patch(
                target_href,
                payload,
            )
            for duplicate in matches[1:]:
                self._delete(_href(duplicate, f"/api/v3/work_packages/{duplicate['id']}"))
                duplicate_ids.append(int(duplicate["id"]))
            return self._get(_href(work_package, target_href)), False, duplicate_ids

        work_package = self._post(
            f"/api/v3/projects/{project_id}/work_packages",
            payload,
        )
        return self._get(
            _href(work_package, f"/api/v3/work_packages/{work_package['id']}")
        ), True, duplicate_ids

    def _resolve_assignee(
        self,
        spec: dict[str, Any],
        users_by_login: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        assignee_login = str(spec.get("assignee_login") or "").strip()
        assignee_name = str(spec.get("assignee_name") or "").strip()
        if assignee_login:
            assignee = users_by_login.get(assignee_login.casefold())
            if not assignee:
                raise OpenProjectError(f"user login not found: {assignee_login}")
        elif assignee_name:
            name_matches = [
                user for user in users_by_login.values()
                if str(user.get("name") or "").casefold() == assignee_name.casefold()
            ]
            if len(name_matches) != 1:
                raise OpenProjectError(
                    f"expected exactly one user named {assignee_name!r}, "
                    f"found {len(name_matches)}"
                )
            assignee = name_matches[0]
        else:
            return None
        return assignee

    def _find_named(
        self,
        items: list[dict[str, Any]],
        name: str,
        label: str,
    ) -> dict[str, Any]:
        wanted = str(name).casefold()
        for item in items:
            if str(item.get("name") or "").casefold() == wanted:
                return item
        raise OpenProjectError(f"{label} not found: {name}")

    def _find_project(
        self,
        projects: list[dict[str, Any]],
        value: str,
    ) -> dict[str, Any]:
        wanted = str(value).strip().casefold()
        matches = [
            project
            for project in projects
            if wanted
            in {
                str(project.get("name") or "").strip().casefold(),
                str(project.get("identifier") or "").strip().casefold(),
            }
        ]
        if len(matches) != 1:
            raise OpenProjectError(
                f"expected exactly one project matching name or identifier "
                f"{value!r}, found {len(matches)}"
            )
        return matches[0]

    def _collection(self, path: str, *, optional: bool = False) -> list[dict[str, Any]]:
        try:
            payload = self._get(path, params={"pageSize": 1000})
        except OpenProjectError:
            if optional:
                return []
            raise
        embedded = payload.get("_embedded") if isinstance(payload, dict) else None
        elements = embedded.get("elements") if isinstance(embedded, dict) else None
        if elements is None:
            return []
        return [item for item in elements if isinstance(item, dict)]

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _auth_candidates(self) -> list[tuple[str, tuple[str, str]]]:
        candidates: list[tuple[str, tuple[str, str]]] = []
        if self.api_key:
            candidates.append(("api_key", ("apikey", self.api_key)))
        if self.username and self.password:
            candidates.append(("basic", (self.username, self.password)))

        unique: list[tuple[str, tuple[str, str]]] = []
        seen: set[tuple[str, str]] = set()
        for mode, auth in candidates:
            if auth in seen:
                continue
            seen.add(auth)
            unique.append((mode, auth))
        return unique

    def _ensure_authenticated(self) -> None:
        if self._auth_preflight_done:
            return

        failures: list[str] = []
        for mode, auth in self._auth_candidates():
            self.session.auth = auth
            try:
                response = self.session.get(
                    self._url("/api/v3/users/me"),
                    timeout=self.timeout,
                )
            except Exception as exc:
                failures.append(f"{mode}=transport:{type(exc).__name__}")
                continue

            status = getattr(response, "status_code", 200)
            if status < 400:
                try:
                    identity = response.json()
                except Exception:
                    identity = None
                if isinstance(identity, dict) and identity.get("id"):
                    self.auth_mode = mode
                    self._auth_preflight_done = True
                    return
                failures.append(f"{mode}=invalid identity response")
            else:
                failures.append(f"{mode}=HTTP {status}")

        self.session.auth = None
        try:
            self._authenticate_with_login_session()
            self.auth_mode = "session_cookie"
            self._auth_preflight_done = True
            return
        except OpenProjectError as exc:
            failures.append(f"session_cookie={exc}")
        raise OpenProjectError(
            "OpenProject authentication preflight failed: " + ", ".join(failures)
        )

    def _authenticate_with_login_session(self) -> None:
        """Log in through the Rails form and validate API access via its cookie."""

        if not self.username or not self.password:
            raise OpenProjectError(
                "username and password are required for session authentication"
            )
        self.session.auth = None
        try:
            login_page = self.session.get(
                self._url("/login"),
                timeout=self.timeout,
            )
        except Exception as exc:
            raise OpenProjectError(
                f"login page transport error ({type(exc).__name__})"
            ) from exc
        if getattr(login_page, "status_code", 200) >= 400:
            raise OpenProjectError(
                f"login page HTTP {getattr(login_page, 'status_code', '?')}"
            )

        parser = _LoginFormParser()
        parser.feed(str(getattr(login_page, "text", "") or ""))
        form = next(
            (
                candidate
                for candidate in parser.forms
                if "login" in str(candidate.get("action") or "").lower()
                or any(
                    item.get("type", "").lower() == "password"
                    for item in candidate.get("inputs", [])
                )
            ),
            None,
        )
        if not form:
            raise OpenProjectError("login form not found")

        payload: dict[str, str] = {}
        username_field = "username"
        password_field = "password"
        for item in form.get("inputs", []):
            name = str(item.get("name") or "")
            field_type = str(item.get("type") or "text").lower()
            if field_type == "hidden":
                payload[name] = str(item.get("value") or "")
            elif field_type in {"text", "email"} and any(
                marker in name.lower() for marker in ("user", "login", "email")
            ):
                username_field = name
            elif field_type == "password":
                password_field = name
        if parser.csrf_token and "authenticity_token" not in payload:
            payload["authenticity_token"] = parser.csrf_token
        login_csrf_token = str(
            parser.csrf_token or payload.get("authenticity_token") or ""
        ).strip()
        payload[username_field] = self.username
        payload[password_field] = self.password

        try:
            response = self.session.post(
                urljoin(f"{self.base_url}/", str(form.get("action") or "/login")),
                data=payload,
                timeout=self.timeout,
                allow_redirects=True,
            )
        except Exception as exc:
            raise OpenProjectError(
                f"login submit transport error ({type(exc).__name__})"
            ) from exc
        if getattr(response, "status_code", 200) >= 400:
            raise OpenProjectError(
                f"login submit HTTP {getattr(response, 'status_code', '?')}"
            )

        identity_probe = self.session.get(
            self._url("/api/v3/users/me"),
            timeout=self.timeout,
        )
        status = getattr(identity_probe, "status_code", 200)
        if status >= 400:
            raise OpenProjectError(f"post-login identity API HTTP {status}")
        try:
            identity = identity_probe.json()
        except Exception:
            identity = None
        if not isinstance(identity, dict) or not identity.get("id"):
            raise OpenProjectError("post-login identity API returned invalid user")

        csrf_token = _csrf_token_from_html(
            str(getattr(response, "text", "") or "")
        )
        if not csrf_token:
            csrf_token = self._refresh_session_csrf_token(required=False)
        if not csrf_token:
            csrf_token = login_csrf_token
        if not csrf_token:
            raise OpenProjectError("post-login page contained no CSRF token")
        self._set_session_csrf_token(csrf_token)

    def _set_session_csrf_token(self, token: str) -> None:
        token = str(token or "").strip()
        if not token:
            raise OpenProjectError("session CSRF token is empty")
        self._session_csrf_token = token
        if hasattr(self.session, "headers"):
            self.session.headers.update({
                "X-CSRF-Token": token,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{self.base_url}/",
            })

    def _refresh_session_csrf_token(self, *, required: bool = True) -> str | None:
        try:
            page = self.session.get(
                f"{self.base_url}/",
                timeout=self.timeout,
            )
        except Exception as exc:
            if required:
                raise OpenProjectError(
                    f"CSRF refresh transport error ({type(exc).__name__})"
                ) from exc
            return None
        status = getattr(page, "status_code", 200)
        if status >= 400:
            if required:
                raise OpenProjectError(f"CSRF refresh page HTTP {status}")
            return None
        token = _csrf_token_from_html(str(getattr(page, "text", "") or ""))
        if not token:
            if required:
                raise OpenProjectError("CSRF refresh page contained no token")
            return None
        self._set_session_csrf_token(token)
        return token

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self._ensure_authenticated()
        kwargs.setdefault("timeout", self.timeout)
        request = getattr(self.session, method.lower())
        response = request(self._url(path), **kwargs)
        status = getattr(response, "status_code", 200)
        if (
            self.auth_mode == "session_cookie"
            and method.lower() in {"post", "patch", "delete"}
            and status in {401, 403, 422}
        ):
            self._refresh_session_csrf_token()
            response = request(self._url(path), **kwargs)
            status = getattr(response, "status_code", 200)
        if status >= 400:
            detail = str(getattr(response, "text", ""))[:500]
            for secret in (self.api_key, self.password):
                if secret:
                    detail = detail.replace(secret, "[redacted]")
            raise OpenProjectError(
                f"{method.upper()} {path} failed with HTTP {status} "
                f"using auth_mode={self.auth_mode}: "
                f"{detail}"
            )
        if status == 204 or not getattr(response, "text", ""):
            return {}
        try:
            return response.json()
        except Exception as exc:
            raise OpenProjectError(
                f"{method.upper()} {path} returned invalid JSON"
            ) from exc

    def _get(self, path: str, **kwargs: Any) -> Any:
        return self._request("get", path, **kwargs)

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("post", path, json=payload)

    def _patch(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("patch", path, json=payload)

    def _delete(self, path: str) -> Any:
        return self._request("delete", path)


def _records(value: Any, name: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise OpenProjectError(f"{name} must be a list of objects")
    return value


def _csrf_token_from_html(html: str) -> str | None:
    parser = _LoginFormParser()
    parser.feed(str(html or ""))
    if parser.csrf_token:
        return parser.csrf_token
    for form in parser.forms:
        for item in form.get("inputs", []):
            if item.get("name") == "authenticity_token" and item.get("value"):
                return str(item["value"])
    return None


def _require_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise OpenProjectError(f"{name} is required")
    return text


def _href(item: dict[str, Any], default: str | None = None) -> str:
    href = ((item.get("_links") or {}).get("self") or {}).get("href") or default
    if not href:
        raise OpenProjectError(f"object has no self href: {item}")
    return str(href)


def _link_title(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("title") or value.get("name") or "")


def _description_raw(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("raw") or "")
    return str(value or "")


def _compact_work_package(item: dict[str, Any]) -> dict[str, Any]:
    """Keep task-relevant readback without returning a full HAL document."""
    links = item.get("_links") if isinstance(item.get("_links"), dict) else {}
    description = _description_raw(item.get("description"))
    result: dict[str, Any] = {
        "id": item.get("id"),
        "subject": str(item.get("subject") or ""),
        "type": _link_title(links.get("type")),
        "status": _link_title(links.get("status")),
        "priority": _link_title(links.get("priority")),
        "assignee": _link_title(links.get("assignee")),
        "parent": _link_title(links.get("parent")),
        "version": _link_title(links.get("version")),
        "description": description[:1000],
    }
    if len(description) > 1000:
        result["description_truncated"] = True
        result["description_length"] = len(description)
    return result


def _parse_iso_hours(value: Any) -> float | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    match = re.fullmatch(
        r"P(?:(?P<days>\d+(?:\.\d+)?)D)?"
        r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
        r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?)?",
        text,
    )
    if not match:
        return None
    days = float(match.group("days") or 0)
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    return days * 24 + hours + minutes / 60


def _work_package_closed(item: dict[str, Any], status: str) -> bool:
    links = item.get("_links") or {}
    link_status = links.get("status") or {}
    embedded_status = ((item.get("_embedded") or {}).get("status") or {})
    for source in (link_status, embedded_status):
        if isinstance(source.get("isClosed"), bool):
            return bool(source["isClosed"])
    return status.casefold() in {"closed", "done", "resolved"}


def _sortable_id(value: Any) -> tuple[int, int | str]:
    try:
        return 0, int(value)
    except (TypeError, ValueError):
        return 1, str(value or "")
