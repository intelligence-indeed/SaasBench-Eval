"""Docker-backed helper for durable code-server file operations."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_PROJECT_ROOT = "/home/coder/project"
DEFAULT_WORKSPACE_ROOT = "/home/coder/workspace"


class CodeServerError(RuntimeError):
    """Raised when a code-server helper operation cannot be completed."""


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CommandResult]


_CONTAINER_SCRIPT = r"""
import fnmatch
import glob
import hashlib
import json
import os
import re
import subprocess
import sys

ALLOWED_ROOTS = ["/home/coder/project", "/home/coder/workspace", "/home/coder"]
SKIP_DIRS = {".git", "node_modules", ".next", "dist", "build", "coverage", "__pycache__", ".venv", "venv"}
PROJECT_ROOT = "/home/coder/project"
WORKSPACE_ROOT = "/home/coder/workspace"


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False))


def fail(message):
    emit({"error": message})
    sys.exit(2)


def is_under(path, root):
    return path == root or path.startswith(root + os.sep)


def check_allowed(path):
    if not any(is_under(path, root) for root in ALLOWED_ROOTS):
        fail(f"path outside allowed roots: {path}")
    return path


def candidate_paths(path, default_root):
    path = path or "."
    if os.path.isabs(path):
        return [os.path.normpath(path)]
    roots = [default_root] + [root for root in ALLOWED_ROOTS if root != default_root]
    seen = set()
    result = []
    for root in roots:
        candidate = os.path.normpath(os.path.join(root, path))
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def ensure_project_bridge():
    # Expose workspace projects under the stable /home/coder/project root.
    try:
        os.makedirs(PROJECT_ROOT, exist_ok=True)
        if not os.path.isdir(WORKSPACE_ROOT):
            return []
        linked = []
        for name in os.listdir(WORKSPACE_ROOT):
            if name.startswith("."):
                continue
            source = os.path.join(WORKSPACE_ROOT, name)
            target = os.path.join(PROJECT_ROOT, name)
            if not os.path.isdir(source) or os.path.exists(target):
                continue
            os.symlink(source, target)
            linked.append(name)
        return linked
    except OSError:
        return []


def resolve(path, default_root, must_exist=False, want_dir=False):
    candidates = [check_allowed(path) for path in candidate_paths(path, default_root)]
    if must_exist:
        for candidate in candidates:
            if want_dir and os.path.isdir(candidate):
                return candidate
            if not want_dir and os.path.exists(candidate):
                return candidate
        fail(f"path not found: {path}")
    return candidates[0]


def is_text_file(path, max_probe=4096):
    try:
        with open(path, "rb") as f:
            chunk = f.read(max_probe)
        return b"\x00" not in chunk
    except OSError:
        return False


def rel_for_match(path):
    for root in ALLOWED_ROOTS:
        if is_under(path, root):
            return os.path.relpath(path, root)
    return path


def glob_allowed(path, include_globs, exclude_globs):
    rel = rel_for_match(path)
    name = os.path.basename(path)
    if include_globs:
        if not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) for pat in include_globs):
            return False
    if exclude_globs:
        if any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat) for pat in exclude_globs):
            return False
    return True


def op_search(payload):
    ensure_project_bridge()
    roots = payload.get("roots") or ["."]
    default_root = payload.get("default_root") or "/home/coder/project"
    include_globs = payload.get("include_globs") or []
    exclude_globs = payload.get("exclude_globs") or []
    max_matches = int(payload.get("max_matches") or 100)
    max_file_bytes = int(payload.get("max_file_bytes") or 2_000_000)
    flags = 0 if payload.get("case_sensitive") else re.IGNORECASE
    regex = re.compile(payload["pattern"], flags)

    matches = []
    truncated = False
    searched_roots = []
    for root in roots:
        root_path = resolve(root, default_root, must_exist=True, want_dir=True)
        searched_roots.append(root_path)
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                if not glob_allowed(path, include_globs, exclude_globs):
                    continue
                try:
                    if os.path.getsize(path) > max_file_bytes or not is_text_file(path):
                        continue
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        for line_no, line in enumerate(f, start=1):
                            if regex.search(line):
                                matches.append({
                                    "path": path,
                                    "line_number": line_no,
                                    "line": line.rstrip("\n"),
                                })
                                if len(matches) >= max_matches:
                                    truncated = True
                                    emit({"matches": matches, "truncated": truncated, "searched_roots": searched_roots})
                                    return
                except OSError:
                    continue
    emit({"matches": matches, "truncated": truncated, "searched_roots": searched_roots})


