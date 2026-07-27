from urllib.parse import urlparse

import pytest

from saas_agent.openproject_helper import OpenProjectClient, OpenProjectError


@pytest.fixture(autouse=True)
def openproject_credentials(monkeypatch):
    monkeypatch.setenv("OPENPROJECT_USERNAME", "admin")
    monkeypatch.setenv("OPENPROJECT_PASSWORD", "AdminPass123!")


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=None):
        self.payload = payload
        self.status_code = status_code
        self.text = ("" if payload is None else str(payload)) if text is None else text

    def json(self):
        return self.payload


def collection(*items):
    return {"_embedded": {"elements": list(items)}}


class FakeOpenProjectSession:
    def __init__(self):
        self.auth = None
        self.headers = {}
        self.calls = []
        self.projects = [{
            "id": 3,
            "name": "Security Audit",
            "identifier": "security-audit",
            "_links": {"self": {"href": "/api/v3/projects/3"}},
        }]
        self.users = [{
            "id": 1,
            "login": "admin",
            "name": "OpenProject Admin",
            "_links": {"self": {"href": "/api/v3/users/1"}},
        }]
        self.memberships = []
        self.work_packages = []
        self.next_user_id = 10
        self.next_wp_id = 100

    def get(self, url, **kwargs):
        path = urlparse(url).path
        self.calls.append(("GET", path, kwargs))
        if path == "/api/v3/users/me":
            return FakeResponse({"id": 1, "login": "admin"})
        if path == "/api/v3/projects":
            return FakeResponse(collection(*self.projects))
        if path == "/api/v3/projects/3/types":
            return FakeResponse(collection(
                {"id": 1, "name": "Task", "_links": {"self": {"href": "/api/v3/types/1"}}},
                {"id": 2, "name": "Bug", "_links": {"self": {"href": "/api/v3/types/2"}}},
            ))
        if path == "/api/v3/priorities":
            return FakeResponse(collection(
                {"id": 2, "name": "Normal", "_links": {"self": {"href": "/api/v3/priorities/2"}}},
                {"id": 5, "name": "Immediate", "_links": {"self": {"href": "/api/v3/priorities/5"}}},
            ))
        if path == "/api/v3/users":
            return FakeResponse(collection(*self.users))
        if path == "/api/v3/roles":
            return FakeResponse(collection({
                "id": 4,
                "name": "Member",
                "_links": {"self": {"href": "/api/v3/roles/4"}},
            }))
        if path == "/api/v3/memberships":
            return FakeResponse(collection(*self.memberships))
        if path == "/api/v3/projects/3/work_packages":
            return FakeResponse(collection(*self.work_packages))
        if path.startswith("/api/v3/work_packages/"):
            wp_id = int(path.rsplit("/", 1)[1])
            return FakeResponse(next(wp for wp in self.work_packages if wp["id"] == wp_id))
        raise AssertionError(f"unexpected GET {path}")

    def post(self, url, **kwargs):
        path = urlparse(url).path
        payload = kwargs.get("json") or {}
        self.calls.append(("POST", path, payload))
        if path == "/api/v3/users":
            user = {
                **payload,
                "id": self.next_user_id,
                "name": f"{payload['firstName']} {payload['lastName']}",
                "_links": {"self": {"href": f"/api/v3/users/{self.next_user_id}"}},
            }
            self.next_user_id += 1
            self.users.append(user)
            return FakeResponse(user, 201)
        if path == "/api/v3/memberships":
            membership = {
                "id": len(self.memberships) + 1,
                "_links": payload["_links"],
            }
            self.memberships.append(membership)
            return FakeResponse(membership, 201)
        if path == "/api/v3/projects/3/work_packages":
            work_package = {
                **payload,
                "id": self.next_wp_id,
                "lockVersion": 0,
                "_links": {
                    **payload.get("_links", {}),
                    "self": {"href": f"/api/v3/work_packages/{self.next_wp_id}"},
                },
            }
            self.next_wp_id += 1
            self.work_packages.append(work_package)
            return FakeResponse(work_package, 201)
        raise AssertionError(f"unexpected POST {path}")

    def patch(self, url, **kwargs):
        path = urlparse(url).path
        payload = kwargs.get("json") or {}
        self.calls.append(("PATCH", path, payload))
        wp_id = int(path.rsplit("/", 1)[1])
        work_package = next(wp for wp in self.work_packages if wp["id"] == wp_id)
        work_package.update(payload)
        return FakeResponse(work_package)

    def delete(self, url, **kwargs):
        path = urlparse(url).path
        self.calls.append(("DELETE", path, kwargs))
        wp_id = int(path.rsplit("/", 1)[1])
        self.work_packages = [wp for wp in self.work_packages if wp["id"] != wp_id]
        return FakeResponse(None, 204)


