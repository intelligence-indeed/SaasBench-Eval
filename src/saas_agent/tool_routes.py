"""Route app-specific capabilities from explicit app connection metadata."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from saas_agent.baserow_helper import BaserowClient
from saas_agent.bigcapital_helper import BigCapitalClient
from saas_agent.code_server_helper import CodeServerClient
from saas_agent.metabase_helper import MetabaseClient
from saas_agent.openproject_helper import OpenProjectClient
from saas_agent.task_credentials import (
    TaskCredential,
    TaskCredentialError,
    parse_task_credential,
)
from saas_agent.twenty_helper import TwentyClient
from saas_agent.twenty_write_helper import TwentyWriteClient


_ENABLED_MODES = {"routing", "tools", "enabled", "1", "true"}
_DISABLED_MODES = {"disabled", "off", "legacy", "0", "false"}


class ToolCircuitOpenError(RuntimeError):
    """Raised after a routed helper repeats the same failure in one task."""


class _RepeatedFailureCircuit:
    def __init__(self, app_name: str, threshold: int = 2) -> None:
        self.app_name = app_name
        self.threshold = threshold
        self.last_signature: str | None = None
        self.repeat_count = 0
        self.open_reason: str | None = None

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self.open_reason:
            raise ToolCircuitOpenError(
                f"{self.app_name} helper circuit is open for this task after "
                f"a repeated failure: {self.open_reason}. Do not call this "
                f"helper again; use a browser fallback or continue another subtask."
            )
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            signature = _failure_signature(exc)
            if signature == self.last_signature:
                self.repeat_count += 1
            else:
                self.last_signature = signature
                self.repeat_count = 1
            if self.repeat_count >= self.threshold:
                self.open_reason = signature
                raise ToolCircuitOpenError(
                    f"{exc} Circuit opened for {self.app_name} after the same "
                    f"failure occurred {self.repeat_count} times. Do not call "
                    f"this helper again in the current task."
                ) from exc
            raise
        self.last_signature = None
        self.repeat_count = 0
        return result


def _failure_signature(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    message = re.sub(r"https?://[^\s\"']+", "<url>", message)
    return f"{type(exc).__name__}: {message[:400]}"


BASEROW_TOOL_RULES = """\
### Routed Helper Tools
The following app-specific helper action is enabled in addition to the standard
browser and file actions:
- `baserow_ensure_table(database_name, table_name, fields, rows, views=None, replace_rows=false)`:
  creates or updates a Baserow database table through the Baserow REST API,
  removes blank default rows, creates requested views, then returns readback
  evidence.

Use this helper only after you have determined the exact schema and row values
from the task and source files. Do not invent rows. If a Baserow UI grid becomes
unstable or repeated row/field edits fail, prefer this helper over continuing
to click the grid.

When the exact required rows are known, pass `replace_rows=true` so the
table contains exactly those rows. For linked fields, use field specs like
`{"name": "Source Project", "type": "link_row", "link_row_table": "Team Ownership"}`.

Optional `views` entries can use field names, for example:
`{"name": "Remediation Queue", "type": "grid", "filters": [{"field": "Below Threshold", "type": "equal", "value": true}], "sorts": [{"field": "Coverage Pct", "order": "asc"}]}`.
"""


CODE_SERVER_TOOL_RULES = """\
### code-server Helper Tools
The following deterministic code-server helper actions are enabled in addition
to browser/file actions:
- `code_search_files(pattern, roots=None, include_globs=None, exclude_globs=None, max_matches=100, case_sensitive=false)`: searches source files inside the code-server Docker container and returns matching path/line snippets.
- `code_read_files(paths, max_chars=20000)`: reads source files from the code-server container.
- `code_write_file(path, content)`: writes a file under `/home/coder/project` when `path` is relative. Prefer relative paths such as `devops-configs/docs/report.md` so durable files land in `/home/coder/project/...`.
- `code_exec(command, cwd=None, timeout=120, max_output=20000)`: runs a shell command inside the code-server Docker container and returns stdout, stderr, and returncode. Use this for grep, awk, npm, pnpm, jest, vitest, pytest, git status, and script execution.
- `code_run_python(script, cwd=None, timeout=120, max_output=20000)`: runs a Python script inside the code-server Docker container and returns stdout, stderr, and returncode.
- `code_scan_docker_security(dockerfiles, audit_date, finding_id_prefix="HS")`: deterministically audits the exact caller-supplied `{service: Dockerfile path}` mapping and scans those same files for hardcoded secrets, returning compact `audit_rows`, `secret_rows`, and summary values ready for Baserow.
- `code_scan_project_dependencies(projects, scan_roots, ownership)`: deterministically scans real import/require/include references under explicit project roots, returning compact `team_ownership`, `dependency_edges`, and summary values ready for Baserow link-row tables. Derive every argument from the task or source data; there are no hidden project defaults.
- `code_collect_test_metrics(projects)`: runs each explicitly supplied read-only test command once, parses aggregate totals only from pytest/CTest/Jest/Vitest output, counts files from explicit test globs, and reports whether tracked source changed. Every project entry requires `project`, `command`, and `test_globs`; optional keys are `path`, `parser`, and `timeout`.
- `code_git_commit(repo_path, message, paths=None)`: stages paths in a repository under `/home/coder/project` and creates a git commit.

