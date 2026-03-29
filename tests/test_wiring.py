"""Tests for R5.3 — Runtime wiring detection (wiring.py)."""

import textwrap
from pathlib import Path

import pytest

from codd.wiring import RuntimeWire, detect_runtime_wires, build_runtime_wires
from codd.extractor import ModuleInfo, ProjectFacts


# ── Helper ──────────────────────────────────────────────────

def make_facts(*modules: ModuleInfo) -> ProjectFacts:
    facts = ProjectFacts(language="python", source_dirs=["src"])
    for mod in modules:
        facts.modules[mod.name] = mod
    return facts


# ── RuntimeWire dataclass ────────────────────────────────────

def test_runtime_wire_fields():
    wire = RuntimeWire(kind="depends", source="api.py:10", target="get_db", framework="fastapi")
    assert wire.kind == "depends"
    assert wire.source == "api.py:10"
    assert wire.target == "get_db"
    assert wire.framework == "fastapi"


# ── FastAPI Depends ──────────────────────────────────────────

def test_detect_fastapi_depends_simple():
    content = "async def get_items(db: Session = Depends(get_db)):\n    pass\n"
    wires = detect_runtime_wires(content, "api/routes.py")
    deps = [w for w in wires if w.kind == "depends"]
    assert len(deps) == 1
    assert deps[0].target == "get_db"
    assert deps[0].framework == "fastapi"


def test_detect_fastapi_depends_multiple_on_same_line():
    content = "def endpoint(a=Depends(auth), b=Depends(rate_limit)):\n    pass\n"
    wires = detect_runtime_wires(content, "api/v1.py")
    deps = [w for w in wires if w.kind == "depends"]
    targets = {w.target for w in deps}
    assert "auth" in targets
    assert "rate_limit" in targets


def test_detect_fastapi_depends_dotted_target():
    content = "def view(svc=Depends(services.get_session)):\n    ...\n"
    wires = detect_runtime_wires(content, "views.py")
    deps = [w for w in wires if w.kind == "depends"]
    assert any(w.target == "services.get_session" for w in deps)


# ── Django signals ───────────────────────────────────────────

def test_detect_django_post_save_signal():
    content = "post_save.connect(on_user_created, sender=User)\n"
    wires = detect_runtime_wires(content, "signals.py")
    signals = [w for w in wires if w.kind == "signal" and w.framework == "django"]
    assert len(signals) == 1
    assert signals[0].target == "on_user_created"


def test_detect_django_pre_delete_signal():
    content = "pre_delete.connect(cleanup_handler)\n"
    wires = detect_runtime_wires(content, "signals.py")
    signals = [w for w in wires if w.kind == "signal" and w.framework == "django"]
    assert any(w.target == "cleanup_handler" for w in signals)


def test_detect_django_non_signal_connect_not_captured():
    """connect() on an unrecognized signal name should not produce a signal wire."""
    content = "my_custom_event.connect(handler)\n"
    wires = detect_runtime_wires(content, "custom.py")
    django_signals = [w for w in wires if w.kind == "signal" and w.framework == "django"]
    assert django_signals == []


# ── Django MIDDLEWARE ────────────────────────────────────────

def test_detect_django_middleware():
    content = textwrap.dedent("""\
        MIDDLEWARE = [
            'django.middleware.security.SecurityMiddleware',
            'django.contrib.sessions.middleware.SessionMiddleware',
        ]
    """)
    wires = detect_runtime_wires(content, "settings.py")
    mw_wires = [w for w in wires if w.kind == "middleware"]
    targets = {w.target for w in mw_wires}
    assert "django.middleware.security.SecurityMiddleware" in targets
    assert "django.contrib.sessions.middleware.SessionMiddleware" in targets
    assert all(w.framework == "django" for w in mw_wires)


# ── Flask hooks ──────────────────────────────────────────────

