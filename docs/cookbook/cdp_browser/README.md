# CDP Browser Cookbook

This cookbook contains copy-and-edit examples for the CoDD `cdp_browser`
verification template. The CoDD core owns the generic plug-in interfaces:

- `BrowserEngine`: resolves a configured browser debug endpoint to a CDP
  WebSocket URL and advertises normalized capabilities.
- `CdpLauncher`: starts, stops, or attaches to the browser process.
- `FormInteractionStrategy`: returns JavaScript snippets for form steps.

The sample files live in git for project teams to copy. They are not part of
the release package because `pyproject.toml` only includes the `codd/` package,
templates, hooks, `README.md`, and `LICENSE`.

## Files

- `launchers/powershell_script.py`: starts a browser through a PowerShell
  script path supplied by an environment variable.
- `launchers/shell_script.py`: starts a browser through a POSIX shell script
  path supplied by an environment variable.
- `launchers/external_running.py`: attaches to a browser that is already
  running and leaves teardown to the caller.
- `engines/edge.py`: resolves an Edge CDP version endpoint.
- `engines/chromium.py`: resolves a Chromium CDP version endpoint.
- `engines/firefox.py`: resolves a CDP-compatible Firefox version endpoint.
- `strategies/react_native_setter.py`: uses the native value setter plus
  bubbling `input` and `change` events.
- `strategies/standard_input_event.py`: uses plain DOM value assignment plus
  bubbling `input` and `change` events.

## Usage

Copy only the plug-ins your project needs into `codd_plugins/`, then make sure
your CoDD bootstrap imports those files so their `@register_*` decorators run
before `codd dag verify` executes.

Example project layout:

```text
codd_plugins/
  cdp_browser/
    engines/edge.py
    launchers/powershell_script.py
    strategies/react_native_setter.py
```

Set environment variables for machine-specific values instead of committing
paths or hosts:

```text
CODD_CDP_POWERSHELL_SCRIPT=<project-owned launcher script>
CODD_CDP_HOST=<debug host>
CODD_CDP_PORT=<debug port>
```

Then declare the selected plug-ins in the project `codd.yaml`:

```yaml
verification:
  templates:
    cdp_browser:
      browser:
        engine: edge
        host_env: CODD_CDP_HOST
        port_env: CODD_CDP_PORT
      launcher:
        kind: powershell_script
        script_path_env: CODD_CDP_POWERSHELL_SCRIPT
        args:
          - "-Port"
          - "${CODD_CDP_PORT}"
      form_strategy:
        kind: react_native_setter
      timeout_seconds: 60
      step_timeout_seconds: 5
```

## Consumer Workflow

1. Copy the needed files from this cookbook into the project's `codd_plugins/`.
2. Import the copied files from the project's CoDD bootstrap so the decorators
   register `edge`, `powershell_script`, and `react_native_setter`.
3. Store browser script paths, debug hosts, and debug ports in environment
   variables or local secret configuration.
4. Declare the selected plug-ins in `codd.yaml`.
5. Run `codd dag verify` against a design document that contains
   `user_journeys` steps such as `navigate`, `fill`, `click`, `form_submit`,
   `expect_url`, `expect_browser_state`, or `expect_dom_visible`.
