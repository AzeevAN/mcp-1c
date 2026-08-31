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
_TREE_FORM_FOLDERS = {
    "AccountingRegisters", "AccumulationRegisters", "BusinessProcesses",
    "CalculationRegisters", "Catalogs", "ChartsOfAccounts",
    "ChartsOfCalculationTypes", "ChartsOfCharacteristicTypes", "CommonForms",
    "DataProcessors", "DocumentJournals", "Documents", "Enums",
    "ExchangePlans", "FilterCriteria", "InformationRegisters", "Reports",
    "SettingsStorages", "Tasks", "ExternalDataSources",
}
# Плоская: модуль в `.txt`, код формы — записью внутри контейнера `.Form`.
_FLAT_SUFFIXES = {".txt", ".Form"}


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
    if части[0] == "ExternalDataSources":
        # Таблица источника: Forms лежит под Tables/<Таблица>/, а не сразу
        # под именем объекта. Обычное правило len==4 / len==6 сюда не подходит.
        descriptor = (
            len(части) == 6
            and части[2] == "Tables"
            and части[4] == "Forms"
            and путь.suffix == ".xml"
            and bool(путь.stem)
        )
        body = (
            len(части) == 8
            and части[2] == "Tables"
            and части[4] == "Forms"
            and части[6] == "Ext"
            and части[7] in {"Form.xml", "Form.bin"}
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
    путь, который `safe_target` затем тихо отвергал, хотя `planned_size` его
    уже посчитал. `PurePosixPath` двойной слэш схлопывает сам.

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
INDEX_RESERVE_MIN = 25 * 1024 * 1024
# Совместимое публичное имя нижнего порога: старые проверки импортируют его
# напрямую. Фактический резерв теперь может быть больше этой константы.
INDEX_RESERVE = INDEX_RESERVE_MIN
INDEX_RESERVE_PERCENT = 15


def карта_архива(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Единая карта пригодных целей; нормализованный дубль заменяет прежний.

    Обёртка, манифест, определение формата, предпроверка, план и распаковка
    обязаны смотреть именно на эту карту. Иначе двойной слэш или ``./`` в
    имени способен пройти один этап и потеряться на другом.
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

    Единый расчёт используют предпроверка, план места и распаковка. Поэтому
    дубль имени и опасный путь не могут занимать байты только в одном из трёх
    представлений.
    """
    карта = карта if карта is not None else карта_архива(zf)
    формат = detect_format(list(карта))
    результат: dict[str, zipfile.ZipInfo] = {}
    for имя, info in карта.items():
        if not is_wanted(имя, формат):
            continue
        результат[имя] = info
    return результат, формат


def _резерв(отобрано: int) -> int:
    доля = (отобрано * INDEX_RESERVE_PERCENT + 99) // 100
    return max(доля, INDEX_RESERVE_MIN)


def planned_size(архив: Path, *, existing: bool = False) -> tuple[int, str]:
    """Сколько места займёт отобранное, плюс запас под индекс.

    Размеры берутся из центрального каталога: тело архива не читается вовсе.
    Соврать в опасную сторону это не может — `zipfile` обрезает вывод по
    объявленному размеру, а несовпадение ловит CRC.

    Цифра точная для обоих форматов. Постановка допускала для плоской выгрузки
    «оценку сверху» на том основании, что двоичную запись `form` внутри
    контейнера `.Form` мы не сохраняем, — но `is_wanted` берёт контейнер
    целиком, вместе с ней: разбирать контейнер здесь означало бы тащить в
    приём `v8container`. Что взвешено, то и ляжет на диск.

    Обёртка архива (снятая в `карта_архива`) на сумму не влияет: имя
    без неё то же самое по суффиксу и по последнему компоненту, а значит и
    `is_wanted` решает одинаково что с обёрткой, что без — обёртка меняет
    только КУДА член ляжет (`extract`), а не ЧТО отобрано. Снимаем её здесь
    ровно затем, чтобы совпадение считалось честно, а не по совпадению.

    ``existing`` сохраняет явный контракт вызывающего о наличии канонического
    корня, но не меняет арифметику: ``disk_usage.free`` уже исключает байты
    старого корня. Дополнительно требуется ровно новая выбранная копия плюс
    резерв индекса.
    """
    del existing
    with zipfile.ZipFile(архив) as zf:
        записи, формат = _отобранные_записи(zf)
        отобрано = sum(info.file_size for info in записи.values())
    return отобрано + _резерв(отобрано), формат


def enough_space(нужно: int, каталог: Path) -> tuple[bool, int]:
    свободно = shutil.disk_usage(каталог).free
    return свободно >= нужно, свободно


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
# 6: иерархический отбор берёт формы `SettingsStorages`,
# `ChartsOfCalculationTypes`, `AccountingRegisters`, `CalculationRegisters`
# и таблиц `ExternalDataSources` (дескриптор, `Form.xml`, `Form.bin`).
# На «Автосалон6» без хранилищ 19 форм оставались без структуры; XML таблиц
# внешнего источника лежит на два уровня глубже обычного объекта
# (`…/Tables/<Таблица>/Forms/…`) и прежним правилом отбрасывался, хотя
# Module.bsl уже попадал как `.bsl`.
SELECTION_VERSION = 6


def extract(архив: Path, корень: Path) -> tuple[int, int]:
    """Достать из архива модули и формы. Возвращает (файлов, байт).

    Читается членом за членом: развёрнутого архива на диске не возникает.
    Счётчики отражают количество файлов и байт, реально лежащих на диске:
    при дублирующихся имёнах в архиве последняя запись побеждает, и считается
    только фактическое содержимое на диске.

    Обёртка архива (единственный каталог верхнего уровня — см.
    единой картой в `карта_архива`) в целевом пути не воспроизводится: член
    `Обёртка/Catalogs/…` ложится как `<корень>/Catalogs/…`, а не
    `<корень>/Обёртка/Catalogs/…`. Санитизация (`safe_target`) применяется
    ПОСЛЕ снятия обёртки, а не вместо неё — снимается только обёртка из
    имени, правило безопасности остаётся прежним.
    """
    корень.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(архив) as zf:
        записи, _формат = _отобранные_записи(zf)
        for имя, info in записи.items():
            цель = safe_target(имя, корень)
            assert цель is not None  # `_отобранные_записи` уже проверила путь.
            цель.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as входящий, цель.open("wb") as исходящий:
                shutil.copyfileobj(входящий, исходящий, length=1 << 20)
    файлов = len(записи)
    байт = sum(info.file_size for info in записи.values())
    return файлов, байт
