import json

import pytest

from saas_agent.code_server_helper import (
    CodeServerClient,
    CodeServerError,
    CommandResult,
    _CONTAINER_SCRIPT,
)
from saas_agent.dependency_scan import extract_module_references
from saas_agent.test_metrics import parse_test_output


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *, input_text=None, timeout=None):
        self.calls.append({"cmd": cmd, "input_text": input_text, "timeout": timeout})
        assert cmd[:4] == ["docker", "exec", "-i", "rollout_0_code-server"]
        payload = json.loads(input_text)
        op = payload["op"]
        if op == "search":
            return CommandResult(
                0,
                json.dumps({
                    "matches": [
                        {
                            "path": "/home/coder/project/todo-api/app.py",
                            "line_number": 12,
                            "line": "@app.route('/categories')",
                        }
                    ],
                    "truncated": False,
                }),
                "",
            )
        if op == "read":
            return CommandResult(
                0,
                json.dumps({
                    "files": [
                        {
                            "path": "/home/coder/project/todo-api/app.py",
                            "content": "print('ok')\n",
                            "truncated": False,
                        }
                    ]
                }),
                "",
            )
        if op == "write":
            return CommandResult(
                0,
                json.dumps({
                    "path": "/home/coder/project/devops-configs/docs/report.md",
                    "bytes": 12,
                }),
                "",
            )
        if op == "exec":
            return CommandResult(
                0,
                json.dumps({
                    "cwd": "/home/coder/project/todo-api",
                    "command": payload["command"],
                    "returncode": 0,
                    "stdout": "ok\n",
                    "stderr": "",
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "timed_out": False,
                }),
                "",
            )
        if op == "run_python":
            return CommandResult(
                0,
                json.dumps({
                    "cwd": "/home/coder/project",
                    "returncode": 0,
                    "stdout": "42\n",
                    "stderr": "",
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "timed_out": False,
                }),
                "",
            )
        if op == "scan_docker_security":
            return CommandResult(
                0,
                json.dumps({
                    "root": "/home/coder/project",
                    "audit_rows": [{"Audit ID": "DA-01", "Service": "todo-api"}],
                    "secret_rows": [{"Finding ID": "HS-001", "Severity": "Critical"}],
                    "summary": {"services": 1, "secrets": 1},
                }),
                "",
            )
        if op == "scan_project_dependencies":
            return CommandResult(
                0,
                json.dumps({
                    "root": "/home/coder/project",
                    "team_ownership": [{"Project": "todo-api", "Owning Team": "Backend"}],
                    "dependency_edges": [{"Edge ID": "DE-001", "Cross Team": True}],
                    "summary": {"projects": 1, "edges": 1, "cross_team_edges": 1},
                }),
                "",
            )
        if op == "collect_test_metrics":
            return CommandResult(
                0,
                json.dumps({
                    "projects": [
                        {
                            "project": "data-analyzer",
                            "command": "pytest tests/ -v",
                            "returncode": 0,
                            "test_files_count": 3,
                            "metrics": {
                                "passed": 8,
                                "failed": 0,
                                "total": 8,
                                "pass_rate": 100.0,
                            },
                            "source_modified": False,
                            "blocker": None,
                        }
                    ]
                }),
                "",
            )
        if op == "git_commit":
            return CommandResult(
                0,
                json.dumps({
                    "repo_path": "/home/coder/project/devops-configs",
                    "committed": True,
                    "commit": "abc1234",
                    "message": "audit: report",
                }),
                "",
            )
        raise AssertionError(f"unexpected op {op}")


def test_container_script_is_valid_python():
    compile(_CONTAINER_SCRIPT, "<code-server-helper>", "exec")


def test_embedded_dependency_and_test_parsers_match_host_modules():
    definitions = _CONTAINER_SCRIPT.split("payload = json.load(sys.stdin)", 1)[0]
    namespace = {}
    exec(compile(definitions, "<code-server-helper-definitions>", "exec"), namespace)

    dependency_lines = [
        "import json, todo_api as api",
        "default_type application/json;",
        "const client = require('@acme/tabler/core')",
    ]
    for line in dependency_lines:
        assert namespace["extract_module_references"](line) == extract_module_references(line)

    metric_samples = [
        ("2 failed, 18 passed, 1 skipped in 4.2s", "pytest"),
        ("90% tests passed, 2 tests failed out of 20", "ctest"),
        ("Tests: 1 failed, 9 passed, 10 total", "jest"),
        ("build directory does not exist", "auto"),
    ]
    for output, parser in metric_samples:
        assert namespace["parse_test_output"](output, parser) == parse_test_output(
            output,
            parser,
        )


