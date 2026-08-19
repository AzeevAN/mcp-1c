"""Приём выгрузки конфигурации в файлы: что берём из архива и куда кладём.

Форматов выгрузки два, и раскладка у них разная (разведка, раздел 6).
Определяем по содержимому, а не по имени архива: имя даёт человек.
"""
from __future__ import annotations

from pathlib import Path, PurePosixPath

FORMAT_TREE = "tree"
FORMAT_FLAT = "flat"

# Иерархическая: модуль — отдельный файл, форма — разбираемый XML.
_TREE_SUFFIXES = {".bsl"}
_TREE_NAMES = {"Form.xml"}
# Плоская: модуль в `.txt`, код формы — записью внутри контейнера `.Form`.
_FLAT_SUFFIXES = {".txt", ".Form"}


def detect_format(names: list[str]) -> str:
    """Контейнер `.Form` встречается только в плоской выгрузке."""
    for имя in names:
        if имя.endswith(".Form"):
            return FORMAT_FLAT
    return FORMAT_TREE


def is_wanted(name: str, формат: str) -> bool:
    путь = PurePosixPath(name)
    if формат == FORMAT_FLAT:
        return путь.suffix in _FLAT_SUFFIXES
    return путь.suffix in _TREE_SUFFIXES or путь.name in _TREE_NAMES


def safe_target(name: str, корень: Path) -> Path | None:
    """Куда лечь члену архива. `None` — член отвергнут.

    `ZipFile.open` путь не чистит, в отличие от `extract`: имя внутри архива
    приходит от того, кто архив собрал, и может увести наружу корня.
    """
    путь = PurePosixPath(name)
    if путь.is_absolute() or ".." in путь.parts:
        return None
    цель = (корень / Path(*путь.parts)).resolve()
    корень = корень.resolve()
    if корень != цель and корень not in цель.parents:
        return None
    return цель