class AuthProbeSession:
    def __init__(self, statuses):
        self.auth = None
        self.headers = {}
        self.statuses = statuses
        self.calls = []

    def get(self, url, **kwargs):
        path = urlparse(url).path
        status = self.statuses.get(self.auth, 401)
        self.calls.append((self.auth, path, kwargs, status))
        payload = {"id": 1, "login": "admin"}
        return FakeResponse(payload, status)


def test_ensure_work_packages_preserves_identity_links_and_plain_description():
    session = FakeOpenProjectSession()
    client = OpenProjectClient("http://localhost:32003", session=session)

    result = client.ensure_work_packages(
        "Security Audit",
        users=[{
            "login": "frank.nguyen",
            "email": "frank.nguyen@example.com",
            "first_name": "Frank",
            "last_name": "Nguyen",
            "password": "ExactPass123!",
            "roles": ["Member"],
        }],
        work_packages=[
            {
                "subject": "Dependency map parent",
                "type": "Task",
                "priority": "Normal",
                "assignee_name": "OpenProject Admin",
                "description": "Source_File: exact_value",
            },
            {
                "subject": "Cross-team child",
                "type": "Bug",
                "priority": "Immediate",
                "assignee_login": "frank.nguyen",
                "parent_subject": "Dependency map parent",
                "description": "Do_not_escape_underscores",
            },
        ],
    )

    user_call = next(call for call in session.calls if call[:2] == ("POST", "/api/v3/users"))
    assert user_call[2]["login"] == "frank.nguyen"
    assert user_call[2]["password"] == "ExactPass123!"
    membership_call = next(
        call for call in session.calls if call[:2] == ("POST", "/api/v3/memberships")
    )
    assert membership_call[2]["_links"]["principal"]["href"] == "/api/v3/users/10"
    membership_principals = {
        call[2]["_links"]["principal"]["href"]
        for call in session.calls
        if call[:2] == ("POST", "/api/v3/memberships")
    }
    assert membership_principals == {"/api/v3/users/1", "/api/v3/users/10"}

    child = next(wp for wp in session.work_packages if wp["subject"] == "Cross-team child")
    parent = next(wp for wp in session.work_packages if wp["subject"] == "Dependency map parent")
    assert parent["_links"]["assignee"]["href"] == "/api/v3/users/1"
    assert child["description"] == {
        "format": "plain",
        "raw": "Do_not_escape_underscores",
    }
    assert child["_links"]["parent"]["href"] == "/api/v3/work_packages/100"
    assert child["_links"]["assignee"]["href"] == "/api/v3/users/10"
    assert child["_links"]["priority"]["href"] == "/api/v3/priorities/5"
    assert result["work_packages"][1]["subject"] == "Cross-team child"
    assert "_links" not in result["work_packages"][1]
    assert result["work_packages"][1]["description"] == "Do_not_escape_underscores"


def test_existing_subject_is_updated_and_duplicate_is_removed():
    session = FakeOpenProjectSession()
    session.work_packages = [
        {
            "id": 7,
            "lockVersion": 2,
            "subject": "Retro action items",
            "_links": {"self": {"href": "/api/v3/work_packages/7"}},
        },
        {
            "id": 8,
            "lockVersion": 1,
            "subject": "Retro action items",
            "_links": {"self": {"href": "/api/v3/work_packages/8"}},
        },
    ]
    client = OpenProjectClient("http://localhost:32003", session=session)

    result = client.ensure_work_packages(
        "Security Audit",
        work_packages=[{
            "subject": "Retro action items",
            "type": "Task",
            "priority": "Normal",
            "assignee_login": "admin",
            "description": "exact text",
        }],
    )

    patch_call = next(call for call in session.calls if call[:2] == ("PATCH", "/api/v3/work_packages/7"))
    assert patch_call[2]["lockVersion"] == 2
    assert ("DELETE", "/api/v3/work_packages/8") in [call[:2] for call in session.calls]
    assert result["deleted_duplicate_ids"] == [8]


