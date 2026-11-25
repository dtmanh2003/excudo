"""The dev utility from zun_loc is off by default; when on, every tool has a risk and Japanese text."""
from excudo import config, tools
from excudo.permissions import ActionRisk
from excudo.utilities.dev.tools import TOOLS


def test_dev_tools_are_off_unless_enabled():
    if "dev" not in config.UTILITIES:
        assert not set(TOOLS) & set(tools.REGISTRY)


def test_every_dev_tool_has_a_risk_and_japanese_text():
    assert set(TOOLS) <= set(tools._DEV_UI)
    for name, (risk, title, action) in tools._DEV_UI.items():
        assert isinstance(risk, ActionRisk) and not title.isascii() and not action.isascii()


def test_network_and_changes_are_never_safe():
    network = {"api_request", "api_check", "inspect_openapi", "outdated_dependencies", "add_dependency",
               "update_dependency", "dependency_vulnerability_scan"}
    changes = {"database_execute", "remove_dependency", "docker_build", "docker_run", "docker_exec",
               "docker_stop", "docker_remove"}
    assert {tools._DEV_UI[n][0] for n in network} == {ActionRisk.EXTERNAL}
    assert {tools._DEV_UI[n][0] for n in changes} == {ActionRisk.EXECUTE}


def test_ci_tools_are_read_only():
    for name in ("ci_info", "ci_workflows", "ci_validate", "ci_check"):
        assert tools.REGISTRY[name].risk == ActionRisk.SAFE and tools.REGISTRY[name].untrusted_result
