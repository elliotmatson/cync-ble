"""Cross-checks between the code and the translation/metadata files.

These catch the class of mistake that doesn't fail at import and doesn't
show up until a user opens a form: a step whose translation is missing, an
abort reason with no text, or a placeholder the code never supplies (which
makes Home Assistant fail to render the form rather than degrade).

The flow is read with AST rather than executed, so this stays independent of
the stub harness.
"""
from __future__ import annotations

import ast
import json
import string

import pytest
import yaml
from conftest import COMPONENT_DIR, REPO_ROOT, load

ds = load("device_sync")

TRANSLATION_FILES = ["strings.json", "translations/en.json"]


def read_json(relative: str) -> dict:
    return json.loads((COMPONENT_DIR / relative).read_text())


@pytest.fixture(scope="module")
def flow_ast() -> ast.Module:
    return ast.parse((COMPONENT_DIR / "config_flow.py").read_text())


def _keyword(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _flow_class(flow_ast: ast.Module, name: str) -> ast.ClassDef:
    for node in flow_ast.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in config_flow.py")


def _facts(tree: ast.AST) -> dict:
    """Extract shown step ids, abort reasons and supplied placeholders."""
    shown: set[str] = set()
    aborts: set[str] = set()
    placeholders: dict[str, object] = {}
    uses_update_reload = False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        name = node.func.attr
        if name == "async_show_form":
            step = _keyword(node, "step_id")
            if isinstance(step, ast.Constant):
                shown.add(step.value)
                supplied = _keyword(node, "description_placeholders")
                if isinstance(supplied, ast.Dict):
                    placeholders[step.value] = {
                        k.value for k in supplied.keys
                        if isinstance(k, ast.Constant)
                    }
                elif isinstance(supplied, ast.Call) and \
                        getattr(supplied.func, "id", None) == "summarize":
                    placeholders[step.value] = "summarize"
        elif name == "async_abort":
            reason = _keyword(node, "reason") or (node.args[0] if node.args else None)
            if isinstance(reason, ast.Constant):
                aborts.add(reason.value)
        elif name == "async_update_reload_and_abort":
            uses_update_reload = True

    return {
        "shown_steps": shown,
        "abort_reasons": aborts,
        "placeholders": placeholders,
        "uses_update_reload": uses_update_reload,
    }


@pytest.fixture(scope="module")
def flow_facts(flow_ast):
    """Facts for the config flow only. The options flow's steps translate
    under "options", not "config", so it is scanned separately."""
    return _facts(_flow_class(flow_ast, "CyncBLEConfigFlow"))


@pytest.fixture(scope="module")
def options_facts(flow_ast):
    return _facts(_flow_class(flow_ast, "CyncBLEOptionsFlow"))


# --------------------------------------------------------------------------
# Files parse
# --------------------------------------------------------------------------

@pytest.mark.parametrize("relative", TRANSLATION_FILES + ["manifest.json"])
def test_component_json_parses(relative):
    read_json(relative)


def test_hacs_json_parses():
    json.loads((REPO_ROOT / "hacs.json").read_text())


def test_services_yaml_parses():
    services = yaml.safe_load((COMPONENT_DIR / "services.yaml").read_text())
    assert "query_firmware_versions" in services


@pytest.mark.parametrize(
    "workflow",
    sorted(p.name for p in (REPO_ROOT / ".github/workflows").glob("*.yml")),
)
def test_workflow_yaml_parses(workflow):
    yaml.safe_load((REPO_ROOT / ".github/workflows" / workflow).read_text())


# --------------------------------------------------------------------------
# Config flow <-> translations
# --------------------------------------------------------------------------

@pytest.mark.parametrize("relative", TRANSLATION_FILES)
def test_every_shown_step_has_a_translation(relative, flow_facts):
    steps = read_json(relative)["config"]["step"]
    assert flow_facts["shown_steps"] - set(steps) == set()


@pytest.mark.parametrize("relative", TRANSLATION_FILES)
def test_no_orphaned_step_translations(relative, flow_facts):
    steps = read_json(relative)["config"]["step"]
    assert set(steps) - flow_facts["shown_steps"] == set()


@pytest.mark.parametrize("relative", TRANSLATION_FILES)
def test_every_abort_reason_is_translated(relative, flow_facts):
    aborts = set(read_json(relative)["config"]["abort"])
    expected = set(flow_facts["abort_reasons"])
    if flow_facts["uses_update_reload"]:
        # HA emits this reason itself on a successful reconfigure.
        expected.add("reconfigure_successful")
    assert expected - aborts == set()


@pytest.mark.parametrize("relative", TRANSLATION_FILES)
def test_step_placeholders_are_all_supplied_by_the_code(relative, flow_facts):
    """An unsupplied placeholder makes HA fail to render the form."""
    steps = read_json(relative)["config"]["step"]
    for step_id, body in steps.items():
        if step_id not in flow_facts["shown_steps"]:
            continue
        used = {
            field for _, field, _, _
            in string.Formatter().parse(body.get("description", "")) if field
        }
        supplied = flow_facts["placeholders"].get(step_id, set())
        if supplied == "summarize":
            supplied = set(ds.summarize(ds.diff_devices([], [])))
        assert used - supplied == set(), f"{relative}:{step_id}"


@pytest.mark.parametrize("relative", TRANSLATION_FILES)
def test_summarize_produces_no_unused_placeholders(relative):
    """Keeps summarize() and the form text from drifting apart."""
    description = \
        read_json(relative)["config"]["step"]["reconfigure_confirm"]["description"]
    used = {f for _, f, _, _ in string.Formatter().parse(description) if f}
    produced = set(ds.summarize(ds.diff_devices([], [])))
    assert produced - used == set()


@pytest.mark.parametrize("relative", TRANSLATION_FILES)
@pytest.mark.parametrize(
    ("step_id", "fields"),
    [
        ("user", {"email", "password"}),
        ("otp", {"otp"}),
        ("reconfigure_auth", {"password"}),
        ("reconfigure_otp", {"otp"}),
        ("reconfigure_confirm", {"remove_missing"}),
    ],
)
def test_step_data_keys_match_the_schema(relative, step_id, fields):
    steps = read_json(relative)["config"]["step"]
    assert set(steps[step_id].get("data", {})) == fields


# --------------------------------------------------------------------------
# Options flow <-> translations
# --------------------------------------------------------------------------

@pytest.mark.parametrize("relative", TRANSLATION_FILES)
def test_options_steps_match_translations(relative, options_facts):
    steps = read_json(relative)["options"]["step"]
    assert set(steps) == options_facts["shown_steps"]


@pytest.mark.parametrize("relative", TRANSLATION_FILES)
def test_options_init_data_keys_match_the_schema(relative):
    const = load("const")
    steps = read_json(relative)["options"]["step"]
    assert set(steps["init"]["data"]) == {const.CONF_WRITE_WITHOUT_RESPONSE}


def test_en_json_options_omit_data_description_by_convention():
    steps = read_json("translations/en.json")["options"]["step"]
    assert [s for s, body in steps.items() if "data_description" in body] == []


# --------------------------------------------------------------------------
# Repairs issue
# --------------------------------------------------------------------------

@pytest.mark.parametrize("relative", TRANSLATION_FILES)
def test_issues_section_defines_unknown_devices(relative):
    assert "unknown_devices" in read_json(relative)["issues"]


@pytest.mark.parametrize("relative", TRANSLATION_FILES)
def test_issue_placeholders_are_supplied_by_the_coordinator(relative):
    description = read_json(relative)["issues"]["unknown_devices"]["description"]
    used = {f for _, f, _, _ in string.Formatter().parse(description) if f}
    source = (COMPONENT_DIR / "coordinator.py").read_text()
    # The coordinator builds translation_placeholders with these keys.
    assert used - {"count", "devices"} == set()
    for key in used:
        assert f'"{key}":' in source


# --------------------------------------------------------------------------
# Entity name translations
# --------------------------------------------------------------------------

def _sensor_translation_keys() -> set[str]:
    """The key each sensor class passes up to CyncBLESystemEntity.__init__.

    Read from the source rather than by instantiating the classes, which
    would need a coordinator.
    """
    tree = ast.parse((COMPONENT_DIR / "sensor.py").read_text())
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "__init__":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    keys.add(arg.value)
    return keys


@pytest.mark.parametrize("relative", TRANSLATION_FILES)
def test_sensor_translation_keys_cover_every_sensor(relative):
    keys = _sensor_translation_keys()
    assert keys, "no sensor translation keys found — did sensor.py change shape?"
    assert set(read_json(relative)["entity"]["sensor"]) == keys


@pytest.mark.parametrize("relative", TRANSLATION_FILES)
def test_binary_sensor_translation_keys_cover_every_entity(relative):
    assert set(read_json(relative)["entity"]["binary_sensor"]) == \
        {"mesh_connectivity"}


# --------------------------------------------------------------------------
# The two translation files must agree structurally
# --------------------------------------------------------------------------

@pytest.mark.parametrize("section", ["step", "abort", "error"])
def test_both_translation_files_define_the_same_config_keys(section):
    a = read_json("strings.json")["config"][section]
    b = read_json("translations/en.json")["config"][section]
    assert set(a) == set(b)


def test_both_translation_files_define_the_same_issues():
    assert set(read_json("strings.json")["issues"]) == \
        set(read_json("translations/en.json")["issues"])


def test_en_json_omits_data_description_by_convention():
    """Matches the existing convention in this repo."""
    steps = read_json("translations/en.json")["config"]["step"]
    assert [s for s, body in steps.items() if "data_description" in body] == []


# --------------------------------------------------------------------------
# Version metadata
# --------------------------------------------------------------------------

def test_manifest_keys_are_sorted_the_way_hassfest_requires():
    """domain, name, then strict alphabetical.

    hassfest enforces this, and it failed there before it failed here —
    which is the wrong order. Asserting it locally means the next manifest
    edit is caught before CI.
    """
    keys = list(read_json("manifest.json"))
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])


def test_integration_declares_a_config_schema():
    """hassfest requires one for any integration that can be configured;
    this one is config-entry only."""
    source = (COMPONENT_DIR / "__init__.py").read_text()
    assert "CONFIG_SCHEMA = cv.config_entry_only_config_schema" in source


def test_manifest_version_is_present_and_semver_shaped():
    version = read_json("manifest.json")["version"]
    parts = version.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts), version


def test_every_declared_platform_has_a_module():
    const = load("const")
    for platform in const.PLATFORMS:
        assert (COMPONENT_DIR / f"{platform}.py").is_file(), platform
