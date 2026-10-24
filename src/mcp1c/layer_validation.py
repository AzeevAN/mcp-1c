"""Общие проверки канонических слоёв поколения."""

from __future__ import annotations

from typing import Mapping


class GenerationRuntimeError(ValueError):
    """Слои поколения нельзя безопасно представить действующему runtime."""


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise GenerationRuntimeError(f"{label} должен быть объектом")
    return value


def _exact_keys(
    value: Mapping[str, object], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        raise GenerationRuntimeError(f"{label} содержит неверный набор полей")


def _text(value: object, label: str, *, required: bool = False) -> str:
    if not isinstance(value, str) or (required and not value):
        raise GenerationRuntimeError(f"{label} должен быть строкой")
    return value


def _optional_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GenerationRuntimeError(
            f"{label} должен быть целым числом не меньше нуля или null"
        )
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise GenerationRuntimeError(f"{label} должен быть bool")
    return value


def _text_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise GenerationRuntimeError(f"{label} должен быть массивом строк")
    return list(value)