def test_unassigned_parent_omits_assignee_and_child_keeps_assignee():
    session = FakeOpenProjectSession()
    client = OpenProjectClient("http://localhost:32003", session=session)

    result = client.ensure_work_packages(
        "Security Audit",
        work_packages=[
            {
                "subject": "Dependency map parent",
                "type": "Task",
                "priority": "Normal",
                "description": "Parent intentionally unassigned",
            },
            {
                "subject": "Cross-team child",
                "type": "Bug",
                "priority": "Immediate",
                "assignee_name": "OpenProject Admin",
                "parent_subject": "Dependency map parent",
                "description": "Assigned child",
            },
        ],
    )

    parent = next(wp for wp in session.work_packages if wp["subject"] == "Dependency map parent")
    child = next(wp for wp in session.work_packages if wp["subject"] == "Cross-team child")
    assert "assignee" not in parent["_links"]
    assert child["_links"]["assignee"]["href"] == "/api/v3/users/1"
    assert child["_links"]["parent"]["href"] == "/api/v3/work_packages/100"
    assert result["work_packages"][0]["subject"] == "Dependency map parent"


def test_active_new_user_requires_explicit_password():
    session = FakeOpenProjectSession()
    client = OpenProjectClient("http://localhost:32003", session=session)

    with pytest.raises(OpenProjectError, match="active user password is required"):
        client.ensure_work_packages(
            "Security Audit",
            users=[{
                "login": "new.user",
                "email": "new.user@example.com",
                "first_name": "New",
                "last_name": "User",
                "roles": ["Member"],
            }],
            work_packages=[{
                "subject": "Task",
                "type": "Task",
                "priority": "Normal",
                "assignee_login": "admin",
                "description": "text",
            }],
        )


def test_project_identifier_is_accepted_for_write_helper():
    session = FakeOpenProjectSession()
    client = OpenProjectClient("http://localhost:32003", session=session)

    result = client.ensure_work_packages(
        "security-audit",
        work_packages=[{
            "subject": "Identifier-resolved task",
            "type": "Task",
            "priority": "Normal",
            "assignee_login": "admin",
            "description": "exact text",
        }],
    )

    assert result["project_id"] == 3
    assert result["project_name"] == "Security Audit"


def test_project_resolution_rejects_ambiguous_name_or_identifier():
    session = FakeOpenProjectSession()
    session.projects.append({
        "id": 4,
        "name": "security-audit",
        "identifier": "other-project",
        "_links": {"self": {"href": "/api/v3/projects/4"}},
    })
    client = OpenProjectClient("http://localhost:32003", session=session)

    with pytest.raises(OpenProjectError, match="expected exactly one project"):
        client.query_work_packages("security-audit")


@pytest.mark.parametrize(
    ("value", "expected_id"),
    [
        ("devops-automation", 1),
        ("Data Analytics Pipeline", 2),
        ("demo-project", 3),
        ("SECURITY AUDIT", 4),
    ],
)
def test_project_resolver_accepts_real_name_and_identifier_shapes(value, expected_id):
    client = OpenProjectClient(
        "http://localhost:32003",
        session=FakeOpenProjectSession(),
    )
    projects = [
        {"id": 1, "name": "DevOps Automation", "identifier": "devops-automation"},
        {"id": 2, "name": "Data Analytics Pipeline", "identifier": "data-analytics-pipeline"},
        {"id": 3, "name": "Demo Project", "identifier": "demo-project"},
        {"id": 4, "name": "Security Audit", "identifier": "security-audit"},
    ]

    assert client._find_project(projects, value)["id"] == expected_id


