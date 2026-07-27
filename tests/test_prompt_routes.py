from saas_agent.prompt_routes import build_prompt_rules


def test_default_mode_routes_trimmed_rules(monkeypatch):
    monkeypatch.delenv("SAAS_AGENT_PROMPT_MODE", raising=False)

    rules, meta = build_prompt_rules(["baserow"])

    assert rules is not None
    assert meta["mode"] == "routing_trimmed"
    assert meta["app_rules"] == ["baserow"]
    assert meta["apps"] == ["baserow"]


def test_disabled_mode_returns_no_rules():
    rules, meta = build_prompt_rules(["baserow"], mode="off")

    assert rules is None
    assert meta["mode"] == "disabled"
    assert meta["app_rules"] == []


def test_routing_trimmed_injects_only_matching_app_rules():
    rules, meta = build_prompt_rules(
        ["farmos", "siyuan"], mode="routing_trimmed"
    )

    assert "FarmOS" in rules
    assert "SiYuan" in rules
    assert "Baserow" not in rules
    assert meta["app_rules"] == ["farmos", "siyuan"]
    assert meta["rules_chars"] == len(rules)


def test_routing_trimmed_uses_persisted_farmos_rule():
    rules, meta = build_prompt_rules(["farmos"], mode="routing_trimmed")

    assert "FarmOS Persisted Structured Logs" in rules
    assert "temporary form text or a notification" in rules
    assert meta["app_rules"] == ["farmos"]


def test_multi_app_rules_are_conditional():
    single_rules, single_meta = build_prompt_rules(["siyuan"])
    multi_rules, multi_meta = build_prompt_rules(["siyuan", "watcharr"])

    assert "Multi-App Workflow" not in single_rules
    assert single_meta["multi_app_rules"] is False
    assert "Multi-App Workflow" in multi_rules
    assert multi_meta["multi_app_rules"] is True


def test_missing_app_rules_are_reported():
    rules, meta = build_prompt_rules(["siyuan", "unknown-app"])

    assert "SiYuan" in rules
    assert meta["app_rules"] == ["siyuan"]
    assert meta["missing_app_rules"] == ["unknown-app"]


def test_routes_watcharr_openemr_and_onlyoffice():
    rules, meta = build_prompt_rules(["watcharr", "openemr", "onlyoffice"])

    assert "Watcharr Status Guidance" in rules
    assert "OpenEMR Encounter Guidance" in rules
    assert "OnlyOffice Document Guidance" in rules
    assert meta["app_rules"] == ["watcharr", "openemr", "onlyoffice"]


def test_opnform_requires_durable_readback():
    rules, meta = build_prompt_rules(["opnform"])

    assert "form list, public page, or preview page" in rules
    assert "Do not report the form as complete" in rules
    assert meta["app_rules"] == ["opnform"]


def test_opnform_requires_blank_form_and_early_persistence_checkpoint():
    rules, _ = build_prompt_rules(["opnform"])

    assert "If the task says blank form" in rules
    assert "do not use contact or simple templates" in rules
    assert "before adding many fields" in rules
    assert "If the form is not visible in My Forms" in rules


def test_openemr_forbids_duplicate_patient_for_open_chart_tasks():
    rules, _ = build_prompt_rules(["openemr"])

    assert "If the task says open the chart" in rules
    assert "do not create a new patient" in rules
    assert "skip the patient-specific subtask" in rules


def test_watcharr_search_failure_falls_back_to_existing_entries():
    rules, _ = build_prompt_rules(["watcharr"])

    assert "After two failed searches" in rules
    assert "do not click Try Again again" in rules
    assert "HOLD" in rules
    assert "existing entry" in rules


def test_routing_bucket_uses_bucket_mode_metadata():
    rules, meta = build_prompt_rules(["baserow"], mode="routing_bucket")

    assert "Baserow Grid and Dropdown Workaround" in rules
    assert "### Baserow Guidance" not in rules
    assert meta["mode"] == "routing_bucket"
    assert meta["app_rules"] == ["baserow"]


def test_app_names_are_normalized_and_deduplicated():
    _, meta = build_prompt_rules([" Baserow ", "baserow", "SiYuan"])

    assert meta["apps"] == ["baserow", "siyuan"]
