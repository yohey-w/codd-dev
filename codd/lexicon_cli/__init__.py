"""CLI helpers for bundled lexicon plug-ins."""

from codd.lexicon_cli.inspector import AxisInspection, LexiconDiffResult, TextHit
from codd.lexicon_cli.manager import InstallResult, LexiconManager, LexiconRecord
from codd.lexicon_cli.reporter import CoverageMatrixReport, CoverageReporter, CoverageRow

__all__ = [
    "AxisInspection",
    "CoverageMatrixReport",
    "CoverageReporter",
    "CoverageRow",
    "InstallResult",
    "LexiconDiffResult",
    "LexiconManager",
    "LexiconRecord",
    "TextHit",
]
