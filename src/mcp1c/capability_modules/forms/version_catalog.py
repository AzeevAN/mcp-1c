"""Версионные профили управляемых форм, выведенные из справки платформы."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


PlatformSupport = Literal["compiler", "documentation_only"]
PlatformConfidence = Literal[
    "confirmed", "inferred", "unverified", "unspecified"
]


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


@dataclass(frozen=True, slots=True)
class PlatformResolution:
    profile: PlatformProfile
    confidence: PlatformConfidence


@dataclass(frozen=True, slots=True)
class PlatformCompatibilityNote:
    code: str
    message: str


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


def _documented_version(profile: PlatformProfile, version: str) -> bool:
    if version in profile.documented_versions:
        return True
    if version.count(".") != 2:
        return False
    normalized = normalized_platform_version(version)
    return any(
        normalized_platform_version(documented) == normalized
        for documented in profile.documented_versions
    )


def platform_resolution(version: str | None) -> PlatformResolution | None:
    """Выбрать профиль и отдельно вернуть степень доказанности выбора."""

    if version is None:
        return PlatformResolution(DEFAULT_PLATFORM_PROFILE, "unspecified")
    normalized = normalized_platform_version(version)
    if normalized is None:
        return None
    direct = next(
        (profile for profile in PLATFORM_PROFILES if profile.contains(normalized)),
        None,
    )
    if direct is not None:
        if direct.support != "compiler":
            return PlatformResolution(direct, "unverified")
        confidence: PlatformConfidence = (
            "confirmed" if _documented_version(direct, version) else "inferred"
        )
        return PlatformResolution(direct, confidence)

    earlier = [
        profile for profile in PLATFORM_PROFILES if profile.minimum <= normalized
    ]
    fallback = (
        max(earlier, key=lambda profile: profile.minimum)
        if earlier
        else min(PLATFORM_PROFILES, key=lambda profile: profile.minimum)
    )
    return PlatformResolution(fallback, "unverified")


def platform_profile(version: str | None) -> PlatformProfile | None:
    """Вернуть выбранный профиль; доказательность доступна отдельно."""

    resolution = platform_resolution(version)
    return resolution.profile if resolution is not None else None


def platform_compatibility_note(
    version: str | None,
) -> PlatformCompatibilityNote | None:
    """Вернуть предупреждение, когда профиль выбран не по прямому доказательству."""

    resolution = platform_resolution(version)
    if resolution is None or resolution.confidence == "confirmed":
        return None
    if resolution.confidence == "unspecified":
        return PlatformCompatibilityNote(
            "platform_version_unspecified",
            (
                "Целевая версия платформы не указана; использован нейтральный "
                "профиль совместимости без гарантии импорта."
            ),
        )
    if resolution.confidence == "inferred":
        return PlatformCompatibilityNote(
            "platform_compatibility_inferred",
            (
                "Совместимость версии выведена из подтверждённых границ "
                "интервала, но отдельно на этой версии не проверялась."
            ),
        )
    return PlatformCompatibilityNote(
        "platform_compatibility_unverified",
        (
            "Для целевой версии нет прямого доказательства; использован "
            "ближайший известный профиль без гарантии импорта."
        ),
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
            "evidence": {
                "documented": list(profile.documented_versions),
                "interval": (
                    "exact_only"
                    if profile.minimum == profile.maximum
                    else "inferred_between_confirmed_versions"
                ),
            },
        }
        for profile in PLATFORM_PROFILES
    ]


__all__ = [
    "DEFAULT_PLATFORM_PROFILE",
    "DOCUMENTED_8_3_5_PROFILE",
    "PLATFORM_PROFILES",
    "PlatformProfile",
    "PlatformResolution",
    "PlatformConfidence",
    "PlatformCompatibilityNote",
    "normalized_platform_version",
    "platform_compatibility_note",
    "platform_profile",
    "platform_resolution",
    "platform_profiles_payload",
]
