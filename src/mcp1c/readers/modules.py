"""Ридеры содержимого модулей без привязки к физическому контейнеру."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModuleBodyResult:
    """Результат чтения уже извлечённого тела модуля."""

    state: str
    text: str | None
    reason: str = ""


class _Utf8BslReader:
    """Текстовый BSL из XML/flat-выгрузки или записи ``module`` формы."""

    def read(self, payload: bytes) -> ModuleBodyResult:
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            return ModuleBodyResult(
                "unreadable", None, "тело модуля не является UTF-8"
            )
        if not text.strip():
            return ModuleBodyResult("empty", text)
        return ModuleBodyResult("ready", text)


_READERS = (_Utf8BslReader(),)


def read_module_body(payload: bytes) -> ModuleBodyResult:
    """Прочитать тело первой применимой реализацией.

    После распознавания формата результат конечный: запасной ридер не должен
    скрывать повреждение другим толкованием тех же байтов.
    """
    if not isinstance(payload, bytes):
        raise TypeError("Тело модуля должно быть bytes.")
    for reader in _READERS:
        return reader.read(payload)
    raise AssertionError("цепочка ридеров модулей пуста")


__all__ = ["ModuleBodyResult", "read_module_body"]
