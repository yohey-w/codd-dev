"""Language & adapter registries (design §0, Phase 1).

* :class:`LanguageRegistry` discovers and loads
  ``codd/languages/profiles/*.yaml`` and resolves a language string to a
  :class:`~codd.languages.profile.LanguageProfile`, matching by ``id`` OR
  any ``alias`` (case-insensitive).
* :class:`AdapterRegistry` is a minimal register/require stub keyed by
  ``(kind, id)`` — a placeholder for the pluggable adapters that later
  phases will populate. No real adapters exist yet.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .loader import LanguageProfileError, load_language_profile
from .profile import LanguageProfile

#: Directory holding the bundled profile YAMLs.
PROFILES_DIR = Path(__file__).with_name("profiles")


class UnknownLanguageError(KeyError):
    """Raised when a language cannot be resolved by id or alias."""

    def __init__(self, language: str, known: Iterable[str]) -> None:
        self.language = language
        self.known = sorted(set(known))
        super().__init__(
            f"unknown language {language!r}; "
            f"known languages/aliases: {', '.join(self.known) or '(none)'}"
        )


class LanguageRegistry:
    """Discovers and resolves language profiles.

    Profiles are loaded lazily on first use, then cached. Resolution matches
    a language string against each profile's ``id`` and ``aliases``,
    case-insensitively.
    """

    def __init__(self, profiles_dir: Path | str | None = None) -> None:
        self._profiles_dir = Path(profiles_dir) if profiles_dir else PROFILES_DIR
        self._profiles: dict[str, LanguageProfile] | None = None

    # -- discovery ---------------------------------------------------------

    def _ensure_loaded(self) -> dict[str, LanguageProfile]:
        if self._profiles is None:
            self._profiles = self._discover()
        return self._profiles

    def _discover(self) -> dict[str, LanguageProfile]:
        profiles: dict[str, LanguageProfile] = {}
        if not self._profiles_dir.is_dir():
            return profiles
        for yaml_path in sorted(self._profiles_dir.glob("*.yaml")):
            profile = load_language_profile(yaml_path)
            key = profile.identity.id.lower()
            if key in profiles:
                raise LanguageProfileError(
                    f"duplicate language id {profile.identity.id!r} "
                    f"(from {yaml_path.name})"
                )
            profiles[key] = profile
        return profiles

    def reload(self) -> None:
        """Force a re-scan of the profiles directory (drops the cache)."""
        self._profiles = None

    # -- access ------------------------------------------------------------

    def all_profiles(self) -> tuple[LanguageProfile, ...]:
        return tuple(self._ensure_loaded().values())

    def ids(self) -> tuple[str, ...]:
        return tuple(p.identity.id for p in self._ensure_loaded().values())

    def _known_names(self) -> list[str]:
        names: list[str] = []
        for profile in self._ensure_loaded().values():
            names.append(profile.identity.id)
            names.extend(profile.identity.aliases)
        return names

    def resolve(self, language: str) -> LanguageProfile:
        """Return the profile whose id or alias matches *language*.

        Matching is case-insensitive. Raises :class:`UnknownLanguageError`
        if nothing matches.
        """
        if language is None:
            raise UnknownLanguageError("None", self._known_names())
        needle = str(language).strip().lower()
        profiles = self._ensure_loaded()

        # Fast path: direct id hit.
        direct = profiles.get(needle)
        if direct is not None:
            return direct

        # Alias / case-insensitive scan.
        for profile in profiles.values():
            if profile.matches(needle):
                return profile

        raise UnknownLanguageError(str(language), self._known_names())


class AdapterRegistry:
    """Minimal (kind, id) -> adapter registry (stub for later phases).

    Phase 1 ships no real adapters; this exists so the wiring shape is
    settled. ``require`` raises if an adapter is missing — strict languages
    will use that in a later phase to forbid green-on-missing-adapter.
    """

    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], Any] = {}

    def register(self, kind: str, id: str, adapter: Any) -> None:
        self._adapters[(kind, id)] = adapter

    def get(self, kind: str, id: str) -> Any | None:
        return self._adapters.get((kind, id))

    def require(self, kind: str, id: str) -> Any:
        try:
            return self._adapters[(kind, id)]
        except KeyError as exc:
            raise KeyError(
                f"no adapter registered for kind={kind!r} id={id!r}"
            ) from exc

    def __contains__(self, key: object) -> bool:
        return key in self._adapters


#: Process-wide default language registry (lazy).
default_registry = LanguageRegistry()