For code-server tasks, `/home/coder/project` is the durable workspace root.
Always prefer `/home/coder/project/<repo>` or relative paths resolved under
`/home/coder/project` for reads, writes, command cwd, git commits, and evidence.
The helper keeps `/home/coder/project/<repo>` bridged to workspace repos when
the app exposes them under `/home/coder/workspace`; do not write deliverables
only under `/home/coder/workspace`.

An exact code-search pattern returning zero matches is not proof that the
requested objects do not exist. Inspect the surrounding syntax and broaden
only the syntactic part of the search before declaring the source blocked.

Use these helpers instead of the integrated terminal when terminal input is
duplicated/corrupted, dependency installation hangs, or VS Code search is too
slow. After collecting enough row-level evidence for a Baserow table, move on to
Baserow helper calls instead of spending many extra steps in code-server.

When a task asks you to open the integrated terminal or run shell commands in
code-server, use `code_exec` instead of typing into the UI terminal. After
writing a script with `code_write_file`, run it with `code_exec`. Use
`code_run_python` for Python-based measurement or parsing tasks when shell
quoting would be fragile. Use `code_git_commit` for required commits; do not
rely on the Source Control UI.

For Docker compliance tasks, call `code_scan_docker_security` before creating
Baserow rows or OpenProject Bug/Task work packages. If it returns `secret_rows`,
create one Baserow row and one `SECRET LEAK [...]` Bug work package per finding.
Supply every Dockerfile path and audit date from the task or inspected source;
the helper has no hidden service list or date.
For cross-project dependency-map tasks, call `code_scan_project_dependencies`
and use its `team_ownership` and `dependency_edges` directly with
`baserow_ensure_table`; make Source/Target Project fields `link_row` fields to
the Team Ownership table.

For audit or measurement tasks, use `code_collect_test_metrics`. Never install
dependencies or edit source to make tests pass. Record the exact observed
outcome, including blockers and unparsed output. If a required input source is
missing, report an infrastructure blocker; do not synthesize replacement data.
Call it directly with the task's explicit commands and paths, for example:
`code_collect_test_metrics(projects=[{"project":"service-a","path":"service-a","command":"pytest tests/ -v","parser":"pytest","test_globs":["tests/**/*.py"],"timeout":300},{"project":"service-b","path":"service-b","command":"ctest --test-dir build --output-on-failure","parser":"ctest","test_globs":["tests/**/*"],"timeout":300}])`.
Run each requested command once. A missing executable, missing prerequisite,
timeout, nonzero exit, or unparsed output is evidence to record and carry into
downstream artifacts, not a reason to install packages, repair source, or rerun
the same command through `code_exec`.
This helper reports aggregate totals only. It does not produce per-test-case
rows, per-module coverage, project file counts, or complexity metrics; collect
those separately from the exact source requested by the task.

Do not use the generic `write_file` for files requested inside code-server,
source repositories, `/home/coder/project`, or task deliverables.
Use `code_write_file` for those files. Use generic `write_file` only for local
scratch notes such as `todo.md`.
"""


OPENPROJECT_TOOL_RULES = """\
### OpenProject Helper Tool
- `openproject_ensure_work_packages(project_name, work_packages, users=None, exact_subject_set=false)`:
  creates or updates exactly one work package per requested subject through
  OpenProject API v3 and returns readback evidence. Each work package must
  provide exact `subject`, `type`, `priority`, plain-text `description`, and
  may provide `assignee_login` or `assignee_name` when the task requires an
  assignee; `parent_subject`, `status`, dates, and estimated hours are optional.
  User entries require exact login/email/first/last names and roles;
  creating an active user also requires an explicit password. The helper
  ensures each assignee has project membership; use `assignee_roles` to
  override the default `Member` role.
- `openproject_query_work_packages(project_name, version_name=None, status_name=None, max_items=200)`:
  performs a read-only API query and returns compact normalized work packages
  with ID, subject, type, status, version, assignee, estimated hours, closed
  state, and plain description. Descriptions over 1000 characters are truncated
  with explicit length metadata.

