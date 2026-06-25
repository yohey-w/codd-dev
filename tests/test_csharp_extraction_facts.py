"""C# extract/scan facts (Increment 1, Piece 1 — scan-side).

* ``_symbols_csharp`` captures ``class`` / ``struct`` / ``interface`` / ``enum`` /
  ``record``.
* ``_imports_csharp`` classifies first-party ``using X.Y;`` as ``internal`` vs
  framework ``using System.*`` / ``Microsoft.*`` as ``external``; it covers
  plain / ``static`` / ``global`` / alias using forms, and namespace declarations
  in both file-scoped (``namespace X.Y;``) and block (``namespace X.Y {``) form.
* A C# scanner CEG resolver materializes NAMESPACE-keyed ``module:`` import nodes
  (modeled on the Java module-node resolver, NOT the C++/TS path-based file
  resolver — C# resolution is namespace-based; the DAG builder owns precise
  namespace→file edges via the reverse-index).
"""

from __future__ import annotations

from codd.extractor import _extract_imports, _extract_symbols
from codd.parsing.regex_strategies import (
    _CEG_IMPORT_RESOLVERS,
    _STRATEGIES,
    ceg_import_targets,
    strategy_for,
)


CSHARP_TYPES_SRC = """\
using System;
using Dapper.Internal;

namespace Demo.Models;

public class Service
{
    public int DoWork(int n) => n;
}

public struct Point
{
    public int X;
    public int Y;
}

public interface IRepo
{
    void Save();
}

public enum Color { Red, Green }

public record Money(decimal Amount);

internal sealed class Helper { }
"""


def _symbol_kinds(symbols) -> dict[str, str]:
    return {s.name: s.kind for s in symbols}


def test_csharp_strategy_registered():
    assert "csharp" in _STRATEGIES
    assert ".cs" in strategy_for("csharp").extensions


def test_csharp_language_extensions_nonempty():
    """``language_extensions('csharp')`` must report ``.cs`` so source-dir
    auto-detection (which keys on the strategy's extensions) finds C# dirs."""
    from codd.parsing.regex_strategies import language_extensions

    assert ".cs" in language_extensions("csharp")


def test_csharp_symbols_capture_class_struct_interface_enum_record():
    kinds = _symbol_kinds(_extract_symbols(CSHARP_TYPES_SRC, "service.cs", "csharp"))
    assert kinds.get("Service") == "class"
    assert kinds.get("Point") == "struct"
    assert kinds.get("IRepo") == "interface"
    assert kinds.get("Color") == "enum"
    assert kinds.get("Money") == "class"  # record collapses to class
    assert kinds.get("Helper") == "class"  # modifiers tolerated


CSHARP_IMPORTS_SRC = """\
using System;
using System.Collections.Generic;
using Microsoft.Extensions.Logging;
using Dapper.ProviderTools;
using Dapper.ProviderTools.Internal;
using static Dapper.SqlMapper;
global using Dapper.Contrib.Extensions;
using Alias = Dapper.Rainbow;
"""


def test_csharp_imports_classify_internal_vs_external(tmp_path):
    internal, external = _extract_imports(
        CSHARP_IMPORTS_SRC, "csharp", tmp_path, tmp_path / "app", tmp_path / "app" / "service.cs"
    )
    internal_lines = [line for lines in internal.values() for line in lines]
    # First-party (Dapper.*) → internal, all forms (plain/static/global/alias).
    assert any("Dapper.ProviderTools" in line for line in internal_lines)
    assert any("static Dapper.SqlMapper" in line for line in internal_lines)
    assert any("global using Dapper.Contrib.Extensions" in line for line in internal_lines)
    assert any("Dapper.Rainbow" in line for line in internal_lines)  # alias RHS
    # Framework/BCL → external.
    assert "System" in external
    assert "System.Collections.Generic" in external
    assert "Microsoft.Extensions.Logging" in external
    # No first-party namespace leaked into external.
    assert not any("Dapper" in ext for ext in external)


def test_csharp_ceg_resolver_registered_and_resolves_module_nodes(tmp_path):
    """C# CEG resolver produces NAMESPACE-keyed ``module:`` nodes (like Java)."""
    assert "csharp" in _CEG_IMPORT_RESOLVERS

    internal = {
        "Dapper": [
            "using Dapper.ProviderTools;",
            "using static Dapper.SqlMapper;",
            "using Alias = Dapper.Rainbow;",
        ]
    }
    targets = ceg_import_targets(
        "csharp", internal, tmp_path, tmp_path / "app" / "service.cs"
    )
    ids = {t.target_id for t in targets}
    node_types = {t.node_type for t in targets}
    assert "module:Dapper.ProviderTools" in ids, f"namespace module node missing; ids={ids}"
    assert "module:Dapper.SqlMapper" in ids, f"static-using namespace node missing; ids={ids}"
    assert "module:Dapper.Rainbow" in ids, f"alias-using namespace node missing; ids={ids}"
    assert node_types == {"module"}, f"C# CEG nodes must be module:, got {node_types}"


def test_csharp_ceg_resolver_no_internal_means_no_targets(tmp_path):
    """No first-party usings → no module nodes (framework usings never reach
    ``internal``)."""
    targets = ceg_import_targets(
        "csharp", {}, tmp_path, tmp_path / "app" / "service.cs"
    )
    assert targets == [], f"empty internal must yield no targets; {targets}"
