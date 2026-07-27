from pathlib import Path

from scripts.audit_public_tree import audit_tree


def _rules(findings):
    return {(item.severity, item.rule, item.path) for item in findings}


def test_clean_tree_passes(tmp_path: Path):
    (tmp_path / "README.md").write_text("# Clean\n", encoding="utf-8")

    assert audit_tree(tmp_path) == []


def test_reports_secret_path_without_secret_value(tmp_path: Path):
    secret = "sk-" + ("a" * 24)
    path = tmp_path / "config.py"
    path.write_text(f'API_TOKEN = "{secret}"\n', encoding="utf-8")

    findings = audit_tree(tmp_path)

    assert ("error", "provider-token", "config.py") in _rules(findings)
    assert all(secret not in item.path for item in findings)


def test_rejects_raw_results_and_archives(tmp_path: Path):
    result = tmp_path / "results" / "model" / "task_r0.json"
    result.parent.mkdir(parents=True)
    result.write_text("{}", encoding="utf-8")
    archive = tmp_path / "bundle.tgz"
    archive.write_bytes(b"not an archive")

    findings = _rules(audit_tree(tmp_path))

    assert ("error", "raw-result-artifact", "results/model/task_r0.json") in findings
    assert ("error", "archive-artifact", "bundle.tgz") in findings


def test_allows_env_example_and_code_server_workspace_path(tmp_path: Path):
    (tmp_path / ".env.example").write_text(
        "SAAS_AGENT_LLM_API_KEY=replace-me\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "Use /home/coder/project for durable workspace files.\n",
        encoding="utf-8",
    )

    assert audit_tree(tmp_path) == []


def test_rejects_hardcoded_application_password_in_source(tmp_path: Path):
    source = tmp_path / "src" / "package" / "client.py"
    source.parent.mkdir(parents=True)
    source.write_text('APP_PASSWORD = "real-deployment-password"\n', encoding="utf-8")

    findings = _rules(audit_tree(tmp_path))

    assert ("error", "hardcoded-app-secret", "src/package/client.py") in findings


def test_release_placeholder_is_warning(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        "git clone <future-public-repository-url>\n",
        encoding="utf-8",
    )

    findings = audit_tree(tmp_path)

    assert findings == [
        type(findings[0])("warning", "release-placeholder", "README.md")
    ]