def test_search_files_uses_docker_exec_payload():
    runner = FakeRunner()
    client = CodeServerClient("rollout_0_code-server", runner=runner)

    result = client.search_files(
        pattern=r"@app\.route",
        roots=["todo-api"],
        include_globs=["*.py"],
    )

    payload = json.loads(runner.calls[0]["input_text"])
    assert payload["op"] == "search"
    assert payload["roots"] == ["todo-api"]
    assert payload["include_globs"] == ["*.py"]
    assert result["matches"][0]["path"].endswith("todo-api/app.py")


def test_read_files_returns_file_contents():
    runner = FakeRunner()
    client = CodeServerClient("rollout_0_code-server", runner=runner)

    result = client.read_files(["todo-api/app.py"])

    payload = json.loads(runner.calls[0]["input_text"])
    assert payload["op"] == "read"
    assert payload["paths"] == ["todo-api/app.py"]
    assert result["files"][0]["content"] == "print('ok')\n"


def test_write_file_defaults_to_project_root():
    runner = FakeRunner()
    client = CodeServerClient("rollout_0_code-server", runner=runner)

    result = client.write_file("devops-configs/docs/report.md", "hello world\n")

    payload = json.loads(runner.calls[0]["input_text"])
    assert payload["op"] == "write"
    assert payload["path"] == "devops-configs/docs/report.md"
    assert payload["default_root"] == "/home/coder/project"
    assert result["path"] == "/home/coder/project/devops-configs/docs/report.md"


def test_git_commit_stages_paths_and_returns_commit():
    runner = FakeRunner()
    client = CodeServerClient("rollout_0_code-server", runner=runner)

    result = client.git_commit(
        repo_path="devops-configs",
        message="audit: report",
        paths=["docs/report.md"],
    )

    payload = json.loads(runner.calls[0]["input_text"])
    assert payload["op"] == "git_commit"
    assert payload["repo_path"] == "devops-configs"
    assert payload["paths"] == ["docs/report.md"]
    assert result["committed"] is True


def test_run_shell_uses_project_default_and_operation_timeout():
    runner = FakeRunner()
    client = CodeServerClient("rollout_0_code-server", runner=runner, timeout=60)

    result = client.run_shell("python3 -V", cwd="todo-api", timeout=120, max_output=1234)

    payload = json.loads(runner.calls[0]["input_text"])
    assert payload["op"] == "exec"
    assert payload["command"] == "python3 -V"
    assert payload["cwd"] == "todo-api"
    assert payload["timeout"] == 120
    assert payload["max_output"] == 1234
    assert runner.calls[0]["timeout"] == 125
    assert result["stdout"] == "ok\n"


def test_run_python_uses_project_default():
    runner = FakeRunner()
    client = CodeServerClient("rollout_0_code-server", runner=runner)

    result = client.run_python("print(6 * 7)")

    payload = json.loads(runner.calls[0]["input_text"])
    assert payload["op"] == "run_python"
    assert payload["script"] == "print(6 * 7)"
    assert payload["cwd"] == "/home/coder/project"
    assert result["stdout"] == "42\n"


def test_scan_docker_security_uses_project_root_and_compact_rows():
    runner = FakeRunner()
    client = CodeServerClient("rollout_0_code-server", runner=runner)

    result = client.scan_docker_security(
        dockerfiles={"todo-api": "todo-api/Dockerfile.prod"},
        audit_date="2026-04-12",
        finding_id_prefix="HS",
    )

    payload = json.loads(runner.calls[0]["input_text"])
    assert payload["op"] == "scan_docker_security"
    assert payload["root"] == "/home/coder/project"
    assert payload["dockerfiles"] == {"todo-api": "todo-api/Dockerfile.prod"}
    assert payload["audit_date"] == "2026-04-12"
    assert payload["finding_id_prefix"] == "HS"
    assert result["summary"]["secrets"] == 1
    assert result["secret_rows"][0]["Severity"] == "Critical"


def test_scan_docker_security_requires_caller_inputs():
    client = CodeServerClient("rollout_0_code-server", runner=FakeRunner())

    with pytest.raises(CodeServerError, match="dockerfiles is required"):
        client.scan_docker_security({}, "2026-04-12")
    with pytest.raises(CodeServerError, match="audit_date is required"):
        client.scan_docker_security({"todo-api": "todo-api/Dockerfile"}, "")


def test_docker_scanner_has_no_fixed_services_or_date():
    assert "blog-engine" not in _CONTAINER_SCRIPT
    assert "2026-04-12" not in _CONTAINER_SCRIPT