def test_detect_flask_before_request():
    content = "@app.before_request\ndef check_auth():\n    pass\n"
    wires = detect_runtime_wires(content, "app.py")
    hooks = [w for w in wires if w.kind == "decorator" and w.framework == "flask"]
    assert len(hooks) == 1
    assert hooks[0].target == "before_request"


def test_detect_flask_after_request():
    content = "@bp.after_request\ndef add_cors(response):\n    return response\n"
    wires = detect_runtime_wires(content, "blueprints.py")
    hooks = [w for w in wires if w.kind == "decorator" and w.framework == "flask"]
    assert any(w.target == "after_request" for w in hooks)


def test_detect_flask_teardown_appcontext():
    content = "@app.teardown_appcontext\ndef close_db(exc):\n    pass\n"
    wires = detect_runtime_wires(content, "app.py")
    hooks = [w for w in wires if w.kind == "decorator" and w.framework == "flask"]
    assert any(w.target == "teardown_appcontext" for w in hooks)


# ── Celery tasks ─────────────────────────────────────────────

def test_detect_celery_task_decorator():
    content = "@celery_app.task\ndef send_email(to, subject):\n    pass\n"
    wires = detect_runtime_wires(content, "tasks.py")
    tasks = [w for w in wires if w.kind == "task" and w.framework == "celery"]
    assert len(tasks) == 1
    assert tasks[0].target == "celery_task"


def test_detect_celery_shared_task():
    content = "@shared.task\ndef process_data():\n    pass\n"
    wires = detect_runtime_wires(content, "workers.py")
    tasks = [w for w in wires if w.kind == "task" and w.framework == "celery"]
    assert len(tasks) == 1


# ── Generic event handlers ───────────────────────────────────

def test_detect_generic_on_event():
    content = "app.on_event('startup', startup_handler)\n"
    wires = detect_runtime_wires(content, "main.py")
    generic = [w for w in wires if w.framework == "generic"]
    assert any(w.target == "startup_handler" for w in generic)


def test_detect_no_wires_plain_code():
    """Plain Python code without wiring patterns yields no wires."""
    content = textwrap.dedent("""\
        def add(a, b):
            return a + b

        class Calculator:
            def multiply(self, x, y):
                return x * y
    """)
    wires = detect_runtime_wires(content, "calc.py")
    assert wires == []


def test_detect_source_includes_line_number():
    """source field must contain the file path and line number."""
    content = "post_save.connect(my_handler)\n"
    wires = detect_runtime_wires(content, "signals.py")
    assert wires[0].source == "signals.py:1"


# ── build_runtime_wires integration ─────────────────────────

def test_build_runtime_wires_populates_module(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "api.py").write_text("def view(db=Depends(get_db)): pass\n")

    mod = ModuleInfo(name="api")
    mod.files = ["src/api.py"]
    facts = make_facts(mod)

    build_runtime_wires(facts, tmp_path)

    assert len(mod.runtime_wires) >= 1
    assert any(w.framework == "fastapi" for w in mod.runtime_wires)


def test_build_runtime_wires_missing_file_skipped(tmp_path):
    mod = ModuleInfo(name="ghost")
    mod.files = ["src/nonexistent.py"]
    facts = make_facts(mod)

    build_runtime_wires(facts, tmp_path)

    assert mod.runtime_wires == []


def test_build_runtime_wires_multiple_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "routes.py").write_text("def ep(db=Depends(get_db)): pass\n")
    (src / "tasks.py").write_text("@worker.task\ndef job(): pass\n")

    mod = ModuleInfo(name="app")
    mod.files = ["src/routes.py", "src/tasks.py"]
    facts = make_facts(mod)

    build_runtime_wires(facts, tmp_path)

    kinds = {w.kind for w in mod.runtime_wires}
    frameworks = {w.framework for w in mod.runtime_wires}
    assert "depends" in kinds
    assert "task" in kinds
    assert "fastapi" in frameworks
    assert "celery" in frameworks
