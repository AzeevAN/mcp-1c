"""Лёгкая история исходников без сохранения parsed payload или generation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class SourceHistoryStatus(str, Enum):
    ACTIVE = "active"
    MISSING = "missing"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class SourceHistoryEntry:
    identity: str
    parent: str
    locator: str
    origin: str
    transport: str
    raw_sha256: str
    status: SourceHistoryStatus = SourceHistoryStatus.ACTIVE

    def __post_init__(self) -> None:
        for name in ("identity", "parent", "locator", "origin", "transport", "raw_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} должен быть непустой строкой")

    def to_dict(self) -> dict[str, str]:
        return {name: getattr(self, name).value if isinstance(getattr(self, name), Enum) else getattr(self, name)
                for name in ("identity", "parent", "locator", "origin", "transport", "raw_sha256", "status")}


def reconnect_candidates(
    history: Iterable[SourceHistoryEntry],
    candidates: Iterable[tuple[str, str, str, str]],
) -> tuple[SourceHistoryEntry, ...]:
    """Найти только exact или same identity+parent обновлённые источники.

    Candidate tuple: ``identity, parent, locator, raw_sha256``. Display name
    не участвует в сопоставлении; проверка содержимого остаётся за probe.
    """
    available = tuple(candidates)
    result: list[SourceHistoryEntry] = []
    for item in history:
        matches = [
            candidate
            for candidate in available
            if candidate[0] == item.identity and candidate[1] == item.parent
        ]
        if not matches:
            result.append(SourceHistoryEntry(**{**item.to_dict(), "status": SourceHistoryStatus.MISSING}))
            continue
        exact = next((candidate for candidate in matches if candidate[3] == item.raw_sha256), matches[0])
        result.append(SourceHistoryEntry(
            identity=item.identity, parent=item.parent, locator=exact[2],
            origin=item.origin, transport=item.transport, raw_sha256=exact[3],
            status=SourceHistoryStatus.ACTIVE if exact[3] == item.raw_sha256 else SourceHistoryStatus.INCOMPATIBLE,
        ))
    return tuple(result)


__all__ = ["SourceHistoryEntry", "SourceHistoryStatus", "reconnect_candidates"]