def test_embedded_docker_scanner_uses_only_supplied_paths(tmp_path):
    definitions = _CONTAINER_SCRIPT.split("payload = json.load(sys.stdin)", 1)[0]
    namespace = {}
    exec(compile(definitions, "<code-server-helper-definitions>", "exec"), namespace)

    selected = tmp_path / "service-a" / "Dockerfile.prod"
    selected.parent.mkdir()
    selected.write_text(
        "FROM python:3.11\n"
        "USER app\n"
        "HEALTHCHECK CMD true\n"
        'ENV API_KEY="abcd1234"\n'
        "ENV SECRET='hiddenvalue'\n"
        "ENV TOKEN=unquoted-helper-only\n",
        encoding="utf-8",
    )
    ignored = tmp_path / "service-b" / "Dockerfile"
    ignored.parent.mkdir()
    ignored.write_text("FROM alpine\nENV PASSWORD=ignoreme\n", encoding="utf-8")

    captured = []
    namespace["emit"] = captured.append
    namespace["ensure_project_bridge"] = lambda: None

    def fake_resolve(path, default_root, must_exist=False, want_dir=False):
        if path == "/home/coder/project":
            return str(tmp_path)
        candidate = tmp_path / path
        assert not must_exist or candidate.exists()
        assert not want_dir or candidate.is_dir()
        return str(candidate)

    namespace["resolve"] = fake_resolve
    namespace["op_scan_docker_security"]({
        "root": "/home/coder/project",
        "dockerfiles": {"service-a": "service-a/Dockerfile.prod"},
        "audit_date": "2026-07-11",
        "finding_id_prefix": "HS",
    })

    result = captured[-1]
    assert [row["Service"] for row in result["audit_rows"]] == ["service-a"]
    assert [row["Finding ID"] for row in result["secret_rows"]] == [
        "HS-001",
        "HS-002",
    ]
    assert {row["Pattern Name"] for row in result["secret_rows"]} == {
        "APIToken",
        "Other",
    }
    assert all(row["Service"] == "service-a" for row in result["secret_rows"])
    evidence = result["secret_scan_evidence"]
    assert evidence["task_regex_matches"] == 2
    assert evidence["helper_regex_matches"] == 3
    encoded_evidence = json.dumps(evidence)
    assert "abcd1234" not in encoded_evidence
    assert "hiddenvalue" not in encoded_evidence
    assert "[redacted]" in encoded_evidence


def test_scan_project_dependencies_returns_baserow_ready_rows():
    runner = FakeRunner()
    client = CodeServerClient("rollout_0_code-server", runner=runner)

    result = client.scan_project_dependencies(
        projects=["todo-api"],
        scan_roots={"todo-api": "todo-api/app"},
        ownership={
            "todo-api": {"Owning Team": "Backend", "Tech Lead": "Grace Patel"}
        },
    )

    payload = json.loads(runner.calls[0]["input_text"])
    assert payload["op"] == "scan_project_dependencies"
    assert payload["root"] == "/home/coder/project"
    assert payload["projects"] == ["todo-api"]
    assert payload["scan_roots"] == {"todo-api": "todo-api/app"}
    assert payload["ownership"]["todo-api"]["Owning Team"] == "Backend"
    assert result["team_ownership"][0]["Owning Team"] == "Backend"
    assert result["dependency_edges"][0]["Edge ID"] == "DE-001"


def test_scan_project_dependencies_requires_explicit_metadata():
    runner = FakeRunner()
    client = CodeServerClient("rollout_0_code-server", runner=runner)

    with pytest.raises(CodeServerError, match="projects is required"):
        client.scan_project_dependencies(
            projects=[],
            scan_roots={},
            ownership={},
        )


def test_collect_test_metrics_uses_exact_explicit_commands():
    runner = FakeRunner()
    client = CodeServerClient("rollout_0_code-server", runner=runner)
    projects = [{
        "project": "data-analyzer",
        "path": "data-analyzer",
        "command": "pytest tests/ -v",
        "parser": "pytest",
        "test_globs": ["tests/**/*.py"],
    }]

    result = client.collect_test_metrics(projects)

    payload = json.loads(runner.calls[0]["input_text"])
    assert payload["op"] == "collect_test_metrics"
    assert payload["projects"] == projects
    assert result["projects"][0]["source_modified"] is False


def test_collect_test_metrics_requires_projects():
    client = CodeServerClient("rollout_0_code-server", runner=FakeRunner())

    with pytest.raises(CodeServerError, match="projects is required"):
        client.collect_test_metrics([])


def test_generic_shell_execution_preserves_pipeline_failures():
    from saas_agent import code_server_helper

    assert '["bash", "-o", "pipefail", "-lc", command]' in (
        code_server_helper._CONTAINER_SCRIPT
    )


def test_command_failure_is_reported():
    def bad_runner(cmd, *, input_text=None, timeout=None):
        return CommandResult(1, "", "boom")

    client = CodeServerClient("rollout_0_code-server", runner=bad_runner)

    with pytest.raises(CodeServerError, match="boom"):
        client.read_files(["missing.py"])
