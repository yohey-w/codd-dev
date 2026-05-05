from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from codd.deployment.providers.verification.cdp_engines import BROWSER_ENGINES, BrowserEngine
from codd.deployment.providers.verification.cdp_launchers import CDP_LAUNCHERS, CdpLauncher
from codd.deployment.providers.verification.form_strategies import FORM_STRATEGIES, FormInteractionStrategy


COOKBOOK_ROOT = Path(__file__).resolve().parents[2] / "docs" / "cookbook" / "cdp_browser"
PLUGIN_FILES = [
    COOKBOOK_ROOT / "launchers" / "powershell_script.py",
    COOKBOOK_ROOT / "launchers" / "shell_script.py",
    COOKBOOK_ROOT / "launchers" / "external_running.py",
    COOKBOOK_ROOT / "engines" / "edge.py",
    COOKBOOK_ROOT / "engines" / "chromium.py",
    COOKBOOK_ROOT / "engines" / "firefox.py",
    COOKBOOK_ROOT / "strategies" / "react_native_setter.py",
    COOKBOOK_ROOT / "strategies" / "standard_input_event.py",
]


@pytest.fixture(autouse=True)
def restore_registries():
    engines = dict(BROWSER_ENGINES)
    launchers = dict(CDP_LAUNCHERS)
    strategies = dict(FORM_STRATEGIES)
    yield
    BROWSER_ENGINES.clear()
    BROWSER_ENGINES.update(engines)
    CDP_LAUNCHERS.clear()
    CDP_LAUNCHERS.update(launchers)
    FORM_STRATEGIES.clear()
    FORM_STRATEGIES.update(strategies)


def _load_plugin(path: Path):
    module_name = "cookbook_" + "_".join(path.with_suffix("").parts[-3:])
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_all_plugins() -> None:
    for path in PLUGIN_FILES:
        _load_plugin(path)


def test_all_cookbook_plugin_files_import_and_instantiate():
    _load_all_plugins()

    assert isinstance(BROWSER_ENGINES["edge"](), BrowserEngine)
    assert isinstance(BROWSER_ENGINES["chromium"](), BrowserEngine)
    assert isinstance(BROWSER_ENGINES["firefox"](), BrowserEngine)
    assert isinstance(CDP_LAUNCHERS["powershell_script"](), CdpLauncher)
    assert isinstance(CDP_LAUNCHERS["shell_script"](), CdpLauncher)
    assert isinstance(CDP_LAUNCHERS["external_running"](), CdpLauncher)
    assert isinstance(FORM_STRATEGIES["react_native_setter"](), FormInteractionStrategy)
    assert isinstance(FORM_STRATEGIES["standard_input_event"](), FormInteractionStrategy)


def test_powershell_launcher_uses_script_path_from_env(monkeypatch):
    module = _load_plugin(COOKBOOK_ROOT / "launchers" / "powershell_script.py")
    monkeypatch.setenv("CUSTOM_CDP_SCRIPT", "scripts/launch_debug.ps1")

    command = module.PowerShellScriptLauncher().launch_command(
        {"script_path_env": "CUSTOM_CDP_SCRIPT", "args": ["-Port", "9444"]}
    )

    assert command == [
        "pwsh",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "scripts/launch_debug.ps1",
        "-Port",
        "9444",
    ]


def test_edge_engine_reads_websocket_url_from_version_payload(monkeypatch):
    module = _load_plugin(COOKBOOK_ROOT / "engines" / "edge.py")
    ws_url = "ws://" + "browser-session"

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return ('{"webSocketDebuggerUrl": "' + ws_url + '"}').encode("utf-8")

    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: Response())

    endpoint = module.EdgeBrowserEngine().cdp_endpoint({"host": "configured_host", "port": 9444})

    assert endpoint == ws_url


def test_react_native_setter_returns_js_with_input_event():
    module = _load_plugin(COOKBOOK_ROOT / "strategies" / "react_native_setter.py")

    script = module.ReactNativeSetterStrategy().fill_input_js("input[name=email]", "user value")

    assert isinstance(script, str)
    assert "descriptor.set.call" in script
    assert "input" in script
    assert "user value" in script


def test_standard_input_event_returns_plain_dom_assignment():
    module = _load_plugin(COOKBOOK_ROOT / "strategies" / "standard_input_event.py")

    script = module.StandardInputEventStrategy().fill_input_js("#field", "abc")

    assert "element.value" in script
    assert "dispatchEvent" in script
    assert "#field" in script


def test_cookbook_files_do_not_hardcode_paths_hosts_or_project_names():
    forbidden_tokens = [
        "local" + "host",
        "127" + "." + "0" + "." + "0" + "." + "1",
        "144" + "." + "91" + "." + "125" + "." + "163",
        "os" + "ato",
        "os" + "ato-lms",
        "codd" + "-dev",
    ]
    forbidden = [
        re.compile(r"(?<![A-Za-z0-9_])/(home|mnt|Users|opt|var|tmp|etc|usr|root)/"),
        re.compile(r"[A-Za-z]:\\\\"),
        re.compile(r"\b(" + "|".join(re.escape(token) for token in forbidden_tokens) + r")\b", re.I),
    ]
    files = [*PLUGIN_FILES, COOKBOOK_ROOT / "README.md"]

    violations: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            if pattern.search(text):
                violations.append(f"{path.relative_to(COOKBOOK_ROOT)}: {pattern.pattern}")

    assert violations == []


def test_readme_explains_copy_into_codd_plugins_workflow():
    readme = (COOKBOOK_ROOT / "README.md").read_text(encoding="utf-8")

    assert "codd_plugins/" in readme
    assert "Copy only the plug-ins your project needs" in readme