def op_read(payload):
    ensure_project_bridge()
    default_root = payload.get("default_root") or "/home/coder/project"
    max_chars = int(payload.get("max_chars") or 20_000)
    files = []
    errors = []
    for raw_path in payload.get("paths") or []:
        try:
            path = resolve(raw_path, default_root, must_exist=True)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_chars + 1)
            files.append({
                "path": path,
                "content": content[:max_chars],
                "truncated": len(content) > max_chars,
            })
        except SystemExit:
            raise
        except Exception as exc:
            errors.append({"path": raw_path, "error": str(exc)})
    emit({"files": files, "errors": errors})


def op_write(payload):
    ensure_project_bridge()
    default_root = payload.get("default_root") or "/home/coder/project"
    path = resolve(payload["path"], default_root, must_exist=False)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = payload.get("content") or ""
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    emit({"path": path, "bytes": len(content.encode("utf-8"))})


def trim_output(text, max_chars):
    text = text or ""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def op_exec(payload):
    ensure_project_bridge()
    default_root = payload.get("default_root") or "/home/coder/project"
    cwd = resolve(payload.get("cwd") or "/home/coder/project", default_root, must_exist=True, want_dir=True)
    command = payload.get("command")
    if not command or not str(command).strip():
        fail("command is required")
    timeout = int(payload.get("timeout") or 120)
    max_output = int(payload.get("max_output") or 20_000)
    try:
        completed = subprocess.run(
            ["bash", "-o", "pipefail", "-lc", command],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        stdout, stdout_truncated = trim_output(completed.stdout, max_output)
        stderr, stderr_truncated = trim_output(completed.stderr, max_output)
        emit({
            "cwd": cwd,
            "command": command,
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "timed_out": False,
        })
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = trim_output(exc.stdout if isinstance(exc.stdout, str) else "", max_output)
        stderr_text = exc.stderr if isinstance(exc.stderr, str) else ""
        stderr, stderr_truncated = trim_output(
            (stderr_text + ("\n" if stderr_text else "") + f"timed out after {timeout}s"),
            max_output,
        )
        emit({
            "cwd": cwd,
            "command": command,
            "returncode": -9,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "timed_out": True,
        })


def op_run_python(payload):
    ensure_project_bridge()
    default_root = payload.get("default_root") or "/home/coder/project"
    cwd = resolve(payload.get("cwd") or "/home/coder/project", default_root, must_exist=True, want_dir=True)
    script = payload.get("script")
    if not script or not str(script).strip():
        fail("script is required")
    timeout = int(payload.get("timeout") or 120)
    max_output = int(payload.get("max_output") or 20_000)
    try:
        completed = subprocess.run(
            ["python3", "-"],
            input=script,
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        stdout, stdout_truncated = trim_output(completed.stdout, max_output)
        stderr, stderr_truncated = trim_output(completed.stderr, max_output)
        emit({
            "cwd": cwd,
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "timed_out": False,
        })
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = trim_output(exc.stdout if isinstance(exc.stdout, str) else "", max_output)
        stderr_text = exc.stderr if isinstance(exc.stderr, str) else ""
        stderr, stderr_truncated = trim_output(
            (stderr_text + ("\n" if stderr_text else "") + f"timed out after {timeout}s"),
            max_output,
        )
        emit({
            "cwd": cwd,
            "returncode": -9,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "timed_out": True,
        })


def parse_dockerfile(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None

    from_lines = []
    user = None
    has_healthcheck = False
    run_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        upper = stripped.upper()
        if upper.startswith("FROM "):
            from_lines.append(stripped.split(None, 1)[1].split(" AS ", 1)[0].split(" as ", 1)[0].strip())
        elif upper.startswith("USER "):
            user = stripped.split(None, 1)[1].strip()
        elif upper.startswith("HEALTHCHECK "):
            has_healthcheck = True
        elif upper.startswith("RUN "):
            run_count += 1

    base_image = from_lines[0] if from_lines else ""
    image_name = base_image.split("@", 1)[0]
    tag = image_name.rsplit(":", 1)[1] if ":" in image_name.rsplit("/", 1)[-1] else ""
    uses_latest = tag in {"", "latest"}
    runs_as_root = user is None or user in {"root", "0"}
    multistage = len(from_lines) > 1
    score = 100
    if uses_latest:
        score -= 25
    if runs_as_root:
        score -= 25
    if not has_healthcheck:
        score -= 15
    if not multistage:
        score -= 10
    score -= 5 * max(0, run_count - 6)
    return {
        "base_image": base_image,
        "uses_latest_tag": uses_latest,
        "runs_as_root": runs_as_root,
        "has_healthcheck": has_healthcheck,
        "multistage_build": multistage,
        "run_instruction_count": run_count,
        "compliance_score": max(0, score),
    }


SECRET_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}|(?:aws_secret|api[_-]?key|token|passwd|password|secret)\s*(?:[:=]|\s+)\s*['\"]?[^'\"\s]{4,}['\"]?)",
    re.IGNORECASE,
)

TASK_SECRET_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}|(?:aws_secret|api[_-]?key|token|passwd|password|secret)"
    r"\s*[:=]\s*['\"][^'\"]{4,}['\"])",
    re.IGNORECASE,
)


