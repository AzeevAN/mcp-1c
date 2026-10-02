"""Отбор кода из ZIP выгрузки конфигурации в файлы.

Распаковки в каталог кода здесь больше нет: приём идёт через native intake.
Отбор остался для bootstrap (похож ли ZIP на выгрузку в файлы) и для
каталога модулей (`is_intentionally_ignored`).

Форматов выгрузки два, и раскладка у них разная (разведка, раздел 6).
Определяем по содержимому, а не по имени архива: имя даёт человек.
"""
from __future__ import annotations

import zipfile
from pathlib import PurePosixPath

FORMAT_TREE = "tree"
FORMAT_FLAT = "flat"

# Иерархическая: модуль — отдельный файл, форма — разбираемый XML.
_TREE_SUFFIXES = {".bsl"}
_TREE_FORM_FOLDERS = {
    "AccumulationRegisters", "BusinessProcesses", "Catalogs",
    "ChartsOfAccounts", "ChartsOfCharacteristicTypes", "CommonForms",
    "DataProcessors", "DocumentJournals", "Documents", "Enums",
    "ExchangePlans", "FilterCriteria", "InformationRegisters", "Reports",
    "Tasks",
}
# Плоская: модуль в `.txt`, код формы — записью внутри контейнера `.Form`.
_FLAT_SUFFIXES = {".txt", ".Form"}

# Хранилища настроек не являются поддержанным объектом полного разбора.
# Брать только их BSL означало бы публиковать код без дескриптора и структуры
# форм. Поэтому отбрасываем весь вид до coverage.
_IGNORED_TREE_FOLDERS = {"SettingsStorages"}
_IGNORED_FLAT_PREFIXES = ("SettingsStorage.",)


def is_intentionally_ignored(name: str) -> bool:
    """Известный вид вне текущего продуктового coverage."""
    путь = PurePosixPath(name)
    return (
        bool(путь.parts and путь.parts[0] in _IGNORED_TREE_FOLDERS)
        or (
            len(путь.parts) == 1
            and путь.name.startswith(_IGNORED_FLAT_PREFIXES)
        )
    )


def _скомпилированный_общий_модуль(name: str) -> bool:
    """Канонические имена скомпилированного общего модуля.

    Платформа пишет две раскладки: ``CommonModules/<Имя>.Module`` и плоскую
    ``CommonModule.<Имя>.Module``. Регистр и положение значимы; каталог-
    обёртка к моменту этой проверки уже снят единой картой архива.
    """
    части = PurePosixPath(name).parts
    имя = части[-1] if части else ""
    плоские_части = имя.split(".")
    плоское = (
        len(плоские_части) == 3
        and плоские_части[0] == "CommonModule"
        and bool(плоские_части[1])
        and плоские_части[2] == "Module"
        and len(части) == 1
    )
    каталожное = (
        len(части) >= 2
        and части[-2] == "CommonModules"
        and PurePosixPath(имя).suffix == ".Module"
        and имя.count(".") == 1
        and len(части) == 2
    )
    return плоское or каталожное


def detect_format(names: list[str]) -> str:
    """Контейнер ``.Form`` и скомпилированный общий модуль — плоские."""
    есть_txt = False
    есть_иерархический = False
    for имя in names:
        if имя.endswith(".Form") or _скомпилированный_общий_модуль(имя):
            return FORMAT_FLAT
        путь = PurePosixPath(имя)
        есть_txt = есть_txt or путь.suffix == ".txt"
        есть_иерархический = (
            есть_иерархический
            or путь.suffix == ".bsl"
            or путь.name == "Form.xml"
        )
    if есть_txt and not есть_иерархический:
        return FORMAT_FLAT
    return FORMAT_TREE


def is_wanted(name: str, формат: str) -> bool:
    путь = PurePosixPath(name)
    # Мусор архиватора Finder на macOS («Сжать объекты»): `__MACOSX/` несёт
    # копии ресурсных вилок каждого файла под именем `._Имя` — с тем же
    # суффиксом, что у оригинала. Без исключения `__MACOSX/.../._ObjectModule.bsl`
    # проходит отбор как настоящий `.bsl`, а `._Ext.txt` — как настоящий
    # плоский модуль: рядом с кодом на диске ложится двоичный
    # AppleDouble-файл (регрессия проявлялась как `items_total=2` на архиве с одним
    # настоящим модулем).
    if путь.parts and путь.parts[0] == "__MACOSX":
        return False
    if путь.name.startswith("._"):
        return False
    if is_intentionally_ignored(name):
        return False
    if формат == FORMAT_FLAT:
        if путь.name.endswith(".Template.txt"):
            return False
        return (
            путь.suffix in _FLAT_SUFFIXES
            or _скомпилированный_общий_модуль(name)
        )
    if путь.suffix in _TREE_SUFFIXES:
        return True
    части = путь.parts
    if not части or части[0] not in _TREE_FORM_FOLDERS:
        return False
    if части[0] == "CommonForms":
        descriptor = len(части) == 2 and путь.suffix == ".xml" and bool(путь.stem)
        body = (
            len(части) == 4
            and части[2] == "Ext"
            and части[3] in {"Form.xml", "Form.bin"}
        )
        return descriptor or body
    descriptor = (
        len(части) == 4
        and части[2] == "Forms"
        and путь.suffix == ".xml"
        and bool(путь.stem)
    )
    body = (
        len(части) == 6
        and части[2] == "Forms"
        and части[4] == "Ext"
        and части[5] in {"Form.xml", "Form.bin"}
    )
    return descriptor or body