Prefer this helper when the task requires structured OpenProject objects.
Both helpers accept either the exact project display name or exact project
identifier. Query existing work packages before extracting the same data by UI.
Use `assignee_login` when the task supplies a login and `assignee_name` when it
supplies a display name; never substitute an email address for either. Pass
parent work packages in the same request and refer to them by exact subject.
Descriptions are stored as plain text, so do not add Markdown escaping.
If an OpenProject helper reports that its circuit is open, do not call either
OpenProject helper again in the current task. Use the browser once or continue
with another subtask.
"""


METABASE_TOOL_RULES = """\
### Metabase Helper Tools
- `metabase_inspect_schema(table_names=None, sync=true)`: ensures the routed
  Baserow PostgreSQL connection, optionally synchronizes it, and returns exact
  persisted table and field metadata.
- `metabase_ensure_analytics(collection_name, questions, dashboard, sync=true)`:
  idempotently creates or updates the requested collection, semantic questions,
  dashboard, and dashboard cards, then executes every question and returns
  query/dashboard readback evidence.

Use `metabase_inspect_schema` after the source Baserow tables have been created.
Then use `metabase_ensure_analytics` with exact task-derived question and
dashboard specs. Qualify joined fields as `Table Name.Field Name`. Use
`aggregation_index` in `order_by` when sorting by an aggregate. Do not use the
Metabase UI to guess database hosts, credentials, table IDs, or field IDs.
Treat a tool call as complete only when query and dashboard readback both
succeed. This routed helper supports Metabase analytics over Baserow PostgreSQL;
it does not guess configuration for other data sources.
If a Metabase helper reports that its circuit is open, do not call either
Metabase helper again in the current task. Record the infrastructure blocker and
continue with another subtask.

Each question object uses `name`, `source_table`, and `display` (`table`, `bar`,
`pie`, `scatter`, or `scalar`). Optional keys are `columns`, `aggregations`
(`op` is `count`, `sum`, or `avg`), `breakouts`, one `joins` entry, `filters`,
`order_by`, `limit`, and `visualization` (`x_axis`, `y_axes`). The dashboard
object uses exact `name`, `description`, and `question_names`.
"""


TWENTY_READ_TOOL_RULES = """\
### Twenty Readback Tool
- `twenty_query_records(entity, exact_names=None, limit=200)` performs a
  read-only query of persisted Twenty `companies`, `people`,
  `opportunities`, `tasks`, or `notes` and returns exact-name matches.

Use this after a UI save when the grid still shows `Untitled`, inline editing is
uncertain, or the same field has already failed twice. If the exact requested
record is present, stop editing or creating duplicates. If it is missing, use
one genuinely different UI path or skip that subtask. This helper cannot create
or modify Twenty records and does not accept arbitrary SQL.
"""


TWENTY_WRITE_TOOL_RULES = """\
### Twenty Deterministic Write Tool
- `twenty_ensure_records(companies=None, people=None, opportunities=None, tasks=None, notes=None)`
  idempotently creates or updates a bounded Twenty CRM object graph through
  the authenticated Twenty API, resolves relations by exact business keys,
  and verifies every requested object against persisted PostgreSQL state.

Use this action only for objects the task explicitly asks you to create or
update. Pass only task-provided values. Never pass internal UUIDs. Companies
use exact name; people use primary email; opportunities use exact name plus
`company_name`; tasks use title, `due_at`, and optional `company_name`; notes
use exact title. A company may include explicit `favorite=true`.

Treat any `blocked`, `duplicates`, or `mismatches` entry as incomplete work.
Do not retry the UI after helper readback confirms the exact object and
relationships. Do not invent missing relation names, emails, fields, or values.
"""


BIGCAPITAL_QUERY_TOOL_RULES = """\
### BigCapital Customer Tools
- `bigcapital_query_customers(exact_names=None, limit=200)` returns exact
  customer readback from the authenticated BigCapital API.
"""


BIGCAPITAL_WRITE_TOOL_RULES = """\
### BigCapital Deterministic Customer Write Tool
- `bigcapital_ensure_customers(customers, currency_code=None)` idempotently
  creates or updates customers by exact `display_name`, then reads them back.