def classify_secret(text):
    lower = text.lower()
    if text.startswith("AKIA"):
        return "AWSKey", "Critical"
    if "api" in lower and "key" in lower:
        return "APIToken", "Critical"
    if "token" in lower:
        return "APIToken", "Critical"
    if "password" in lower or "passwd" in lower:
        return "Password", "High"
    return "Other", "High"


def redact_secret_match(text):
    if text.upper().startswith("AKIA"):
        return "AKIA...[redacted]"
    key = re.match(r"\s*([A-Za-z0-9_-]+)", text)
    return f"{key.group(1) if key else 'secret'}=[redacted]"


def op_scan_docker_security(payload):
    ensure_project_bridge()
    root = resolve(payload.get("root") or "/home/coder/project", "/home/coder/project", must_exist=True, want_dir=True)
    dockerfiles = payload.get("dockerfiles") or {}
    audit_date = str(payload.get("audit_date") or "").strip()
    finding_id_prefix = str(payload.get("finding_id_prefix") or "HS").strip()
    if not isinstance(dockerfiles, dict) or not dockerfiles:
        fail("dockerfiles is required")
    if not audit_date:
        fail("audit_date is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", finding_id_prefix):
        fail("finding_id_prefix must contain only letters, digits, underscores, or hyphens")

    resolved_dockerfiles = []
    for raw_service, raw_path in dockerfiles.items():
        service = str(raw_service or "").strip()
        supplied_path = str(raw_path or "").strip()
        if not service or not supplied_path:
            fail("each dockerfiles entry requires a non-empty service and path")
        path = resolve(supplied_path, root, must_exist=True, want_dir=False)
        if not os.path.isfile(path):
            fail(f"Dockerfile path is not a file: {supplied_path}")
        resolved_dockerfiles.append((service, path))
    resolved_dockerfiles.sort(key=lambda item: (item[0], item[1]))

    audit_rows = []
    for idx, (service, path) in enumerate(resolved_dockerfiles, start=1):
        parsed = parse_dockerfile(path)
        if not parsed:
            continue
        rel_path = os.path.relpath(path, root)
        audit_rows.append({
            "Audit ID": f"DA-{idx:02d}",
            "Service": service,
            "Dockerfile Path": rel_path,
            "Base Image": parsed["base_image"],
            "Uses Latest Tag": parsed["uses_latest_tag"],
            "Runs As Root": parsed["runs_as_root"],
            "Has Healthcheck": parsed["has_healthcheck"],
            "Multistage Build": parsed["multistage_build"],
            "Run Instruction Count": parsed["run_instruction_count"],
            "Captured At": audit_date,
            "Compliance Score": parsed["compliance_score"],
        })

    secret_findings = []
    task_regex_evidence = []
    secret_evidence = []
    for service, path in resolved_dockerfiles:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, start=1):
                    for match in TASK_SECRET_RE.finditer(line):
                        pattern_name, severity = classify_secret(match.group(1))
                        finding = {
                            "Service": service,
                            "File Path": os.path.relpath(path, root),
                            "Line Number": lineno,
                            "Pattern Name": pattern_name,
                            "Severity": severity,
                            "Detected At": audit_date,
                        }
                        secret_findings.append(finding)
                        task_regex_evidence.append({
                            **{key: finding[key] for key in (
                                "Service", "File Path", "Line Number",
                                "Pattern Name", "Severity",
                            )},
                            "Redacted Match": redact_secret_match(match.group(1)),
                            "Match SHA256": hashlib.sha256(
                                match.group(1).encode("utf-8", errors="replace")
                            ).hexdigest()[:16],
                        })
                    for match in SECRET_RE.finditer(line):
                        pattern_name, severity = classify_secret(match.group(1))
                        broad_finding = {
                            "Service": service,
                            "File Path": os.path.relpath(path, root),
                            "Line Number": lineno,
                            "Pattern Name": pattern_name,
                            "Severity": severity,
                        }
                        secret_evidence.append({
                            **{key: broad_finding[key] for key in (
                                "Service", "File Path", "Line Number",
                                "Pattern Name", "Severity",
                            )},
                            "Redacted Match": redact_secret_match(match.group(1)),
                            "Match SHA256": hashlib.sha256(
                                match.group(1).encode("utf-8", errors="replace")
                            ).hexdigest()[:16],
                        })
        except OSError:
            continue

    secret_findings.sort(key=lambda row: (
        row["File Path"],
        row["Line Number"],
        row["Service"],
        row["Pattern Name"],
    ))
    secret_rows = []
    for index, finding in enumerate(secret_findings, start=1):
        secret_rows.append({
            "Finding ID": f"{finding_id_prefix}-{index:03d}",
            **finding,
        })

    avg_score = (
        round(sum(row["Compliance Score"] for row in audit_rows) / len(audit_rows), 1)
        if audit_rows
        else 0.0
    )
    below_75 = [row for row in audit_rows if float(row["Compliance Score"]) < 75]
    critical = [row for row in secret_rows if row["Severity"] == "Critical"]
    high = [row for row in secret_rows if row["Severity"] == "High"]
    emit({
        "root": root,
        "audit_rows": audit_rows,
        "secret_rows": secret_rows,
        "secret_scan_evidence": {
            "task_regex_matches": len(task_regex_evidence),
            "helper_regex_matches": len(secret_evidence),
            "task_regex": task_regex_evidence,
            "helper_regex": secret_evidence,
        },
        "summary": {
            "services": len(audit_rows),
            "avg_compliance_score": avg_score,
            "services_below_75": len(below_75),
            "secrets": len(secret_rows),
            "critical": len(critical),
            "high": len(high),
        },
    })