def _безопасное_относительное_имя(name: str) -> str | None:
    """Нормализованное имя цели без обращения к файловой системе."""
    путь = PurePosixPath(name)
    if путь.is_absolute() or ".." in путь.parts or not путь.parts:
        return None
    return PurePosixPath(*путь.parts).as_posix()


def _базовые_записи(
    zf: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, str]]:
    """Безопасные нормализованные файлы до снятия каталога-обёртки."""
    результат: list[tuple[zipfile.ZipInfo, str]] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        имя = _безопасное_относительное_имя(info.filename)
        if имя is None:
            continue
        путь = PurePosixPath(имя)
        if путь.parts and путь.parts[0] == "__MACOSX":
            continue
        if путь.name.startswith("._") or путь.name == ".DS_Store":
            continue
        результат.append((info, имя))
    return результат


def _обёртка_записей(записи: list[tuple[zipfile.ZipInfo, str]]) -> str | None:
    """Единственный верхний каталог пригодных файлов, если он общий."""
    каталоги: set[str] = set()
    for _info, имя in записи:
        части = PurePosixPath(имя).parts
        if len(части) == 1:
            return None
        каталоги.add(части[0])
    return next(iter(каталоги)) if len(каталоги) == 1 else None


def _без_обёртки(name: str, обёртка: str | None) -> str:
    """Путь члена архива без общей обёртки единой карты.

    Снимается покомпонентно, через `PurePosixPath.parts`, а не срезанием
    строкового префикса: у `"Обёртка//Catalogs/…"` (двойной слэш — не
    редкость при ручной сборке архива) строковый срез `"Обёртка/"` оставлял
    бы один слэш из двух и превращал остаток в `"/Catalogs/…"` — абсолютный
    путь, который отбор отверг бы как опасный. `PurePosixPath` двойной слэш
    схлопывает сам.

    Мусор Finder (`__MACOSX/`, `.DS_Store`, все `._*`) отбрасывается до
    вычисления обёртки, поэтому не может ни создать её, ни разрушить.
    """
    if обёртка is None:
        return name
    части = PurePosixPath(name).parts
    if части[:1] != (обёртка,):
        return name
    остаток = части[1:]
    return str(PurePosixPath(*остаток)) if остаток else name


def карта_архива(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Единая карта пригодных целей; нормализованный дубль заменяет прежний.

    Обёртка, манифест, определение формата и отбор кода обязаны смотреть
    именно на эту карту. Иначе двойной слэш или ``./`` в имени способен
    пройти один этап и потеряться на другом.
    """
    базовые = _базовые_записи(zf)
    обёртка = _обёртка_записей(базовые)
    результат: dict[str, zipfile.ZipInfo] = {}
    for info, сырое in базовые:
        имя = _без_обёртки(сырое, обёртка)
        путь = PurePosixPath(имя)
        if путь.parts and путь.parts[0] == "__MACOSX":
            continue
        if путь.name.startswith("._") or путь.name == ".DS_Store":
            continue
        результат[путь.as_posix()] = info
    return результат


def _отобранные_записи(
    zf: zipfile.ZipFile, *, карта: dict[str, zipfile.ZipInfo] | None = None,
) -> tuple[dict[str, zipfile.ZipInfo], str]:
    """Последняя безопасная запись на каждую реальную цель и формат.

    Сейчас по нему bootstrap решает, похож ли ZIP на выгрузку в файлы
    (`registry._похоже_на_выгрузку_в_файлы`): есть ли в архиве отбираемый
    код. Записи берутся из той же карты, что и формат, поэтому дубль имени не
    решает вопрос иначе, чем определение формата.
    """
    карта = карта if карта is not None else карта_архива(zf)
    формат = detect_format(list(карта))
    результат: dict[str, zipfile.ZipInfo] = {}
    for имя, info in карта.items():
        if not is_wanted(имя, формат):
            continue
        результат[имя] = info
    return результат, формат


# Версия правила отбора. Поднимается, когда меняется то, ЧТО мы достаём из
# архива, — тогда и только тогда нужен переразбор zip. Правки, меняющие лишь
# индекс, сервер переживает сам, пересобрав его из `data/modules/`.
#
# 2: обёртка архива (единственный каталог верхнего уровня — `zip -r архив.zip
# папка` или Finder) больше не воспроизводится в целевом пути. Меняется КУДА
# ложится уже отобранное — тот же файл, разобранный старым правилом, лежит на
# диске на один уровень глубже, чем разобранный новым.
#
# 3: добавлены канонические `CommonModules/<Имя>.Module`, исключены
# `*.Template.txt`; план и распаковка используют один безопасный набор.
#
# 4: иерархическая выгрузка сохраняет XML-дескриптор формы и `Form.bin`;
# без повторного разбора старый корень физически не содержит этих файлов.
#
# 5: рядом с выбранным кодом атомарно публикуется производный gzip-каталог
# происхождения структуры. Исходные XML объектов по-прежнему не сохраняются,
# но старый корень без нового каталога нельзя считать разобранным по текущему
# правилу: восстановить доказательство после удаления ZIP уже неоткуда.
#
# 6: SettingsStorages целиком исключены. Старый корень мог содержать их модули,
# поэтому без явного переразбора его нельзя выдавать за результат текущего
# отбора.
#
# 7: native collector сохраняет descriptor HTTP-сервисов в extended_structure.
# Сам legacy-корень кода не меняется, но общий маркер свежести обязан отправить
# прежний source B на явный переразбор: из его поколения новый слой восстановить
# уже неоткуда.
SELECTION_VERSION = 7
