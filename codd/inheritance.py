"""R9 — Inheritance chain analysis for codd extract.

Builds a project-wide inheritance graph from Symbol.bases fields,
resolves parent classes within the project, and surfaces overrides
and silently-inherited methods (the dangerous ones).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codd.extractor import ProjectFacts


# ═══════════════════════════════════════════════════════════
# Data structures
# ═══════════════════════════════════════════════════════════

@dataclass
class InheritanceEdge:
    """A resolved parent→child relationship between two project classes."""
    child_class: str    # "module.ChildClass"
    parent_class: str   # "module.ParentClass"
    child_module: str   # module name
    parent_module: str  # module name
    child_file: str     # "file:line"
    parent_file: str    # "file:line"


# Base class names that are stdlib / third-party and should be skipped
_BUILTIN_BASES: frozenset[str] = frozenset({
    "object",
    "ABC",
    "ABCMeta",
    "Protocol",
    "Base",
    "Model",
    "TestCase",
    "Exception",
    "BaseException",
    "Enum",
    "IntEnum",
    "StrEnum",
    "TypedDict",
    "NamedTuple",
    "BaseModel",
})


# ═══════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════

def build_inheritance_tree(facts: ProjectFacts) -> None:
    """Populate ``facts.inheritance_edges`` by resolving Symbol.bases across modules.

    Steps:
    1. Build a project-wide symbol table: {name: (module_name, Symbol)} for all classes.
       Both simple name ("Foo") and qualified name ("mymodule.Foo") are indexed.
    2. For each class with non-empty bases, try to resolve each base name.
    3. Skip unresolved bases (stdlib / third-party).
    4. Emit one InheritanceEdge per resolved parent.
    """
    # Step 1: build symbol table
    # Keys: simple name AND qualified "module.Name"
    symbol_table: dict[str, tuple[str, object]] = {}  # name -> (module_name, Symbol)

    for mod_name, mod in facts.modules.items():
        for sym in mod.symbols:
            if sym.kind != "class":
                continue
            # Simple name (may collide — last writer wins, acceptable heuristic)
            symbol_table[sym.name] = (mod_name, sym)
            # Qualified name (unique by construction)
            qualified = f"{mod_name}.{sym.name}"
            symbol_table[qualified] = (mod_name, sym)

    # Step 2: walk all classes and resolve bases
    edges: list[InheritanceEdge] = []

    for mod_name, mod in facts.modules.items():
        for sym in mod.symbols:
            if sym.kind != "class" or not sym.bases:
                continue

            child_qualified = f"{mod_name}.{sym.name}"
            child_file = f"{sym.file}:{sym.line}"

            for base_name in sym.bases:
                # Strip leading whitespace / newlines that parsers sometimes leave
                base_name = base_name.strip()
                if not base_name:
                    continue

                # Extract the simple leaf name for builtin check
                # e.g. "models.Base" → "Base", "Base" → "Base"
                leaf = base_name.split(".")[-1]

                if leaf in _BUILTIN_BASES or base_name in _BUILTIN_BASES:
                    continue

                # Attempt resolution: qualified first, then simple name
                resolved = symbol_table.get(base_name) or symbol_table.get(leaf)
                if resolved is None:
                    # Also try prefixing with current module
                    # e.g. base_name="Helper" lives in same module
                    same_mod_qualified = f"{mod_name}.{base_name}"
                    resolved = symbol_table.get(same_mod_qualified)

                if resolved is None:
                    # Unresolved → stdlib or third-party, skip
                    continue

                parent_mod_name, parent_sym = resolved
                parent_qualified = f"{parent_mod_name}.{parent_sym.name}"
                parent_file = f"{parent_sym.file}:{parent_sym.line}"

                # Avoid self-loops (can happen with re-exports)
                if child_qualified == parent_qualified:
                    continue

                edges.append(InheritanceEdge(
                    child_class=child_qualified,
                    parent_class=parent_qualified,
                    child_module=mod_name,
                    parent_module=parent_mod_name,
                    child_file=child_file,
                    parent_file=parent_file,
                ))

    facts.inheritance_edges = edges


def get_overrides(facts: ProjectFacts) -> dict[str, list[str]]:
    """Return methods explicitly overridden by each child class.

    For each InheritanceEdge, a method is an override when its name appears
    in both the child class and the parent class.

    Returns:
        {child_qualified_name: [overridden_method_names]}
    """
    result: dict[str, list[str]] = {}

    for edge in getattr(facts, "inheritance_edges", []):
        child_methods = _method_names_for(facts, edge.child_class)
        parent_methods = _method_names_for(facts, edge.parent_class)

        overrides = sorted(child_methods & parent_methods)
        if overrides:
            existing = result.get(edge.child_class, [])
            # Merge without duplicates (multiple parents)
            merged = sorted(set(existing) | set(overrides))
            result[edge.child_class] = merged

    return result


def get_inherited_methods(facts: ProjectFacts) -> dict[str, list[str]]:
    """Return methods inherited from parent but NOT overridden by child.

    These are the dangerous ones: if a parent changes them, the child breaks
    silently without any local edit.

    Returns:
        {child_qualified_name: [inherited_method_names]}
    """
    result: dict[str, list[str]] = {}

    for edge in getattr(facts, "inheritance_edges", []):
        child_methods = _method_names_for(facts, edge.child_class)
        parent_methods = _method_names_for(facts, edge.parent_class)

        inherited = sorted(parent_methods - child_methods)
        if inherited:
            existing = result.get(edge.child_class, [])
            merged = sorted(set(existing) | set(inherited))
            result[edge.child_class] = merged

    return result


# ═══════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════

def _method_names_for(facts: ProjectFacts, qualified_class: str) -> set[str]:
    """Return the set of method names defined directly on *qualified_class*.

    ``qualified_class`` is "module_name.ClassName".  We look up the module,
    then find the matching class Symbol, and collect the names of any nested
    function Symbols (i.e., methods) or symbols whose kind is "function" and
    whose name starts with the class prefix (extractor style varies).

    Because Symbol does not carry a ``methods`` field directly, we infer
    methods from symbols that share the same parent class via naming convention
    "ClassName.method_name" stored in ``mod.symbols``.
    """
    parts = qualified_class.split(".", 1)
    if len(parts) != 2:
        return set()
    mod_name, class_name = parts

    mod = facts.modules.get(mod_name)
    if mod is None:
        return set()

    methods: set[str] = set()
    prefix = f"{class_name}."

    for sym in mod.symbols:
        # Convention 1: symbol name is "ClassName.method_name"
        if sym.kind == "function" and sym.name.startswith(prefix):
            method_name = sym.name[len(prefix):]
            if method_name:
                methods.add(method_name)
        # Convention 2: symbol kind is "class" with nested children encoded
        # as separate Symbol entries named "method_name" under the same file/line
        # block — not reliably distinguishable without a parent field, so skip.

    return methods