QUOTED_JS_REFERENCE = re.compile(
    r"\b(?:import|export)\s+(?:[^;]*?\s+from\s+)?['\"]([^'\"]+)['\"]"
)
CALLED_JS_REFERENCE = re.compile(
    r"\b(?:require|import)\s*\(\s*['\"]([^'\"]+)['\"]"
)
PYTHON_FROM_REFERENCE = re.compile(
    r"^\s*from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\b"
)
PYTHON_IMPORT_REFERENCE = re.compile(r"^\s*import\s+(.+?)\s*(?:#.*)?$")
C_INCLUDE_REFERENCE = re.compile(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]")
STYLE_REFERENCE = re.compile(
    r"^\s*@(import|use|forward)\s+(?:url\(\s*)?['\"]([^'\"]+)['\"]"
)


def extract_module_references(line):
    references = []
    include = C_INCLUDE_REFERENCE.search(line)
    if include:
        references.append(include.group(1))
    style = STYLE_REFERENCE.search(line)
    if style:
        references.append(style.group(2))
    python_from = PYTHON_FROM_REFERENCE.search(line)
    if python_from:
        references.append(python_from.group(1))
    else:
        python_import = PYTHON_IMPORT_REFERENCE.search(line)
        if python_import:
            for item in python_import.group(1).split(","):
                module = re.split(r"\s+as\s+", item.strip(), maxsplit=1)[0]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module):
                    references.append(module)
    references.extend(QUOTED_JS_REFERENCE.findall(line))
    references.extend(CALLED_JS_REFERENCE.findall(line))
    return list(dict.fromkeys(ref for ref in references if ref))