def test_query_work_packages_returns_normalized_filtered_rows():
    session = FakeOpenProjectSession()
    session.work_packages = [
        {
            "id": 12,
            "subject": "Closed release check",
            "estimatedTime": "PT3H30M",
            "description": {"format": "plain", "raw": "Persisted body"},
            "_links": {
                "self": {"href": "/api/v3/work_packages/12"},
                "type": {"title": "Task", "href": "/api/v3/types/1"},
                "status": {
                    "title": "Closed",
                    "href": "/api/v3/statuses/13",
                    "isClosed": True,
                },
                "version": {"title": "Release 1", "href": "/api/v3/versions/7"},
                "assignee": {"title": "OpenProject Admin", "href": "/api/v3/users/1"},
            },
        },
        {
            "id": 5,
            "subject": "Open release check",
            "estimatedTime": "PT45M",
            "description": {"format": "plain", "raw": "Not selected"},
            "_links": {
                "type": {"title": "Bug"},
                "status": {"title": "New", "isClosed": False},
                "version": {"title": "Release 2"},
                "assignee": {"title": "Frank Nguyen"},
            },
        },
    ]
    client = OpenProjectClient("http://localhost:32003", session=session)

    result = client.query_work_packages(
        "security-audit",
        version_name="release 1",
        status_name="closed",
    )

    assert result["project_identifier"] == "security-audit"
    assert result["count"] == 1
    assert result["work_packages"] == [{
        "id": 12,
        "subject": "Closed release check",
        "type": "Task",
        "status": "Closed",
        "version": "Release 1",
        "assignee": "OpenProject Admin",
        "estimated_hours": 3.5,
        "closed": True,
        "description": "Persisted body",
    }]


def test_auth_preflight_falls_back_from_api_key_to_basic_auth():
    session = AuthProbeSession({
        ("apikey", "wrong-env-key"): 401,
        ("admin", "AdminPass123!"): 200,
    })
    client = OpenProjectClient(
        "http://localhost:32003",
        api_key="wrong-env-key",
        session=session,
    )

    result = client.preflight()

    assert result["auth_mode"] == "basic"
    assert session.auth == ("admin", "AdminPass123!")
    probe_modes = [
        call[0] for call in session.calls
        if call[1] == "/api/v3/users/me"
    ]
    assert probe_modes == [
        ("apikey", "wrong-env-key"),
        ("admin", "AdminPass123!"),
    ]


def test_auth_preflight_falls_back_to_login_session_cookie():
    class LoginSession:
        def __init__(self):
            self.auth = None
            self.headers = {}
            self.logged_in = False
            self.posts = []

        def get(self, url, **kwargs):
            path = urlparse(url).path
            if path == "/login":
                return FakeResponse(
                    text=(
                        '<form action="/login" method="post">'
                        '<input type="hidden" name="authenticity_token" value="csrf">'
                        '<input type="text" name="username">'
                        '<input type="password" name="password">'
                        '</form>'
                    )
                )
            if path == "/api/v3/users/me":
                return FakeResponse(
                    {"id": 1, "login": "admin"},
                    200 if self.logged_in else 401,
                )
            if path == "/":
                return FakeResponse(
                    text='<meta name="csrf-token" content="write-csrf">',
                    status_code=200 if self.logged_in else 401,
                )
            status = 200 if self.logged_in else 401
            return FakeResponse(
                collection({"id": 1, "name": "Demo", "identifier": "demo"}),
                status,
            )

        def post(self, url, **kwargs):
            self.posts.append((urlparse(url).path, kwargs))
            data = kwargs.get("data") or {}
            self.logged_in = (
                data.get("username") == "admin"
                and data.get("password") == "AdminPass123!"
                and data.get("authenticity_token") == "csrf"
            )
            return FakeResponse(
                {},
                200 if self.logged_in else 401,
                text=(
                    '<meta name="csrf-token" content="write-csrf">'
                    if self.logged_in else ""
                ),
            )

    session = LoginSession()
    client = OpenProjectClient("http://localhost:32003", session=session)

    result = client.preflight()

    assert result["auth_mode"] == "session_cookie"
    assert session.posts[0][0] == "/login"
    assert session.auth is None
    assert session.headers["X-CSRF-Token"] == "write-csrf"
    assert session.headers["X-Requested-With"] == "XMLHttpRequest"


