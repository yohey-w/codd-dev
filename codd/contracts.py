"""R4.3 — Interface contract detection for codd extract.

Distinguishes public API (symbols in __init__.py / __all__) from internal
implementation details.  Detects encapsulation violations where other modules
reach into internals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codd.extractor import ProjectFacts


@dataclass
class InterfaceContract:
    """Public vs internal API surface for a module."""

    module: str
    public_symbols: list[str] = field(default_factory=list)
    internal_symbols: list[str] = field(default_factory=list)
    api_surface_ratio: float = 0.0
    encapsulation_violations: list[str] = field(default_factory=list)


# ── __init__.py / __all__ parsing ────────────────────────

_ALL_RE = re.compile(
    r"__all__\s*=\s*\[([^\]]*)\]",
    re.DOTALL,
)

_REEXPORT_FROM_RE = re.compile(
    r"^from\s+\.[\w.]*\s+import\s+(.+)",
    re.MULTILINE,
)

_IMPORT_AS_RE = re.compile(r"(\w+)\s+as\s+(\w+)")


def detect_init_exports(init_content: str) -> list[str]:
    """Parse ``__init__.py`` content and return publicly-exported symbol names."""
    names: list[str] = []

    # 1) __all__ takes priority
    m = _ALL_RE.search(init_content)
    if m:
        raw = m.group(1)
        for token in re.findall(r"""['"](\w+)['"]""", raw):
            if token not in names:
                names.append(token)
        return names

    # 2) Fall back to ``from .xxx import ...`` re-exports
    for m2 in _REEXPORT_FROM_RE.finditer(init_content):
        import_part = m2.group(1).strip().rstrip(")")
        for chunk in import_part.split(","):
            chunk = chunk.strip().strip("()")
            if not chunk:
                continue
            # handle "Foo as Bar" → the exported name is "Bar"
            alias_m = _IMPORT_AS_RE.search(chunk)
            if alias_m:
                name = alias_m.group(2)
            else:
                name = chunk.split()[-1]
            if name.isidentifier() and name not in names:
                names.append(name)

    return names


# ── Build contracts for every module ─────────────────────

def build_interface_contracts(facts: ProjectFacts, project_root: Path) -> None:
    """Populate ``interface_contract`` on every module in *facts*."""
    from codd.extractor import _language_extensions  # avoid circular at import time

    # First pass: compute public/internal for each module
    for mod in facts.modules.values():
        init_files = [
            f for f in mod.files
            if Path(f).name == "__init__.py"
        ]
        all_symbol_names = [s.name for s in mod.symbols]
        if not all_symbol_names:
            continue

        public: list[str] = []
        if init_files:
            init_path = project_root / init_files[0]
            try:
                init_content = init_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                init_content = ""
            public = detect_init_exports(init_content)

        # For single-file modules (no __init__.py), treat all symbols as public
        if not init_files:
            public = list(all_symbol_names)

        internal = [n for n in all_symbol_names if n not in public]
        total = len(all_symbol_names)
        ratio = len(public) / total if total else 0.0

        mod.interface_contract = InterfaceContract(
            module=mod.name,
            public_symbols=public,
            internal_symbols=internal,
            api_surface_ratio=round(ratio, 2),
        )

    # Second pass: detect encapsulation violations
    # Build internal-symbol lookup: {module_name: set(internal_names)}
    internal_lookup: dict[str, set[str]] = {}
    for mod in facts.modules.values():
        if mod.interface_contract:
            internal_lookup[mod.name] = set(mod.interface_contract.internal_symbols)

    for mod in facts.modules.values():
        if not mod.interface_contract:
            continue
        for dep_name, import_lines in mod.internal_imports.items():
            if dep_name not in internal_lookup:
                continue
            internals = internal_lookup[dep_name]
            if not internals:
                continue
            for line in import_lines:
                for internal_name in internals:
                    if internal_name in line:
                        violation = f"{mod.name} uses {dep_name}.{internal_name} (internal)"
                        if violation not in mod.interface_contract.encapsulation_violations:
                            mod.interface_contract.encapsulation_violations.append(violation)