def module_references_project(module, project):
    normalized_module = module.strip().lower().replace("\\", "/").replace("_", "-")
    normalized_project = project.strip().lower().replace("_", "-")
    if not normalized_module or not normalized_project:
        return False
    pattern = re.compile(
        r"(?:^|[/.])" + re.escape(normalized_project) + r"(?:$|[/.])"
    )
    return bool(pattern.search(normalized_module))


def op_scan_project_dependencies(payload):
    ensure_project_bridge()
    root = resolve(payload.get("root") or "/home/coder/project", "/home/coder/project", must_exist=True, want_dir=True)
    projects = payload.get("projects") or []
    scan_roots = payload.get("scan_roots") or {}
    ownership = payload.get("ownership") or {}
    if not projects:
        fail("projects is required")
    missing_roots = [project for project in projects if project not in scan_roots]
    if missing_roots:
        fail("scan_roots missing projects: " + ", ".join(missing_roots))
    missing_owners = [project for project in projects if project not in ownership]
    if missing_owners:
        fail("ownership missing projects: " + ", ".join(missing_owners))

    team_rows = []
    for project in projects:
        owner = ownership.get(project, {})
        team_rows.append({
            "Project": project,
            "Owning Team": owner.get("Owning Team") or owner.get("owning_team") or "",
            "Tech Lead": owner.get("Tech Lead") or owner.get("tech_lead") or "",
        })

    edges = []
    seen = set()
    for source in projects:
        rel_root = scan_roots.get(source, source)
        source_root = os.path.join(root, rel_root)
        if not os.path.isdir(source_root):
            continue
        for dirpath, dirnames, filenames in os.walk(source_root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and d != "fonts"]
            for filename in filenames:
                if filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".woff", ".woff2", ".ttf", ".eot", ".bin", ".zip", ".gz", ".pyc")):
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    if os.path.getsize(path) > 1_000_000 or not is_text_file(path):
                        continue
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, start=1):
                            references = extract_module_references(line)
                            for target in projects:
                                if target == source:
                                    continue
                                if not any(module_references_project(ref, target) for ref in references):
                                    continue
                                key = (source, target, os.path.relpath(path, root), lineno)
                                if key in seen:
                                    continue
                                seen.add(key)
                                source_team = team_rows[projects.index(source)]["Owning Team"] if source in projects else ""
                                target_owner = ownership.get(target, {})
                                target_team = target_owner.get("Owning Team") or target_owner.get("owning_team") or ""
                                edges.append({
                                    "Source Project": source,
                                    "Target Project": target,
                                    "Source File": os.path.relpath(path, root),
                                    "Line Number": lineno,
                                    "Cross Team": bool(source_team and target_team and source_team != target_team),
                                })
                except OSError:
                    continue

    edges.sort(key=lambda edge: (
        edge["Source Project"],
        edge["Source File"],
        edge["Line Number"],
        edge["Target Project"],
    ))
    for index, edge in enumerate(edges, start=1):
        edge["Edge ID"] = f"DE-{index:03d}"

    emit({
        "root": root,
        "team_ownership": team_rows,
        "dependency_edges": edges,
        "summary": {
            "projects": len(team_rows),
            "edges": len(edges),
            "cross_team_edges": sum(1 for edge in edges if edge["Cross Team"]),
        },
    })


