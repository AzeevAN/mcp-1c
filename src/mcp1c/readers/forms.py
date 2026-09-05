"""Диспетчер ридеров записи ``form`` из контейнера формы."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .list_stream import ListStreamError, scan_list_stream
from .ordinary_forms import OrdinaryFormReader


KNOWN_MARKERS = frozenset({19, 20, 23, 25, 26, 27})


class FormReadError(ValueError):
    def __init__(self, category: str, reason: str):
        self.category = category
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class FormReadResult:
    marker: int
    status: str
    category: str
    reason: str
    semantic_fields: Mapping[str, object]
    tokens: int
    max_depth: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "semantic_fields", MappingProxyType(dict(self.semantic_fields))
        )


_READERS = (OrdinaryFormReader(),)


def read_form(payload: bytes) -> FormReadResult:
    try:
        scan = scan_list_stream(payload)
        for reader in _READERS:
            if not reader.applicable(scan.marker):
                continue
            try:
                semantic = reader.read(scan)
            except ListStreamError as error:
                if error.category == "unsupported_profile":
                    return FormReadResult(
                        scan.marker,
                        "deferred",
                        "known_marker_semantics_deferred",
                        error.reason,
                        {},
                        scan.tokens,
                        scan.max_depth,
                    )
                raise
            return FormReadResult(
                scan.marker,
                "deferred",
                "event_semantics_deferred",
                "реквизиты и элементы прочитаны; точные привязки событий отложены",
                {
                    "attributes": semantic.attributes,
                    "elements": semantic.elements,
                    "control_types": semantic.control_types,
                },
                scan.tokens,
                scan.max_depth,
            )
    except ListStreamError as error:
        raise FormReadError(error.category, error.reason) from error

    category = (
        "known_marker_semantics_deferred"
        if scan.marker in KNOWN_MARKERS
        else "unknown_marker"
    )
    reason = (
        "маркер известен, семантический ридер ещё не реализован"
        if scan.marker in KNOWN_MARKERS
        else "маркер form не поддержан"
    )
    return FormReadResult(
        scan.marker,
        "deferred",
        category,
        reason,
        {},
        scan.tokens,
        scan.max_depth,
    )


__all__ = ["FormReadError", "FormReadResult", "KNOWN_MARKERS", "read_form"]