def test_login_session_cookie_can_write_with_csrf_header():
    class LoginWriteSession:
        def __init__(self):
            self.auth = None
            self.headers = {}
            self.logged_in = False
            self.api_posts = []

        def get(self, url, **kwargs):
            path = urlparse(url).path
            if path == "/login":
                return FakeResponse(
                    text=(
                        '<form action="/login" method="post">'
                        '<input type="hidden" name="authenticity_token" value="login-csrf">'
                        '<input type="text" name="username">'
                        '<input type="password" name="password">'
                        '</form>'
                    )
                )
            if path == "/api/v3/users/me":
                return FakeResponse(
                    {"id": 1, "login": "admin"},
                    200 if self.logged_in else 401,
                )
            if path == "/api/v3/projects":
                return FakeResponse(collection(), 401)
            if path == "/":
                return FakeResponse(
                    text='<meta name="csrf-token" content="write-csrf">',
                    status_code=200 if self.logged_in else 401,
                )
            raise AssertionError(f"unexpected GET {path}")

        def post(self, url, **kwargs):
            path = urlparse(url).path
            if path == "/login":
                data = kwargs.get("data") or {}
                self.logged_in = (
                    data.get("username") == "admin"
                    and data.get("password") == "AdminPass123!"
                )
                return FakeResponse(
                    {},
                    200 if self.logged_in else 401,
                    text='<meta name="csrf-token" content="write-csrf">',
                )
            self.api_posts.append((path, kwargs, dict(self.headers)))
            if not self.logged_in or self.headers.get("X-CSRF-Token") != "write-csrf":
                return FakeResponse({"message": "unauthorized"}, 401)
            return FakeResponse({"id": 99, "subject": "Created"}, 201)

    session = LoginWriteSession()
    client = OpenProjectClient("http://localhost:32003", session=session)

    result = client._post(
        "/api/v3/projects/3/work_packages",
        {"subject": "Created"},
    )

    assert result["id"] == 99
    assert session.api_posts[0][2]["X-CSRF-Token"] == "write-csrf"
    assert session.api_posts[0][2]["Referer"] == "http://localhost:32003/"


def test_login_session_write_refreshes_stale_csrf_and_retries_once():
    class RefreshingSession:
        def __init__(self):
            self.auth = None
            self.headers = {}
            self.logged_in = False
            self.api_tokens = []

        def get(self, url, **kwargs):
            path = urlparse(url).path
            if path == "/login":
                return FakeResponse(
                    text=(
                        '<form action="/login" method="post">'
                        '<input type="hidden" name="authenticity_token" value="login-csrf">'
                        '<input type="text" name="username">'
                        '<input type="password" name="password">'
                        '</form>'
                    )
                )
            if path == "/api/v3/users/me":
                return FakeResponse(
                    {"id": 1, "login": "admin"},
                    200 if self.logged_in else 401,
                )
            if path == "/":
                return FakeResponse(
                    text='<meta name="csrf-token" content="fresh-csrf">'
                )
            raise AssertionError(f"unexpected GET {path}")

        def post(self, url, **kwargs):
            path = urlparse(url).path
            if path == "/login":
                self.logged_in = True
                return FakeResponse(
                    {},
                    200,
                    text='<meta name="csrf-token" content="stale-csrf">',
                )
            token = self.headers.get("X-CSRF-Token")
            self.api_tokens.append(token)
            if token == "stale-csrf":
                return FakeResponse({"message": "stale token"}, 401)
            return FakeResponse({"id": 100, "subject": "Retried"}, 201)

    session = RefreshingSession()
    client = OpenProjectClient("http://localhost:32003", session=session)

    result = client._post(
        "/api/v3/projects/3/work_packages",
        {"subject": "Retried"},
    )

    assert result["id"] == 100
    assert session.api_tokens == ["stale-csrf", "fresh-csrf"]