def parse_test_output(output, parser):
    parser = (parser or "auto").strip().lower()
    if parser == "auto":
        if re.search(r"tests failed out of\s+\d+", output, re.IGNORECASE):
            parser = "ctest"
        elif re.search(r"^\s*Tests:\s", output, re.MULTILINE):
            parser = "jest"
        elif re.search(r"\b\d+\s+(?:passed|failed)\b", output):
            parser = "pytest"
        else:
            return {"parser": "auto", "parsed": False, "passed": None, "failed": None, "skipped": None, "total": None, "pass_rate": None}

    if parser == "pytest":
        counts = {
            name.lower(): int(value)
            for value, name in re.findall(
                r"\b(\d+)\s+(passed|failed|skipped)\b", output, re.IGNORECASE
            )
        }
        if "passed" not in counts and "failed" not in counts:
            if re.search(r"\bno tests ran\b", output, re.IGNORECASE):
                return {"parser": parser, "parsed": True, "passed": 0, "failed": 0, "skipped": 0, "total": 0, "pass_rate": None}
            return {"parser": parser, "parsed": False, "passed": None, "failed": None, "skipped": None, "total": None, "pass_rate": None}
        passed = counts.get("passed", 0)
        failed = counts.get("failed", 0)
        skipped = counts.get("skipped", 0)
        total = passed + failed
    elif parser == "ctest":
        match = re.search(r"(\d+)\s+tests?\s+failed\s+out\s+of\s+(\d+)", output, re.IGNORECASE)
        if not match:
            return {"parser": parser, "parsed": False, "passed": None, "failed": None, "skipped": None, "total": None, "pass_rate": None}
        failed = int(match.group(1))
        total = int(match.group(2))
        passed = max(total - failed, 0)
        skipped = 0
    elif parser in {"jest", "vitest"}:
        summary = re.search(r"^\s*Tests(?::|\s{2,})\s*(.+)$", output, re.MULTILINE)
        if not summary:
            return {"parser": parser, "parsed": False, "passed": None, "failed": None, "skipped": None, "total": None, "pass_rate": None}
        counts = {
            name.lower(): int(value)
            for value, name in re.findall(r"(\d+)\s+(failed|passed|skipped|total)", summary.group(1), re.IGNORECASE)
        }
        passed = counts.get("passed", 0)
        failed = counts.get("failed", 0)
        skipped = counts.get("skipped", 0)
        total = counts.get("total", passed + failed)
    else:
        fail(f"unsupported test output parser: {parser}")

    return {
        "parser": parser,
        "parsed": True,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total": total,
        "pass_rate": round(passed / total * 100, 2) if total else None,
    }


def tracked_status(cwd):
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        cwd=cwd,
        text=True,
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def validate_test_command(command):
    lowered = command.strip().lower()
    allowed = re.compile(
        r"^(?:pytest(?:\s|$)|python3?\s+-m\s+pytest(?:\s|$)|ctest(?:\s|$)|"
        r"cmake\s+--build\s+|npm\s+(?:test|run\s+test)(?:\s|$)|"
        r"pnpm\s+(?:test|run\s+test)(?:\s|$)|yarn\s+(?:test|run\s+test)(?:\s|$)|"
        r"npx\s+(?:jest|vitest)(?:\s|$)|(?:jest|vitest)(?:\s|$)|"
        r"make\s+test(?:\s|$)|go\s+test(?:\s|$)|cargo\s+test(?:\s|$)|"
        r"mvn\s+test(?:\s|$)|(?:\./gradlew|gradle)\s+test(?:\s|$))"
    )
    forbidden = re.compile(
        r"(?:^|\s)(?:pip|pip3|npm|pnpm|yarn|apt|apt-get)\s+install\b|"
        r"\bsed\s+-i\b|\bgit\s+(?:checkout|reset|restore|clean)\b|[\r\n;&|><]"
    )
    if not allowed.match(lowered) or forbidden.search(lowered):
        fail("command is not an allowed read-only test command")


def op_collect_test_metrics(payload):
    ensure_project_bridge()
    projects = payload.get("projects") or []
    if not projects:
        fail("projects is required")
    root = resolve(payload.get("root") or PROJECT_ROOT, PROJECT_ROOT, must_exist=True, want_dir=True)
    rows = []
    for spec in projects:
        project = str(spec.get("project") or "").strip()
        command = str(spec.get("command") or "").strip()
        parser = str(spec.get("parser") or "auto").strip().lower()
        test_globs = spec.get("test_globs") or []
        if not project or not command or not test_globs:
            fail("each project requires project, command, and test_globs")
        validate_test_command(command)
        cwd = resolve(spec.get("path") or project, root, must_exist=True, want_dir=True)
        before = tracked_status(cwd)
        timed_out = False
        try:
            process = subprocess.run(
                command,
                cwd=cwd,
                shell=True,
                text=True,
                capture_output=True,
                timeout=int(spec.get("timeout") or 300),
            )
            returncode = process.returncode
            stdout = process.stdout
            stderr = process.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = 124
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
        after = tracked_status(cwd)
        combined = f"{stdout}\n{stderr}"
        blocker = None
        if timed_out:
            blocker = "timeout"
        elif returncode == 127 or re.search(r"command not found|not recognized", combined, re.IGNORECASE):
            blocker = "missing_executable"
        elif re.search(r"no such file or directory|does not exist|could not load cache", combined, re.IGNORECASE):
            blocker = "missing_prerequisite"
        files = set()
        for pattern in test_globs:
            for path in glob.glob(os.path.join(cwd, str(pattern)), recursive=True):
                if os.path.isfile(path):
                    files.add(os.path.realpath(path))
        rows.append({
            "project": project,
            "path": os.path.relpath(cwd, root),
            "command": command,
            "returncode": returncode,
            "stdout": stdout[-20000:],
            "stderr": stderr[-20000:],
            "timed_out": timed_out,
            "test_files_count": len(files),
            "metrics": parse_test_output(combined, parser),
            "source_modified": before is not None and after is not None and before != after,
            "git_status_before": before,
            "git_status_after": after,
            "blocker": blocker,
        })
    emit({"root": root, "projects": rows})


