"""Smoke tests: verify every agent module can be imported without errors."""

import importlib

import pytest

AGENT_MODULES = [
    "s01_agent_loop",
    "s02_tool_system",
    "s03_session_store",
    "s04_prompt_builder",
    "s05_context_compression",
    "s06_error_recovery",
    "s07_memory_system",
    "s08_skill_system",
    "s09_permission_system",
    "s10_subagent_delegation",
    "s11_configuration_system",
    "s12_gateway_architecture",
    "s13_platform_adapters",
    "s14_terminal_backends",
    "s15_scheduled_tasks",
    "s16_mcp",
    "s17_browser_automation",
    "s18_voice_vision",
    "s19_cli_and_web_interface",
    "s20_background_review",
    "s21_skill_creation_loop",
    "s22_hook_system",
    "s23_trajectory_and_rl",
    "s25_skill_evolution",
    "s26_evaluation_system",
    "s27_optimization_and_deploy",
]


@pytest.mark.parametrize("module", AGENT_MODULES)
def test_import(module: str) -> None:
    """Each agent module should import without raising."""
    importlib.import_module(f"agents.{module}")