Use these tools when the BigCapital customer form is blocked by Company Name or
Blueprint Display Name controls. Pass only task-provided customer data. Each
customer requires `display_name`; supported optional fields are
`customer_type`, `company_name`, `first_name`, `last_name`, `email`,
`work_phone`, `personal_phone`, `website`, `note`, `active`, and `code`.
Do not retry the UI form after tool readback confirms the exact customer.
"""


def build_tools(
    apps: list[str] | tuple[str, ...],
    context: dict[str, Any] | None = None,
    *,
    description: str = "",
    mode: str | None = None,
    credentials: Mapping[str, Mapping[str, str]] | None = None,
    write_modes: Mapping[str, str] | None = None,
    tools_factory: Callable[..., Any] | None = None,
    baserow_client_cls: type[BaserowClient] = BaserowClient,
    code_server_client_cls: type[CodeServerClient] = CodeServerClient,
    openproject_client_cls: type[OpenProjectClient] = OpenProjectClient,
    metabase_client_cls: type[MetabaseClient] = MetabaseClient,
    twenty_client_cls: type[TwentyClient] = TwentyClient,
    twenty_write_client_cls: type[TwentyWriteClient] = TwentyWriteClient,
    bigcapital_client_cls: type[BigCapitalClient] = BigCapitalClient,
) -> tuple[Any, dict[str, Any]]:
    """Build browser-use tools for an explicit list of application names.

    Connection context is harness-independent::

        {
          "base_urls": {"baserow": "http://127.0.0.1:8001"},
          "container_names": {"code-server": "dev-code", "twenty": "crm"},
          "postgres": {"baserow": {"host": "db", "port": 5432}},
          "credentials": {"twenty": {"username": "...", "password": "..."}}
        }
    """

    context = context or {}
    tools = _new_tools(tools_factory)
    selected_apps = list(
        dict.fromkeys(str(app).strip().lower() for app in apps if str(app).strip())
    )
    selected_mode = (
        mode or os.environ.get("SAAS_AGENT_TOOL_MODE", "disabled")
    ).strip().lower() or "disabled"
    selected_write_modes = dict(context.get("write_modes") or {})
    selected_write_modes.update(write_modes or {})
    bigcapital_write_mode = str(
        selected_write_modes.get("bigcapital")
        or _configured_mode("SAAS_AGENT_BIGCAPITAL_WRITE_MODE", "off")
    ).strip().lower()
    twenty_write_mode = str(
        selected_write_modes.get("twenty")
        or _configured_mode("SAAS_AGENT_TWENTY_WRITE_MODE", "off")
    ).strip().lower()
    base_urls = dict(context.get("base_urls") or {})
    container_names = dict(context.get("container_names") or {})
    credential_map: dict[str, Mapping[str, str]] = dict(
        context.get("credentials") or {}
    )
    credential_map.update(credentials or {})
    meta: dict[str, Any] = {
        "mode": selected_mode,
        "apps": selected_apps,
        "app_tools": [],
        "actions": [],
        "missing_context": [],
        "base_urls": {},
        "write_modes": {
            "bigcapital": bigcapital_write_mode,
            "twenty": twenty_write_mode,
        },
    }
    if selected_mode in _DISABLED_MODES:
        meta["mode"] = "disabled"
        return tools, meta
    if selected_mode not in _ENABLED_MODES:
        meta["mode"] = "disabled"
        meta["missing_context"].append(
            f"unknown SAAS_AGENT_TOOL_MODE={selected_mode}"
        )
        return tools, meta

    meta["mode"] = "routing"
    if "baserow" in selected_apps:
        base_url = base_urls.get("baserow")
        if not base_url:
            meta["missing_context"].append("baserow base_url")
        else:
            meta["base_urls"]["baserow"] = base_url
            baserow_auth = dict(credential_map.get("baserow") or {})
            baserow_email = baserow_auth.get("email") or baserow_auth.get(
                "username"
            )
            _register_baserow_tool(
                tools,
                base_url,
                baserow_client_cls,
                client_kwargs={
                    key: value
                    for key, value in {
                        "email": baserow_email,
                        "password": baserow_auth.get("password"),
                    }.items()
                    if value
                },
            )
            meta["app_tools"].append("baserow")
            meta["actions"].append("baserow_ensure_table")

    if "code-server" in selected_apps:
        container_name = container_names.get("code-server") or os.environ.get(
            "CODE_SERVER_CONTAINER"
        )
        if not container_name:
            meta["missing_context"].append("code-server container")
        else:
            _register_code_server_tools(tools, container_name, code_server_client_cls)
            meta["app_tools"].append("code-server")
            meta["actions"].extend([
                "code_search_files",
                "code_read_files",
                "code_write_file",
                "code_exec",
                "code_run_python",
                "code_scan_docker_security",
                "code_scan_project_dependencies",
                "code_collect_test_metrics",
                "code_git_commit",
            ])

    if "openproject" in selected_apps:
        base_url = base_urls.get("openproject")
        if not base_url:
            meta["missing_context"].append("openproject base_url")
        else:
            meta["base_urls"]["openproject"] = base_url
            openproject_auth = dict(credential_map.get("openproject") or {})
            _register_openproject_tool(
                tools,
                base_url,
                openproject_client_cls,
                client_kwargs={
                    key: openproject_auth[key]
                    for key in ("username", "password", "api_key")
                    if openproject_auth.get(key)
                },
            )
            meta["app_tools"].append("openproject")
            meta["actions"].extend([
                "openproject_ensure_work_packages",
                "openproject_query_work_packages",
            ])

    if "twenty" in selected_apps:
        container_name = container_names.get("twenty")
        if not container_name:
            meta["missing_context"].append("twenty container")
        else:
            write_config = None
            if twenty_write_mode in _ENABLED_MODES:
                base_url = base_urls.get("twenty")
                if not base_url:
                    meta["missing_context"].append("twenty base_url")
                else:
                    try:
                        credential = _resolve_credential(
                            "twenty", credential_map, description
                        )
                    except TaskCredentialError as exc:
                        meta["missing_context"].append(
                            f"twenty credentials: {str(exc)}"
                        )
                        credential = None
                    if credential is None:
                        meta["missing_context"].append("twenty credentials")
                    else:
                        meta["base_urls"]["twenty"] = base_url
                        write_config = (
                            base_url,
                            credential.username,
                            credential.password,
                            twenty_write_client_cls,
                        )
            elif twenty_write_mode not in _DISABLED_MODES:
                meta["missing_context"].append(
                    f"unknown SAAS_AGENT_TWENTY_WRITE_MODE={twenty_write_mode}"
                )
            _register_twenty_tools(
                tools,
                container_name,
                twenty_client_cls,
                write_config=write_config,
            )
            meta["app_tools"].append("twenty")
            meta["actions"].append("twenty_query_records")
            if write_config is not None:
                meta["actions"].append("twenty_ensure_records")

    if "bigcapital" in selected_apps:
        base_url = base_urls.get("bigcapital")
        if not base_url:
            meta["missing_context"].append("bigcapital base_url")
        else:
            meta["base_urls"]["bigcapital"] = base_url
            bigcapital_auth = dict(credential_map.get("bigcapital") or {})
            bigcapital_email = bigcapital_auth.get("email") or bigcapital_auth.get(
                "username"
            )
            enable_write = bigcapital_write_mode in _ENABLED_MODES
            if bigcapital_write_mode not in _ENABLED_MODES | _DISABLED_MODES:
                meta["missing_context"].append(
                    f"unknown SAAS_AGENT_BIGCAPITAL_WRITE_MODE={bigcapital_write_mode}"
                )
            _register_bigcapital_tools(
                tools,
                base_url,
                bigcapital_client_cls,
                enable_write=enable_write,
                client_kwargs={
                    key: value
                    for key, value in {
                        "email": bigcapital_email,
                        "password": bigcapital_auth.get("password"),
                    }.items()
                    if value
                },
            )
            meta["app_tools"].append("bigcapital")
            meta["actions"].append("bigcapital_query_customers")
            if enable_write:
                meta["actions"].append("bigcapital_ensure_customers")

    metabase_mode = os.environ.get(
        "SAAS_AGENT_METABASE_TOOL_MODE", selected_mode
    ).strip().lower()
    metabase_enabled_modes = {"routing", "tools", "enabled", "1", "true"}
    metabase_disabled_modes = {"disabled", "off", "legacy", "0", "false"}
    if "metabase" in selected_apps and metabase_mode in metabase_enabled_modes:
        base_url = base_urls.get("metabase")
        baserow_pg = dict((context.get("postgres") or {}).get("baserow") or {})
        baserow_pg_host = baserow_pg.get("host")
        baserow_pg_port = baserow_pg.get("port")
        if not base_url:
            meta["missing_context"].append("metabase base_url")
        if not baserow_pg_host:
            meta["missing_context"].append("baserow postgres host")
        if not baserow_pg_port:
            meta["missing_context"].append("baserow postgres port")
        if base_url and baserow_pg_host and baserow_pg_port:
            meta["base_urls"]["metabase"] = base_url
            metabase_auth = dict(credential_map.get("metabase") or {})
            baserow_auth = dict(credential_map.get("baserow") or {})
            metabase_username = metabase_auth.get("username") or metabase_auth.get(
                "email"
            )
            baserow_api_email = baserow_auth.get("email") or baserow_auth.get(
                "username"
            )
            _register_metabase_tools(
                tools,
                base_url,
                str(baserow_pg_host),
                int(baserow_pg_port),
                base_urls.get("baserow"),
                metabase_client_cls,
                client_kwargs={
                    key: value
                    for key, value in {
                        "username": metabase_username,
                        "password": metabase_auth.get("password"),
                        "baserow_database": baserow_pg.get("database"),
                        "baserow_username": baserow_pg.get("username"),
                        "baserow_password": baserow_pg.get("password"),
                        "baserow_api_email": baserow_api_email,
                        "baserow_api_password": baserow_auth.get("password"),
                    }.items()
                    if value
                },
            )
            meta["app_tools"].append("metabase")
            meta["actions"].extend([
                "metabase_inspect_schema",
                "metabase_ensure_analytics",
            ])
    elif "metabase" in selected_apps and metabase_mode not in metabase_disabled_modes:
        meta["missing_context"].append(
            f"unknown SAAS_AGENT_METABASE_TOOL_MODE={metabase_mode}"
        )

    return tools, meta


def _resolve_credential(
    app: str,
    credentials: Mapping[str, Mapping[str, str]],
    description: str,
) -> TaskCredential | None:
    raw = credentials.get(app)
    if raw is not None:
        username = str(raw.get("username") or "").strip()
        password = str(raw.get("password") or "").strip()
        if not username or not password:
            raise TaskCredentialError(
                f"{app} credentials require non-empty username and password"
            )
        return TaskCredential(username=username, password=password)
    return parse_task_credential(description, app)


def build_tool_system_rules(meta: dict[str, Any] | None) -> str | None:
    """Return extra system rules for any routed helper actions."""

    actions = set((meta or {}).get("actions") or [])
    parts: list[str] = []
    if "baserow_ensure_table" in actions:
        parts.append(BASEROW_TOOL_RULES)
    if {
        "code_search_files",
        "code_read_files",
        "code_write_file",
        "code_exec",
        "code_run_python",
        "code_scan_docker_security",
        "code_scan_project_dependencies",
        "code_collect_test_metrics",
        "code_git_commit",
    } & actions:
        parts.append(CODE_SERVER_TOOL_RULES)
    if {
        "openproject_ensure_work_packages",
        "openproject_query_work_packages",
    } & actions:
        parts.append(OPENPROJECT_TOOL_RULES)
    if {
        "metabase_inspect_schema",
        "metabase_ensure_analytics",
    } & actions:
        parts.append(METABASE_TOOL_RULES)
    if "twenty_query_records" in actions:
        parts.append(TWENTY_READ_TOOL_RULES)
    if "twenty_ensure_records" in actions:
        parts.append(TWENTY_WRITE_TOOL_RULES)
    if "bigcapital_query_customers" in actions:
        parts.append(BIGCAPITAL_QUERY_TOOL_RULES)
    if "bigcapital_ensure_customers" in actions:
        parts.append(BIGCAPITAL_WRITE_TOOL_RULES)
    return "\n\n".join(parts) if parts else None


def _new_tools(tools_factory: Callable[..., Any] | None) -> Any:
    if tools_factory is None:
        from browser_use.tools.service import Tools

        tools_factory = Tools
    return tools_factory(exclude_actions=["evaluate"])


def _register_baserow_tool(
    tools: Any,
    base_url: str,
    baserow_client_cls: type[BaserowClient],
    *,
    client_kwargs: Mapping[str, Any] | None = None,
) -> None:
    client_kwargs = dict(client_kwargs or {})

    async def baserow_ensure_table(
        database_name: str,
        table_name: str,
        fields: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        views: list[dict[str, Any]] | None = None,
        replace_rows: bool = False,
    ) -> str:
        """Ensure a Baserow table, fields, rows, and optional views exist."""

        client = baserow_client_cls(base_url, **client_kwargs)
        result = await asyncio.to_thread(
            client.ensure_table,
            database_name=database_name,
            table_name=table_name,
            fields=fields,
            rows=rows,
            views=views,
            replace_rows=replace_rows,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    baserow_ensure_table.__name__ = "baserow_ensure_table"
    description = (
        "Create or update a Baserow database table using REST API. "
        "Arguments: database_name, table_name, fields, rows, optional views, "
        "optional replace_rows. "
        "Use this after you have determined the exact schema and row values "
        "from the task and source files."
    )
    _register_action(tools, "baserow_ensure_table", description, baserow_ensure_table)


def _register_code_server_tools(
    tools: Any,
    container_name: str,
    code_server_client_cls: type[CodeServerClient],
) -> None:
    def client() -> CodeServerClient:
        return code_server_client_cls(container_name)

    async def code_search_files(
        pattern: str,
        roots: list[str] | None = None,
        include_globs: list[str] | None = None,
        exclude_globs: list[str] | None = None,
        max_matches: int = 100,
        case_sensitive: bool = False,
    ) -> str:
        result = await asyncio.to_thread(
            client().search_files,
            pattern=pattern,
            roots=roots,
            include_globs=include_globs,
            exclude_globs=exclude_globs,
            max_matches=max_matches,
            case_sensitive=case_sensitive,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def code_read_files(paths: list[str], max_chars: int = 20_000) -> str:
        result = await asyncio.to_thread(
            client().read_files,
            paths=paths,
            max_chars=max_chars,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def code_write_file(path: str, content: str) -> str:
        result = await asyncio.to_thread(
            client().write_file,
            path=path,
            content=content,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def code_exec(
        command: str,
        cwd: str | None = None,
        timeout: int = 120,
        max_output: int = 20_000,
    ) -> str:
        result = await asyncio.to_thread(
            client().run_shell,
            command=command,
            cwd=cwd,
            timeout=timeout,
            max_output=max_output,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def code_run_python(
        script: str,
        cwd: str | None = None,
        timeout: int = 120,
        max_output: int = 20_000,
    ) -> str:
        result = await asyncio.to_thread(
            client().run_python,
            script=script,
            cwd=cwd,
            timeout=timeout,
            max_output=max_output,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def code_scan_docker_security(
        dockerfiles: dict[str, str],
        audit_date: str,
        finding_id_prefix: str = "HS",
    ) -> str:
        result = await asyncio.to_thread(
            client().scan_docker_security,
            dockerfiles=dockerfiles,
            audit_date=audit_date,
            finding_id_prefix=finding_id_prefix,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def code_scan_project_dependencies(
        projects: list[str],
        scan_roots: dict[str, str],
        ownership: dict[str, dict[str, str]],
    ) -> str:
        result = await asyncio.to_thread(
            client().scan_project_dependencies,
            projects=projects,
            scan_roots=scan_roots,
            ownership=ownership,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def code_collect_test_metrics(
        projects: list[dict[str, Any]],
    ) -> str:
        result = await asyncio.to_thread(
            client().collect_test_metrics,
            projects=projects,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def code_git_commit(
        repo_path: str,
        message: str,
        paths: list[str] | None = None,
    ) -> str:
        result = await asyncio.to_thread(
            client().git_commit,
            repo_path=repo_path,
            message=message,
            paths=paths,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    _register_action(
        tools,
        "code_search_files",
        "Search source files inside the code-server Docker container.",
        code_search_files,
    )
    _register_action(
        tools,
        "code_read_files",
        "Read source files inside the code-server Docker container.",
        code_read_files,
    )
    _register_action(
        tools,
        "code_write_file",
        "Write a durable file inside /home/coder/project in code-server.",
        code_write_file,
    )
    _register_action(
        tools,
        "code_exec",
        "Run a shell command inside the code-server Docker container.",
        code_exec,
    )
    _register_action(
        tools,
        "code_run_python",
        "Run Python code inside the code-server Docker container.",
        code_run_python,
    )
    _register_action(
        tools,
        "code_scan_docker_security",
        "Audit exact caller-supplied Dockerfiles and their hardcoded secrets.",
        code_scan_docker_security,
    )
    _register_action(
        tools,
        "code_scan_project_dependencies",
        "Scan project dependency edges under /home/coder/project.",
        code_scan_project_dependencies,
    )
    _register_action(
        tools,
        "code_collect_test_metrics",
        "Run explicit read-only test commands and collect aggregate test totals.",
        code_collect_test_metrics,
    )
    _register_action(
        tools,
        "code_git_commit",
        "Create a git commit in a repository inside /home/coder/project.",
        code_git_commit,
    )


def _register_openproject_tool(
    tools: Any,
    base_url: str,
    openproject_client_cls: type[OpenProjectClient],
    *,
    client_kwargs: Mapping[str, str] | None = None,
) -> None:
    circuit = _RepeatedFailureCircuit("OpenProject")
    client_kwargs = dict(client_kwargs or {})

    def make_client() -> OpenProjectClient:
        return openproject_client_cls(base_url, **client_kwargs)

    async def openproject_ensure_work_packages(
        project_name: str,
        work_packages: list[dict[str, Any]],
        users: list[dict[str, Any]] | None = None,
        exact_subject_set: bool = False,
    ) -> str:
        client = make_client()
        result = await asyncio.to_thread(
            circuit.call,
            client.ensure_work_packages,
            project_name=project_name,
            work_packages=work_packages,
            users=users,
            exact_subject_set=exact_subject_set,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def openproject_query_work_packages(
        project_name: str,
        version_name: str | None = None,
        status_name: str | None = None,
        max_items: int = 200,
    ) -> str:
        client = make_client()
        result = await asyncio.to_thread(
            circuit.call,
            client.query_work_packages,
            project_name=project_name,
            version_name=version_name,
            status_name=status_name,
            max_items=max_items,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    _register_action(
        tools,
        "openproject_ensure_work_packages",
        "Ensure exact OpenProject users, memberships, parents, and work packages.",
        openproject_ensure_work_packages,
    )
    _register_action(
        tools,
        "openproject_query_work_packages",
        "Read normalized OpenProject work packages without changing state.",
        openproject_query_work_packages,
    )


def _register_metabase_tools(
    tools: Any,
    base_url: str,
    baserow_host: str,
    baserow_port: int,
    baserow_api_url: str | None,
    metabase_client_cls: type[MetabaseClient],
    *,
    client_kwargs: Mapping[str, Any] | None = None,
) -> None:
    circuit = _RepeatedFailureCircuit("Metabase")
    client_kwargs = dict(client_kwargs or {})

    def make_client() -> MetabaseClient:
        kwargs: dict[str, Any] = {
            "baserow_host": baserow_host,
            "baserow_port": baserow_port,
            **client_kwargs,
        }
        if baserow_api_url:
            kwargs["baserow_api_url"] = baserow_api_url
        return metabase_client_cls(base_url, **kwargs)

    async def metabase_inspect_schema(
        table_names: list[str] | None = None,
        sync: bool = True,
    ) -> str:
        client = make_client()
        result = await asyncio.to_thread(
            circuit.call,
            client.inspect_schema,
            table_names,
            sync=sync,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    async def metabase_ensure_analytics(
        collection_name: str,
        questions: list[dict[str, Any]],
        dashboard: dict[str, Any],
        sync: bool = True,
    ) -> str:
        client = make_client()
        result = await asyncio.to_thread(
            circuit.call,
            client.ensure_analytics,
            collection_name,
            questions,
            dashboard,
            sync=sync,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    _register_action(
        tools,
        "metabase_inspect_schema",
        "Inspect synchronized Baserow PostgreSQL tables and fields in Metabase.",
        metabase_inspect_schema,
    )
    _register_action(
        tools,
        "metabase_ensure_analytics",
        "Ensure semantic Metabase questions, dashboard cards, and query readback.",
        metabase_ensure_analytics,
    )


def _register_twenty_tools(
    tools: Any,
    container_name: str,
    twenty_client_cls: type[TwentyClient],
    *,
    write_config: tuple[
        str, str, str, type[TwentyWriteClient]
    ] | None = None,
) -> None:
    circuit = _RepeatedFailureCircuit("twenty")

    async def twenty_query_records(
        entity: str,
        exact_names: list[str] | None = None,
        limit: int = 200,
    ) -> str:
        client = twenty_client_cls(container_name)
        result = await asyncio.to_thread(
            circuit.call,
            client.query_records,
            entity,
            exact_names,
            limit,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    _register_action(
        tools,
        "twenty_query_records",
        "Read persisted Twenty CRM records by whitelisted entity and exact names.",
        twenty_query_records,
    )

    if write_config is not None:
        base_url, email, password, write_client_cls = write_config

        async def twenty_ensure_records(
            companies: list[dict[str, Any]] | None = None,
            people: list[dict[str, Any]] | None = None,
            opportunities: list[dict[str, Any]] | None = None,
            tasks: list[dict[str, Any]] | None = None,
            notes: list[dict[str, Any]] | None = None,
        ) -> str:
            read_client = twenty_client_cls(container_name)
            client = write_client_cls(
                base_url,
                email,
                password,
                read_client,
            )
            result = await asyncio.to_thread(
                circuit.call,
                client.ensure_records,
                companies,
                people,
                opportunities,
                tasks,
                notes,
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)

        _register_action(
            tools,
            "twenty_ensure_records",
            "Idempotently write exact Twenty CRM objects through its API and verify SQL readback.",
            twenty_ensure_records,
        )


def _register_bigcapital_tools(
    tools: Any,
    base_url: str,
    bigcapital_client_cls: type[BigCapitalClient],
    *,
    enable_write: bool,
    client_kwargs: Mapping[str, Any] | None = None,
) -> None:
    circuit = _RepeatedFailureCircuit("bigcapital")
    client_kwargs = dict(client_kwargs or {})

    async def bigcapital_query_customers(
        exact_names: list[str] | None = None,
        limit: int = 200,
    ) -> str:
        client = bigcapital_client_cls(base_url, **client_kwargs)
        result = await asyncio.to_thread(
            circuit.call,
            client.query_customers,
            exact_names,
            limit,
        )
        return json.dumps(result, ensure_ascii=False, sort_keys=True)

    _register_action(
        tools,
        "bigcapital_query_customers",
        "Query BigCapital customers by exact display name through the authenticated API.",
        bigcapital_query_customers,
    )
    if enable_write:
        async def bigcapital_ensure_customers(
            customers: list[dict[str, Any]],
            currency_code: str | None = None,
        ) -> str:
            client = bigcapital_client_cls(base_url, **client_kwargs)
            result = await asyncio.to_thread(
                circuit.call,
                client.ensure_customers,
                customers,
                currency_code,
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)

        _register_action(
            tools,
            "bigcapital_ensure_customers",
            "Idempotently create or update exact BigCapital customers and return readback.",
            bigcapital_ensure_customers,
        )


def _configured_mode(name: str, default: str) -> str:
    return os.environ.get(name, default).strip().lower() or default


def _register_action(tools: Any, name: str, description: str, func: Callable[..., Any]) -> None:
    errors: list[str] = []
    if hasattr(tools, "action"):
        action = getattr(tools, "action")
        for make_decorator in (
            lambda: action(description, name=name),
            lambda: action(description),
            lambda: action(name=name, description=description),
        ):
            try:
                decorator = make_decorator()
                decorator(func)
                return
            except TypeError as exc:
                errors.append(str(exc))

    if hasattr(tools, "register_action"):
        register_action = getattr(tools, "register_action")
        for kwargs in (
            {"name": name, "func": func, "description": description},
            {"action": func, "name": name, "description": description},
        ):
            try:
                register_action(**kwargs)
                return
            except TypeError as exc:
                errors.append(str(exc))

    raise RuntimeError(
        f"Could not register tool action {name}. "
        f"Unsupported browser-use Tools API. Errors: {errors}"
    )
