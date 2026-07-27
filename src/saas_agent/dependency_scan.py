"""Language-aware dependency reference extraction for source scans."""

from __future__ import annotations

import re


_QUOTED_JS_REFERENCE = re.compile(
    r"\b(?:import|export)\s+(?:[^;]*?\s+from\s+)?['\"]([^'\"]+)['\"]"
)
_CALLED_JS_REFERENCE = re.compile(
    r"\b(?:require|import)\s*\(\s*['\"]([^'\"]+)['\"]"
)
_PYTHON_FROM_REFERENCE = re.compile(
    r"^\s*from\s+([A-Za-z_][A-Za-z0-9_.]*)\s+import\b"
)
_PYTHON_IMPORT_REFERENCE = re.compile(r"^\s*import\s+(.+?)\s*(?:#.*)?$")
_C_INCLUDE_REFERENCE = re.compile(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]")
_STYLE_REFERENCE = re.compile(
    r"^\s*@(import|use|forward)\s+(?:url\(\s*)?['\"]([^'\"]+)['\"]"
)


def extract_module_references(line: str) -> list[str]:
    """Return module paths used by real import/include syntax on one line."""

    references: list[str] = []

    include = _C_INCLUDE_REFERENCE.search(line)
    if include:
        references.append(include.group(1))

    style = _STYLE_REFERENCE.search(line)
    if style:
        references.append(style.group(2))

    python_from = _PYTHON_FROM_REFERENCE.search(line)
    if python_from:
        references.append(python_from.group(1))
    else:
        python_import = _PYTHON_IMPORT_REFERENCE.search(line)
        if python_import:
            for item in python_import.group(1).split(","):
                module = re.split(r"\s+as\s+", item.strip(), maxsplit=1)[0]
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module):
                    references.append(module)

    references.extend(_QUOTED_JS_REFERENCE.findall(line))
    references.extend(_CALLED_JS_REFERENCE.findall(line))

    return list(dict.fromkeys(ref for ref in references if ref))


def module_references_project(module: str, project: str) -> bool:
    """Match a project as a complete module/path component."""

    normalized_module = module.strip().lower().replace("\\", "/").replace("_", "-")
    normalized_project = project.strip().lower().replace("_", "-")
    if not normalized_module or not normalized_project:
        return False
    pattern = re.compile(
        r"(?:^|[/.])" + re.escape(normalized_project) + r"(?:$|[/.])"
    )
    return bool(pattern.search(normalized_module))


def dependency_edge_sort_key(edge: dict) -> tuple[str, str, int, str]:
    """Sort edges by source, file, line, then target as a stable tie-breaker."""

    return (
        str(edge["Source Project"]),
        str(edge["Source File"]),
        int(edge["Line Number"]),
        str(edge["Target Project"]),
    )
