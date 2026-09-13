"""Версионные профили управляемых форм, выведенные из справки платформы."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


PlatformSupport = Literal["compiler", "documentation_only"]


@dataclass(frozen=True, slots=True)
class PlatformProfile:
    name: str
    minimum: tuple[int, int, int]
    maximum: tuple[int, int, int]
    documented_versions: tuple[str, ...]
    event_profile: str
    formats: tuple[str, ...]
    support: PlatformSupport

    def contains(self, version: tuple[int, int, int]) -> bool:
        return self.minimum <= version <= self.maximum


DOCUMENTED_8_3_5_PROFILE = PlatformProfile(
    name="managed_form_8_3_5_documented",
    minimum=(8, 3, 5),
    maximum=(8, 3, 5),
    documented_versions=("8.3.5.1570",),
    event_profile="8.3.5",
    formats=(),
    support="documentation_only",
)

DEFAULT_PLATFORM_PROFILE = PlatformProfile(
    name="managed_form_2_16_modern",
    minimum=(8, 3, 23),
    maximum=(8, 3, 27),
    documented_versions=("8.3.23.1997", "8.3.26.15", "8.3.27.2130"),
    event_profile="modern",
    formats=("2.16",),
    support="compiler",
)

PLATFORM_PROFILES = (
    DOCUMENTED_8_3_5_PROFILE,
    DEFAULT_PLATFORM_PROFILE,
)

_PLATFORM_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$")


def normalized_platform_version(value: str) -> tuple[int, int, int] | None:
    match = _PLATFORM_VERSION.fullmatch(value)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def platform_profile(version: str | None) -> PlatformProfile | None:
    """Найти доказанный профиль; отсутствие версии сохраняет прежний контракт."""

    if version is None:
        return DEFAULT_PLATFORM_PROFILE
    normalized = normalized_platform_version(version)
    if normalized is None:
        return None
    return next(
        (profile for profile in PLATFORM_PROFILES if profile.contains(normalized)),
        None,
    )


def platform_profiles_payload() -> list[dict[str, object]]:
    """Вернуть публичную матрицу без локальных путей к исходной справке."""

    return [
        {
            "name": profile.name,
            "minimum": ".".join(str(part) for part in profile.minimum),
            "maximum": ".".join(str(part) for part in profile.maximum),
            "documented_versions": list(profile.documented_versions),
            "event_profile": profile.event_profile,
            "form_formats": list(profile.formats),
            "support": profile.support,
        }
        for profile in PLATFORM_PROFILES
    ]


__all__ = [
    "DEFAULT_PLATFORM_PROFILE",
    "DOCUMENTED_8_3_5_PROFILE",
    "PLATFORM_PROFILES",
    "PlatformProfile",
    "normalized_platform_version",
    "platform_profile",
    "platform_profiles_payload",
]
