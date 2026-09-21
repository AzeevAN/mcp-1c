"""Публичная классификация ссылок на DefinedType без ложного раскрытия типа."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class DefinedTypeState(str, Enum):
    EXACT = "exact"
    GENERIC = "generic"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DefinedTypeResolution:
    state: DefinedTypeState
    reference: str
    members: tuple[str, ...] = ()
    reason: str = ""


def resolve_defined_type(
    reference: str,
    defined_types: Mapping[str, tuple[str, ...]],
) -> DefinedTypeResolution:
    """Вернуть exact только при наличии отдельной карточки и её members."""
    if not isinstance(reference, str) or not reference:
        return DefinedTypeResolution(
            DefinedTypeState.UNKNOWN, str(reference), reason="empty_reference"
        )
    if reference.startswith("ОпределяемыйТип."):
        name = reference.removeprefix("ОпределяемыйТип.")
        members = defined_types.get(name)
        if members is not None:
            return DefinedTypeResolution(
                DefinedTypeState.EXACT, reference, tuple(members)
            )
        return DefinedTypeResolution(
            DefinedTypeState.UNKNOWN, reference, reason="missing_target"
        )
    prefix = "cfg:DefinedTypeRef."
    if reference.startswith(prefix):
        name = reference[len(prefix) :]
        members = defined_types.get(name)
        if members is not None:
            return DefinedTypeResolution(
                DefinedTypeState.EXACT, reference, tuple(members)
            )
        return DefinedTypeResolution(
            DefinedTypeState.UNKNOWN, reference, reason="missing_target"
        )
    if reference.startswith("cfg:"):
        return DefinedTypeResolution(
            DefinedTypeState.GENERIC, reference, reason="generic_reference"
        )
    return DefinedTypeResolution(
        DefinedTypeState.UNKNOWN, reference, reason="unsupported_reference"
    )


__all__ = ["DefinedTypeResolution", "DefinedTypeState", "resolve_defined_type"]
