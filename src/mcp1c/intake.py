"""Приём выгрузки конфигурации в файлы: что берём из архива и куда кладём.

Форматов выгрузки два, и раскладка у них разная (разведка, раздел 6).
Определяем по содержимому, а не по имени архива: имя даёт человек.
"""
from __future__ import annotations

import shutil
import zipfile
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


# Индекс пишется после отбора и в сумму отобранного не входит. По разведке на
# корпусе Розницы это 18,1 МБ сигнатур и 5,8 МБ форм — берём с запасом.
INDEX_RESERVE = 25 * 1024 * 1024


def planned_size(архив: Path) -> tuple[int, str]:
    """Сколько места займёт отобранное, плюс запас под индекс.

    Размеры берутся из центрального каталога: тело архива не читается вовсе.
    Соврать в опасную сторону это не может — `zipfile` обрезает вывод по
    объявленному размеру, а несовпадение ловит CRC.

    Цифра точная для обоих форматов. Постановка допускала для плоской выгрузки
    «оценку сверху» на том основании, что двоичную запись `form` внутри
    контейнера `.Form` мы не сохраняем, — но `is_wanted` берёт контейнер
    целиком, вместе с ней: разбирать контейнер здесь означало бы тащить в
    приём `v8container`. Что взвешено, то и ляжет на диск.
    """
    with zipfile.ZipFile(архив) as zf:
        записи = [i for i in zf.infolist() if not i.is_dir()]
        формат = detect_format([i.filename for i in записи])
        нужно = sum(i.file_size for i in записи if is_wanted(i.filename, формат))
    return нужно + INDEX_RESERVE, формат


def enough_space(нужно: int, каталог: Path) -> tuple[bool, int]:
    свободно = shutil.disk_usage(каталог).free
    return свободно >= нужно, свободно


# Версия правила отбора. Поднимается, когда меняется то, ЧТО мы достаём из
# архива, — тогда и только тогда нужен переразбор zip. Правки, меняющие лишь
# индекс, сервер переживает сам, пересобрав его из `data/modules/`.
SELECTION_VERSION = 1


def extract(архив: Path, корень: Path) -> tuple[int, int]:
    """Достать из архива модули и формы. Возвращает (файлов, байт).

    Читается членом за членом: развёрнутого архива на диске не возникает.
    Счётчики отражают количество файлов и байт, реально лежащих на диске:
    при дублирующихся имёнах в архиве последняя запись побеждает, и считается
    только фактическое содержимое на диске.
    """
    содержимое_целей = {}  # цель → размер последней записи
    корень.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(архив) as zf:
        записи = [i for i in zf.infolist() if not i.is_dir()]
        формат = detect_format([i.filename for i in записи])
        for info in записи:
            if not is_wanted(info.filename, формат):
                continue
            цель = safe_target(info.filename, корень)
            if цель is None:
                continue
            цель.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as входящий, цель.open("wb") as исходящий:
                shutil.copyfileobj(входящий, исходящий, length=1 << 20)
            # При дублирующихся имёнах запоминаем размер последней записи
            содержимое_целей[цель] = info.file_size
    файлов = len(содержимое_целей)
    байт = sum(содержимое_целей.values())
    return файлов, байт
