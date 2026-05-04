from __future__ import annotations

from pathlib import Path
import re

import yaml
from click.testing import CliRunner

from codd.cli import main
from codd.screen_transition_extractor import (
    ScreenTransition,
    extract_transitions,
    load_transition_patterns,
    write_screen_transitions_yaml,
)


ROUTE_CONFIG = {
    "base_dir": "app/",
    "page_pattern": "page.{tsx,jsx}",
    "api_pattern": "route.{ts,js}",
    "url_template": "/{relative_dir}",
    "dynamic_segment": {"from": r"\[(.+)\]", "to": r":$1"},
    "ignore_segment": [r"\(.*\)"],
}


def _write_codd_config(project: Path, config: dict | None = None) -> None:
    codd_dir = project / "codd"
    codd_dir.mkdir(exist_ok=True)
    payload = {"filesystem_routes": [ROUTE_CONFIG]}
    if config:
        payload.update(config)
    (codd_dir / "codd.yaml").write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_source(project: Path, relative_path: str, content: str) -> Path:
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_load_transition_patterns_defaults_loaded(tmp_path):
    patterns = load_transition_patterns(tmp_path)

    assert patterns
    assert {"kind": "link", "ast_node": "jsx_element", "attr": "href", "element": "Link"} in patterns
    assert any(pattern.get("callee") == "redirect" for pattern in patterns)


def test_load_transition_patterns_codd_yaml_override(tmp_path):
    _write_codd_config(
        tmp_path,
        {
            "screen_transitions": {
                "custom": [{"kind": "redirect", "ast_node": "call_expression", "callee": "customNavigate"}]
            }
        },
    )

    patterns = load_transition_patterns(tmp_path)

    assert any(pattern.get("callee") == "redirect" for pattern in patterns)
    assert any(pattern.get("callee") == "customNavigate" for pattern in patterns)


def test_load_transition_patterns_replace_defaults(tmp_path):
    _write_codd_config(
        tmp_path,
        {
            "screen_transitions": {
                "replace_defaults": True,
                "patterns": [{"kind": "redirect", "ast_node": "call_expression", "callee": "go"}],
            }
        },
    )

    patterns = load_transition_patterns(tmp_path)

    assert patterns == [{"kind": "redirect", "ast_node": "call_expression", "callee": "go"}]


def test_extract_link_transition(tmp_path):
    _write_codd_config(tmp_path)
    _write_source(
        tmp_path,
        "app/login/page.tsx",
        'import Link from "next/link";\nexport default function Page() { return <Link href="/dashboard">Dashboard</Link>; }\n',
    )

    transitions = extract_transitions(tmp_path, ["app"])

    assert len(transitions) == 1
    assert transitions[0].from_route == "/login"
    assert transitions[0].to_route == "/dashboard"
    assert transitions[0].kind == "link"


def test_extract_redirect_transition(tmp_path):
    _write_codd_config(tmp_path)
    _write_source(
        tmp_path,
        "app/admin/page.tsx",
        'import { redirect } from "next/navigation";\nexport default function Page() { redirect("/login"); }\n',
    )

    transitions = extract_transitions(tmp_path, ["app"])

    assert [(transition.from_route, transition.to_route, transition.kind) for transition in transitions] == [
        ("/admin", "/login", "redirect")
    ]


def test_extract_router_push_transition(tmp_path):
    _write_codd_config(tmp_path)
    _write_source(
        tmp_path,
        "app/settings/page.tsx",
        '"use client";\nexport default function Page({ router }) { router.push("/profile"); }\n',
    )

    transitions = extract_transitions(tmp_path, ["app"])

    assert len(transitions) == 1
    assert transitions[0].from_route == "/settings"
    assert transitions[0].to_route == "/profile"
    assert transitions[0].kind == "router_push"


def test_extract_signin_callback_url_transition(tmp_path):
    _write_codd_config(tmp_path)
    _write_source(
        tmp_path,
        "app/login/page.tsx",
        'export default function Page() { signIn("credentials", { callbackUrl: "/dashboard" }); }\n',
    )

    transitions = extract_transitions(tmp_path, ["app"])

    assert len(transitions) == 1
    assert transitions[0].to_route == "/dashboard"
    assert transitions[0].kind == "signin_cb"


def test_write_screen_transitions_yaml(tmp_path):
    output = tmp_path / "docs" / "extracted" / "screen-transitions.yaml"
    transitions = [
        ScreenTransition(
            from_route="/login",
            to_route="/dashboard",
            trigger="Link[href]",
            kind="link",
            source_file="app/login/page.tsx",
            source_line=3,
        )
    ]

    write_screen_transitions_yaml(transitions, output)

    data = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert data["edges"][0]["from"] == "/login"
    assert data["edges"][0]["to"] == "/dashboard"
    assert data["edges"][0]["type"] == "link"


def test_generality_no_framework_hardcode():
    source = Path("codd/screen_transition_extractor.py").read_text(encoding="utf-8")

    assert not re.search(r"if .*next", source, re.IGNORECASE)
    assert not re.search(r"if .*nuxt", source, re.IGNORECASE)
    assert not re.search(r"if .*sveltekit", source, re.IGNORECASE)


def test_extract_returns_empty_on_no_matches(tmp_path):
    _write_codd_config(tmp_path)
    _write_source(tmp_path, "app/page.tsx", "export default function Page() { return null; }\n")

    assert extract_transitions(tmp_path, ["app"]) == []


def test_defaults_yaml_covers_multiple_frameworks():
    data = yaml.safe_load(Path("codd/screen_transitions/defaults.yaml").read_text(encoding="utf-8"))

    assert len(data["frameworks"]) >= 3
    assert all(config.get("patterns") for config in data["frameworks"].values())


def test_extract_routes_edges_cli_writes_default_yaml(tmp_path):
    _write_codd_config(tmp_path)
    _write_source(
        tmp_path,
        "app/login/page.tsx",
        'export default function Page({ router }) { router.push("/profile"); }\n',
    )
    runner = CliRunner()

    result = runner.invoke(main, ["extract", "--path", str(tmp_path), "--layer", "routes-edges"])

    output = tmp_path / "docs" / "extracted" / "screen-transitions.yaml"
    assert result.exit_code == 0
    assert "Extracted 1 screen transitions" in result.output
    assert yaml.safe_load(output.read_text(encoding="utf-8"))["edges"][0]["to"] == "/profile"
