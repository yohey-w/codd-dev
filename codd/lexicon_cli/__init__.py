"""CLI helpers for bundled lexicon plug-ins."""

from codd.lexicon_cli.inspector import AxisInspection, LexiconDiffResult, TextHit
from codd.lexicon_cli.manager import InstallResult, LexiconManager, LexiconRecord
from codd.lexicon_cli.reporter import CoverageMatrixReport, CoverageReporter, CoverageRow
from codd.lexicon_cli.threshold import CoverageViolation, ThresholdConfig

__all__ = [
    "AxisInspection",
    "CoverageMatrixReport",
    "CoverageReporter",
    "CoverageRow",
    "CoverageViolation",
    "InstallResult",
    "LexiconDiffResult",
    "LexiconManager",
    "LexiconRecord",
    "TextHit",
    "ThresholdConfig",
]
