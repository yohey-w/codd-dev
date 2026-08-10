"""Validation and ordering for declarative profile revisions.

Profile versions are deliberately small and boring: bundled language,
framework, and addon profiles use stable ``MAJOR.MINOR.PATCH`` revisions.  A
stack-lock update may move only to a strictly newer revision; arbitrary strings
would make that ordering ambiguous and turn a downgrade into a possible green.
"""

from __future__ import annotations

import re


_STABLE_PROFILE_VERSION = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


def profile_version_key(value: object, *, where: str = "profile_version") -> tuple[int, int, int]:
    """Return the ordered key for a stable profile version.

    Profiles are contracts rather than package-manager ranges, so pre-release,
    build-metadata, and abbreviated forms are rejected.  Keeping one canonical
    form makes lock diffs and strict-upgrade checks deterministic.
    """
    text = str(value).strip()
    match = _STABLE_PROFILE_VERSION.fullmatch(text)
    if match is None:
        raise ValueError(
            f"{where} must be a stable MAJOR.MINOR.PATCH version, got {value!r}"
        )
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def validated_profile_version(value: object, *, where: str = "profile_version") -> str:
    """Validate *value* and return its canonical string representation."""
    key = profile_version_key(value, where=where)
    return ".".join(str(part) for part in key)


def is_strict_profile_upgrade(old: object, new: object) -> bool:
    """Whether *new* is a strictly newer stable profile revision than *old*."""
    return profile_version_key(new, where="new profile_version") > profile_version_key(
        old, where="locked profile_version"
    )
