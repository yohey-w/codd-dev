"""Tests for the central project-type registry and capability model."""

from __future__ import annotations

from pathlib import Path

import yaml

from codd.project_types import (
    GENERIC_PROJECT_TYPE,
    ProjectCapabilities,
    SHIPPED_DEFAULTS_DIR,
    is_known_project_type,
    load_capabilities,
    resolve_project_type,
    supported_project_types,
)


# --- discovery / extensibility -------------------------------------------------


def test_supported_types_are_discovered_from_shipped_profiles():
    discovered = {path.stem for path in SHIPPED_DEFAULTS_DIR.glob("*.yaml")}
    supported = set(supported_project_types())
    # Every shipped <type>.yaml is registered (discovery, not enumeration).
    assert discovered <= supported
    # The historical four plus generic are all present.
    for expected in ("web", "cli", "mobile", "iot", "generic"):
        assert expected in supported


def test_generic_always_included():
    assert GENERIC_PROJECT_TYPE in supported_project_types()


def test_project_local_type_registers_without_core_edit(tmp_path):
    # Convention dir: <root>/required_artifacts_defaults/<name>.yaml
    local_dir = tmp_path / "required_artifacts_defaults"
    local_dir.mkdir()
    (local_dir / "embedded.yaml").write_text(
        "project_type: embedded\ndefault_artifacts: []\n", encoding="utf-8"
    )

    supported = supported_project_types(tmp_path)

    assert "embedded" in supported
    assert is_known_project_type("embedded", tmp_path)
    # Without the project root, the ad-hoc type is unknown.
    assert "embedded" not in supported_project_types()


def test_type_defaults_dir_pointer_in_codd_yaml(tmp_path):
    codd_dir = tmp_path / ".codd"
    codd_dir.mkdir()
    custom_dir = tmp_path / "my_types"
    custom_dir.mkdir()
    (custom_dir / "robotics.yaml").write_text(
        "project_type: robotics\ndefault_artifacts: []\n", encoding="utf-8"
    )
    (codd_dir / "codd.yaml").write_text(
        "project:\n  type_defaults_dir: my_types\n", encoding="utf-8"
    )

    assert "robotics" in supported_project_types(tmp_path)


# --- resolve_project_type ------------------------------------------------------


def test_resolve_known_configured_type_wins():
    resolved, reason = resolve_project_type("web", detected="cli")
    assert resolved == "web"
    assert "web" in reason


def test_resolve_unknown_configured_type_is_generic_not_web():
    resolved, reason = resolve_project_type("library", detected="web")
    assert resolved == GENERIC_PROJECT_TYPE
    assert resolved != "web"
    assert "library" in reason
    assert "generic" in reason


def test_resolve_uses_detected_when_no_configured():
    resolved, reason = resolve_project_type(None, detected="cli")
    assert resolved == "cli"
    assert "cli" in reason


def test_resolve_unknown_detected_is_generic():
    resolved, _ = resolve_project_type(None, detected="quantum")
    assert resolved == GENERIC_PROJECT_TYPE


def test_resolve_nothing_is_generic():
    resolved, _ = resolve_project_type(None, None)
    assert resolved == GENERIC_PROJECT_TYPE


def test_resolve_custom_passes_through():
    resolved, reason = resolve_project_type("custom")
    assert resolved == "custom"
    assert "custom" in reason


# --- capability loading --------------------------------------------------------


def test_load_capabilities_web():
    caps = load_capabilities("web")
    assert caps.user_interface is True
    assert caps.network_surface == "http"
    assert caps.e2e_modality == "browser"
    assert caps.long_running_service is True


def test_load_capabilities_cli():
    caps = load_capabilities("cli")
    assert caps == ProjectCapabilities(
        user_interface=False,
        network_surface="none",
        e2e_modality="cli",
        long_running_service=False,
    )


def test_load_capabilities_mobile():
    caps = load_capabilities("mobile")
    assert caps.user_interface is True
    assert caps.e2e_modality == "device"
    assert caps.long_running_service is False


def test_load_capabilities_iot():
    caps = load_capabilities("iot")
    assert caps.user_interface is False
    assert caps.network_surface == "http"
    assert caps.e2e_modality == "device"


def test_load_capabilities_generic_is_conservative():
    caps = load_capabilities("generic")
    assert caps == ProjectCapabilities()  # conservative defaults
    assert caps.user_interface is False
    assert caps.network_surface == "none"
    assert caps.e2e_modality == "cli"
    assert caps.long_running_service is False


def test_capabilities_default_matches_generic_profile():
    # The dataclass defaults must equal the shipped generic.yaml capabilities.
    payload = yaml.safe_load(
        (SHIPPED_DEFAULTS_DIR / "generic.yaml").read_text(encoding="utf-8")
    )
    block = payload["capabilities"]
    defaults = ProjectCapabilities()
    assert block["user_interface"] is defaults.user_interface
    assert block["network_surface"] == defaults.network_surface
    assert block["e2e_modality"] == defaults.e2e_modality
    assert block["long_running_service"] is defaults.long_running_service


def test_load_capabilities_unknown_type_returns_conservative_defaults():
    assert load_capabilities("does_not_exist") == ProjectCapabilities()


def test_load_capabilities_profile_without_block_returns_defaults(tmp_path):
    local_dir = tmp_path / "required_artifacts_defaults"
    local_dir.mkdir()
    (local_dir / "plain.yaml").write_text(
        "project_type: plain\ndefault_artifacts: []\n", encoding="utf-8"
    )

    caps = load_capabilities("plain", tmp_path)

    assert caps == ProjectCapabilities()


def test_load_capabilities_project_local_override_wins(tmp_path):
    local_dir = tmp_path / "required_artifacts_defaults"
    local_dir.mkdir()
    # Override 'web' locally with different capabilities.
    (local_dir / "web.yaml").write_text(
        "project_type: web\n"
        "capabilities:\n"
        "  user_interface: false\n"
        "  network_surface: none\n"
        "  e2e_modality: cli\n"
        "  long_running_service: false\n"
        "default_artifacts: []\n",
        encoding="utf-8",
    )

    caps = load_capabilities("web", tmp_path)

    assert caps.user_interface is False  # local override beat shipped web profile
    # Shipped web (no project root) is unaffected.
    assert load_capabilities("web").user_interface is True


# --- all shipped profiles declare valid capabilities ---------------------------


def test_all_shipped_profiles_have_valid_capabilities():
    for path in SHIPPED_DEFAULTS_DIR.glob("*.yaml"):
        caps = load_capabilities(path.stem)
        assert caps.network_surface in {"http", "none"}
        assert caps.e2e_modality in {"browser", "cli", "device", "none"}
        assert isinstance(caps.user_interface, bool)
        assert isinstance(caps.long_running_service, bool)
