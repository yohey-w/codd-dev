from __future__ import annotations

import subprocess


def test_no_project_specific_literals_in_changed_files() -> None:
    result = subprocess.run(
        [
            "grep",
            "-Ern",
            "osato|lms|caregiver|stripe|prisma",
            "codd/elicit/engine.py",
            "codd/elicit/finding.py",
            "codd/dag/checks/user_journey_coherence.py",
            "codd/dag/checks/ci_health.py",
            "codd/deployer.py",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
