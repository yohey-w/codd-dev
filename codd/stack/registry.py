"""Framework & addon registries — discover and resolve stack-layer profiles.

Mirrors :class:`codd.languages.registry.LanguageRegistry`. A generic
``_LayerRegistry`` (parametrized by a loader fn + a profiles directory) backs
both :class:`FrameworkRegistry` (``codd/stack/profiles/frameworks/*.yaml``) and
:class:`AddonRegistry` (``codd/stack/profiles/addons/*.yaml``). Resolution
matches a name against each profile's ``id`` and ``aliases``, case-insensitively.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Generic, Iterable, TypeVar

from .loader import StackProfileError, load_addon_profile, load_framework_profile
from .profile import AddonProfile, FrameworkProfile

PROFILES_DIR = Path(__file__).with_name("profiles")
FRAMEWORKS_DIR = PROFILES_DIR / "frameworks"
ADDONS_DIR = PROFILES_DIR / "addons"

_P = TypeVar("_P", FrameworkProfile, AddonProfile)


class UnknownLayerError(KeyError):
    """Raised when a framework/addon cannot be resolved by id or alias."""

    def __init__(self, kind: str, name: str, known: Iterable[str]) -> None:
        self.kind = kind
        self.name = name
        self.known = sorted(set(known))
        super().__init__(
            f"unknown {kind} {name!r}; known {kind}s/aliases: "
            f"{', '.join(self.known) or '(none)'}"
        )


class _LayerRegistry(Generic[_P]):
    """Discovers and resolves a directory of layer profiles (lazy, cached)."""

    def __init__(self, kind: str, profiles_dir: Path, loader: Callable[[Path], _P]) -> None:
        self._kind = kind
        self._profiles_dir = Path(profiles_dir)
        self._loader = loader
        self._profiles: dict[str, _P] | None = None

    def _ensure_loaded(self) -> dict[str, _P]:
        if self._profiles is None:
            self._profiles = self._discover()
        return self._profiles

    def _discover(self) -> dict[str, _P]:
        profiles: dict[str, _P] = {}
        if not self._profiles_dir.is_dir():
            return profiles
        for yaml_path in sorted(self._profiles_dir.glob("*.yaml")):
            profile = self._loader(yaml_path)
            key = profile.identity.id.lower()
            if key in profiles:
                raise StackProfileError(
                    f"duplicate {self._kind} id {profile.identity.id!r} (from {yaml_path.name})"
                )
            profiles[key] = profile
        return profiles

    def reload(self) -> None:
        self._profiles = None

    def all_profiles(self) -> tuple[_P, ...]:
        return tuple(self._ensure_loaded().values())

    def ids(self) -> tuple[str, ...]:
        return tuple(p.identity.id for p in self._ensure_loaded().values())

    def _known_names(self) -> list[str]:
        names: list[str] = []
        for profile in self._ensure_loaded().values():
            names.append(profile.identity.id)
            names.extend(profile.identity.aliases)
        return names

    def resolve(self, name: str) -> _P:
        if name is None:
            raise UnknownLayerError(self._kind, "None", self._known_names())
        needle = str(name).strip().lower()
        profiles = self._ensure_loaded()
        direct = profiles.get(needle)
        if direct is not None:
            return direct
        for profile in profiles.values():
            if profile.matches(needle):
                return profile
        raise UnknownLayerError(self._kind, str(name), self._known_names())


class FrameworkRegistry(_LayerRegistry[FrameworkProfile]):
    def __init__(self, profiles_dir: Path | str | None = None) -> None:
        super().__init__(
            "framework", Path(profiles_dir) if profiles_dir else FRAMEWORKS_DIR, load_framework_profile
        )


class AddonRegistry(_LayerRegistry[AddonProfile]):
    def __init__(self, profiles_dir: Path | str | None = None) -> None:
        super().__init__(
            "addon", Path(profiles_dir) if profiles_dir else ADDONS_DIR, load_addon_profile
        )


#: Process-wide default registries (lazy).
default_framework_registry = FrameworkRegistry()
default_addon_registry = AddonRegistry()
