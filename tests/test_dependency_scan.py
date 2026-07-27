from saas_agent.dependency_scan import (
    dependency_edge_sort_key,
    extract_module_references,
    module_references_project,
)


def test_extracts_javascript_module_references():
    assert extract_module_references("import client from 'todo-api/client'") == [
        "todo-api/client"
    ]
    assert extract_module_references('const data = require("json")') == ["json"]
    assert extract_module_references("const page = import('@acme/tabler/core')") == [
        "@acme/tabler/core"
    ]


def test_extracts_python_c_and_stylesheet_references():
    assert extract_module_references("from todo_api.models import Item") == [
        "todo_api.models"
    ]
    assert extract_module_references("import json, todo_api as api") == [
        "json",
        "todo_api",
    ]
    assert extract_module_references('#include "tabler/core/table.hpp"') == [
        "tabler/core/table.hpp"
    ]
    assert extract_module_references('@use "vue-hackernews-2.0/theme";') == [
        "vue-hackernews-2.0/theme"
    ]


def test_ignores_non_import_configuration_and_plain_text():
    assert extract_module_references("default_type application/json;") == []
    assert extract_module_references("# todo-api is owned by Backend") == []
    assert extract_module_references('name = "vue-hackernews-2.0"') == []


def test_project_match_supports_hyphen_underscore_and_module_paths():
    assert module_references_project("todo_api.models", "todo-api")
    assert module_references_project(
        "@acme/vue-hackernews-2.0/theme", "vue-hackernews-2.0"
    )
    assert module_references_project("../../json/include/json.hpp", "json")


def test_project_match_requires_a_complete_module_component():
    assert not module_references_project("todo-api-client", "todo-api")
    assert not module_references_project("myjson", "json")


def test_dependency_edges_sort_by_source_file_and_line_before_target():
    edges = [
        {
            "Source Project": "app",
            "Target Project": "alpha",
            "Source File": "z.py",
            "Line Number": 1,
        },
        {
            "Source Project": "app",
            "Target Project": "zeta",
            "Source File": "a.py",
            "Line Number": 9,
        },
    ]

    assert sorted(edges, key=dependency_edge_sort_key)[0]["Source File"] == "a.py"