def run_git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, text=True, capture_output=True)


def op_git_commit(payload):
    ensure_project_bridge()
    default_root = payload.get("default_root") or "/home/coder/project"
    repo_path = resolve(payload["repo_path"], default_root, must_exist=True, want_dir=True)
    message = payload["message"]
    paths = payload.get("paths") or []

    run_git(
        ["config", "user.email", "intelligence-indeed-agent@example.invalid"],
        repo_path,
    )
    run_git(["config", "user.name", "Intelligence Indeed Agent"], repo_path)
    add = run_git(["add"] + paths if paths else ["add", "-A"], repo_path)
    if add.returncode != 0:
        fail(add.stderr or add.stdout or "git add failed")
    diff = run_git(["diff", "--cached", "--quiet"], repo_path)
    if diff.returncode == 0:
        emit({"repo_path": repo_path, "committed": False, "message": message, "commit": None, "detail": "no staged changes"})
        return
    commit = run_git(["commit", "-m", message], repo_path)
    if commit.returncode != 0:
        fail(commit.stderr or commit.stdout or "git commit failed")
    log = run_git(["rev-parse", "--short", "HEAD"], repo_path)
    emit({
        "repo_path": repo_path,
        "committed": True,
        "message": message,
        "commit": log.stdout.strip(),
        "stdout": commit.stdout,
        "stderr": commit.stderr,
    })


payload = json.load(sys.stdin)
op = payload.get("op")
if op == "search":
    op_search(payload)
elif op == "read":
    op_read(payload)
elif op == "write":
    op_write(payload)
elif op == "exec":
    op_exec(payload)
elif op == "run_python":
    op_run_python(payload)
elif op == "scan_docker_security":
    op_scan_docker_security(payload)
elif op == "scan_project_dependencies":
    op_scan_project_dependencies(payload)
elif op == "collect_test_metrics":
    op_collect_test_metrics(payload)
elif op == "git_commit":
    op_git_commit(payload)
else:
    fail(f"unknown operation: {op}")