def test_login_session_rejects_public_project_probe_without_authenticated_user():
    class PublicProjectsSession:
        def __init__(self):
            self.auth = None
            self.headers = {}

        def get(self, url, **kwargs):
            path = urlparse(url).path
            if path == "/login":
                return FakeResponse(
                    text=(
                        '<form action="/login" method="post">'
                        '<input type="hidden" name="authenticity_token" value="csrf">'
                        '<input type="text" name="username">'
                        '<input type="password" name="password">'
                        '</form>'
                    )
                )
            if path == "/api/v3/users/me":
                return FakeResponse({"message": "unauthorized"}, 401)
            return FakeResponse(collection({"id": 3, "name": "Public"}), 401)

        def post(self, url, **kwargs):
            return FakeResponse({}, 200, text='<meta name="csrf-token" content="csrf">')

    client = OpenProjectClient(
        "http://localhost:32003",
        session=PublicProjectsSession(),
    )

    with pytest.raises(OpenProjectError, match="post-login identity API HTTP 401"):
        client.preflight()


def test_auth_preflight_prefers_environment_api_key():
    session = AuthProbeSession({("apikey", "real-key"): 200})
    client = OpenProjectClient(
        "http://localhost:32003",
        api_key="real-key",
        session=session,
    )

    result = client.preflight()

    assert result["auth_mode"] == "api_key"
    assert session.auth == ("apikey", "real-key")


def test_auth_preflight_failure_is_diagnostic_and_secret_free():
    session = AuthProbeSession({})
    client = OpenProjectClient(
        "http://localhost:32003",
        username="admin-user",
        password="do-not-leak-password",
        api_key="do-not-leak-key",
        session=session,
    )

    with pytest.raises(OpenProjectError) as exc_info:
        client.preflight()

    message = str(exc_info.value)
    assert "api_key=HTTP 401" in message
    assert "basic=HTTP 401" in message
    assert "do-not-leak" not in message


def test_auth_preflight_requires_explicit_credentials(monkeypatch):
    monkeypatch.delenv("OPENPROJECT_USERNAME", raising=False)
    monkeypatch.delenv("OPENPROJECT_PASSWORD", raising=False)
    monkeypatch.delenv("OPENPROJECT_API_KEY", raising=False)
    client = OpenProjectClient(
        "http://localhost:32003",
        session=AuthProbeSession({}),
    )

    with pytest.raises(OpenProjectError, match="username and password are required"):
        client.preflight()


def test_auth_preflight_is_cached_for_business_requests():
    session = FakeOpenProjectSession()
    client = OpenProjectClient("http://localhost:32003", session=session)

    first = client.query_work_packages("security-audit")
    second = client.query_work_packages("security-audit")

    probes = [
        call for call in session.calls
        if call[:2] == ("GET", "/api/v3/users/me")
    ]
    assert len(probes) == 1
    assert first["auth_mode"] == "basic"
    assert second["auth_mode"] == "basic"


def test_auth_preflight_rejects_html_login_page_with_http_200():
    class HtmlLoginSession(AuthProbeSession):
        def get(self, url, **kwargs):
            path = urlparse(url).path
            self.calls.append((self.auth, path, kwargs, 200))
            return FakeResponse(None, 200)

    client = OpenProjectClient(
        "http://localhost:32003",
        session=HtmlLoginSession({}),
    )

    with pytest.raises(OpenProjectError, match="invalid identity response"):
        client.preflight()


def test_openproject_http_error_redacts_known_credentials():
    class ErrorSession(FakeOpenProjectSession):
        def get(self, url, **kwargs):
            path = urlparse(url).path
            if path == "/api/v3/projects" and kwargs.get("params") == {"pageSize": 1}:
                return super().get(url, **kwargs)
            return FakeResponse(
                None,
                500,
            ) if path == "/api/v3/priorities" else super().get(url, **kwargs)

    session = ErrorSession()
    response = FakeResponse(None, 500)
    response.text = "AdminPass123! should-not-appear"
    original_get = session.get

    def get_with_secret(url, **kwargs):
        if urlparse(url).path == "/api/v3/priorities":
            return response
        return original_get(url, **kwargs)

    session.get = get_with_secret
    client = OpenProjectClient("http://localhost:32003", session=session)

    with pytest.raises(OpenProjectError) as exc_info:
        client._get("/api/v3/priorities")

    assert "AdminPass123!" not in str(exc_info.value)
    assert "[redacted]" in str(exc_info.value)