"""


class CodeServerClient:
    """Execute deterministic source/file operations inside a code-server container."""

    def __init__(
        self,
        container_name: str,
        *,
        runner: Runner | None = None,
        timeout: int = 60,
        default_root: str = DEFAULT_PROJECT_ROOT,
    ) -> None:
        if not container_name or not str(container_name).strip():
            raise CodeServerError("container_name is required")
        self.container_name = str(container_name).strip()
        self.runner = runner or _run_command
        self.timeout = timeout
        self.default_root = default_root

    def search_files(
        self,
        pattern: str,
        roots: list[str] | None = None,
        include_globs: list[str] | None = None,
        exclude_globs: list[str] | None = None,
        max_matches: int = 100,
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        return self._operation({
            "op": "search",
            "pattern": _require_text(pattern, "pattern"),
            "roots": roots or ["."],
            "include_globs": include_globs or [],
            "exclude_globs": exclude_globs or [],
            "max_matches": max_matches,
            "case_sensitive": case_sensitive,
            "default_root": self.default_root,
        })

    def read_files(self, paths: list[str], max_chars: int = 20_000) -> dict[str, Any]:
        if not paths:
            raise CodeServerError("paths is required")
        return self._operation({
            "op": "read",
            "paths": [str(path) for path in paths],
            "max_chars": max_chars,
            "default_root": self.default_root,
        })

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        return self._operation({
            "op": "write",
            "path": _require_text(path, "path"),
            "content": content,
            "default_root": self.default_root,
        })

    def run_shell(
        self,
        command: str,
        cwd: str | None = None,
        timeout: int = 120,
        max_output: int = 20_000,
    ) -> dict[str, Any]:
        return self._operation({
            "op": "exec",
            "command": _require_text(command, "command"),
            "cwd": cwd or DEFAULT_PROJECT_ROOT,
            "timeout": timeout,
            "max_output": max_output,
            "default_root": self.default_root,
        })

    def run_python(
        self,
        script: str,
        cwd: str | None = None,
        timeout: int = 120,
        max_output: int = 20_000,
    ) -> dict[str, Any]:
        return self._operation({
            "op": "run_python",
            "script": _require_text(script, "script"),
            "cwd": cwd or DEFAULT_PROJECT_ROOT,
            "timeout": timeout,
            "max_output": max_output,
            "default_root": self.default_root,
        })

    def scan_docker_security(
        self,
        dockerfiles: dict[str, str],
        audit_date: str,
        finding_id_prefix: str = "HS",
    ) -> dict[str, Any]:
        if not dockerfiles:
            raise CodeServerError("dockerfiles is required")
        audit_date = _require_text(audit_date, "audit_date")
        finding_id_prefix = _require_text(
            finding_id_prefix,
            "finding_id_prefix",
        )
        return self._operation({
            "op": "scan_docker_security",
            "root": DEFAULT_PROJECT_ROOT,
            "dockerfiles": {
                _require_text(service, "dockerfile service"): _require_text(
                    path,
                    "dockerfile path",
                )
                for service, path in dockerfiles.items()
            },
            "audit_date": audit_date,
            "finding_id_prefix": finding_id_prefix,
            "default_root": self.default_root,
        })

    def scan_project_dependencies(
        self,
        projects: list[str],
        scan_roots: dict[str, str],
        ownership: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        if not projects:
            raise CodeServerError("projects is required")
        if not scan_roots:
            raise CodeServerError("scan_roots is required")
        if not ownership:
            raise CodeServerError("ownership is required")
        return self._operation({
            "op": "scan_project_dependencies",
            "root": DEFAULT_PROJECT_ROOT,
            "projects": [str(project) for project in projects],
            "scan_roots": {str(key): str(value) for key, value in scan_roots.items()},
            "ownership": ownership,
            "default_root": self.default_root,
        })

    def collect_test_metrics(
        self,
        projects: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not projects:
            raise CodeServerError("projects is required")
        return self._operation({
            "op": "collect_test_metrics",
            "root": DEFAULT_PROJECT_ROOT,
            "projects": projects,
            "default_root": self.default_root,
            "timeout": max(
                sum(int(project.get("timeout") or 300) for project in projects),
                300,
            ) + 30,
        })

    def git_commit(
        self,
        repo_path: str,
        message: str,
        paths: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._operation({
            "op": "git_commit",
            "repo_path": _require_text(repo_path, "repo_path"),
            "message": _require_text(message, "message"),
            "paths": [str(path) for path in (paths or [])],
            "default_root": self.default_root,
        })

    def _operation(self, payload: dict[str, Any]) -> dict[str, Any]:
        op_timeout = int(payload.get("timeout") or self.timeout)
        outer_timeout = max(self.timeout, op_timeout + 5)
        result = self.runner(
            ["docker", "exec", "-i", self.container_name, "python3", "-c", _CONTAINER_SCRIPT],
            input_text=json.dumps(payload, ensure_ascii=False),
            timeout=outer_timeout,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"return code {result.returncode}"
            raise CodeServerError(detail)
        try:
            decoded = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CodeServerError(f"code-server helper returned non-JSON output: {result.stdout[:500]}") from exc
        if isinstance(decoded, dict) and decoded.get("error"):
            raise CodeServerError(str(decoded["error"]))
        return decoded


def _run_command(
    cmd: list[str],
    *,
    input_text: str | None = None,
    timeout: int | None = None,
) -> CommandResult:
    completed = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _require_text(value: Any, name: str) -> str:
    if value is None or not str(value).strip():
        raise CodeServerError(f"{name} is required")
    return str(value).strip()
