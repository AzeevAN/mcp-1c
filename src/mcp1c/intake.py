"""Приём выгрузки конфигурации в файлы: что берём из архива и куда кладём.

Форматов выгрузки два, и раскладка у них разная (разведка, раздел 6).
Определяем по содержимому, а не по имени архива: имя даёт человек.

Источник — ZIP или уже распакованный каталог: карта имён, обёртка и отбор
одни и те же. Идентичность каталога — `ConfigDumpInfo.xml` или, если его нет,
`Configuration.xml`; файл `VERSION` выгрузки gitSync хешируется дополнительно.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import zipfile
from pathlib import Path, PurePosixPath

CONFIG_DUMP_INFO = "ConfigDumpInfo.xml"
CONFIGURATION_XML = "Configuration.xml"
GITSYNC_VERSION = "VERSION"

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


def _обёртка_записей(записи: list[tuple[object, str]]) -> str | None:
    """Единственный верхний каталог пригодных файлов, если он общий."""
    каталоги: set[str] = set()
    for _info, имя in записи:
        части = PurePosixPath(имя).parts
        if len(части) == 1:
            return None
        каталоги.add(части[0])
    return next(iter(каталоги)) if len(каталоги) == 1 else None


def _собрать_карту(базовые: list[tuple[object, str]]) -> dict[str, object]:
    """Общая нормализация: обёртка, мусор Finder, последняя запись побеждает."""
    обёртка = _обёртка_записей(базовые)
    результат: dict[str, object] = {}
    for payload, сырое in базовые:
        имя = _без_обёртки(сырое, обёртка)
        путь = PurePosixPath(имя)
        if путь.parts and путь.parts[0] == "__MACOSX":
            continue
        if путь.name.startswith("._") or путь.name == ".DS_Store":
            continue
        результат[путь.as_posix()] = payload
    return результат


def _обычный_файл(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _мусор_верхнего_уровня(name: str) -> bool:
    return name.startswith("._") or name in {".DS_Store", "__MACOSX"}


def _базовые_файлы_каталога(корень: Path) -> list[tuple[Path, str]]:
    """Обычные файлы каталога без симлинков, до снятия обёртки."""
    результат: list[tuple[Path, str]] = []
    try:
        корень = корень.resolve()
    except OSError:
        return результат
    for dirpath, dirnames, filenames in os.walk(корень, followlinks=False):
        живые: list[str] = []
        for имя in dirnames:
            полный = Path(dirpath) / имя
            if полный.is_symlink():
                continue
            живые.append(имя)
        dirnames[:] = живые
        for имя in filenames:
            полный = Path(dirpath) / имя
            if полный.is_symlink() or not полный.is_file():
                continue
            try:
                относительный = полный.relative_to(корень).as_posix()
            except ValueError:
                continue
            безопасное = _безопасное_относительное_имя(относительный)
            if безопасное is None:
                continue
            путь = PurePosixPath(безопасное)
            if путь.parts and путь.parts[0] == "__MACOSX":
                continue
            if путь.name.startswith("._") or путь.name == ".DS_Store":
                continue
            результат.append((полный, безопасное))
    return результат


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
    return _собрать_карту(_базовые_записи(zf))  # type: ignore[return-value]


def карта_каталога(корень: Path) -> dict[str, Path]:
    """Та же карта имён, что у ZIP, по уже распакованной выгрузке."""
    return _собрать_карту(_базовые_файлы_каталога(корень))  # type: ignore[return-value]


def _единственная_обёртка(корень: Path) -> Path | None:
    """Один каталог верхнего уровня без прочих файлов — как обёртка ZIP."""
    try:
        дети = list(корень.iterdir())
    except OSError:
        return None
    файлы = 0
    каталоги: list[Path] = []
    for ребёнок in дети:
        try:
            if ребёнок.is_symlink():
                continue
            if _мусор_верхнего_уровня(ребёнок.name):
                continue
            if ребёнок.is_file():
                файлы += 1
            elif ребёнок.is_dir():
                каталоги.append(ребёнок)
        except OSError:
            continue
    if файлы or len(каталоги) != 1:
        return None
    return каталоги[0]


def _файл_в_корне_или_обёртке(корень: Path, имя: str) -> Path | None:
    прямой = корень / имя
    if _обычный_файл(прямой):
        return прямой
    обёртка = _единственная_обёртка(корень)
    if обёртка is None:
        return None
    внутренний = обёртка / имя
    return внутренний if _обычный_файл(внутренний) else None


def identity_files(корень: Path) -> tuple[Path, ...]:
    """Файлы, по которым считается отпечаток каталога.

    `ConfigDumpInfo.xml`, если есть; иначе `Configuration.xml`. Файл
    `VERSION` выгрузки gitSync добавляется, когда лежит рядом. Одного
    `VERSION` недостаточно: без манифеста конфигуратора это не выгрузка 1С.
    """
    dump_info = _файл_в_корне_или_обёртке(корень, CONFIG_DUMP_INFO)
    configuration = _файл_в_корне_или_обёртке(корень, CONFIGURATION_XML)
    version = _файл_в_корне_или_обёртке(корень, GITSYNC_VERSION)
    if dump_info is not None:
        файлы = [dump_info]
    elif configuration is not None:
        файлы = [configuration]
    else:
        return ()
    if version is not None:
        файлы.append(version)
    return tuple(файлы)


def нет_идентичности(имя: str) -> str:
    return (
        f"{имя}: в каталоге нет ConfigDumpInfo.xml и Configuration.xml — "
        "это не полная выгрузка конфигурации в файлы."
    )


def identity_fingerprint(корень: Path) -> tuple[tuple[int, int, int, int], ...]:
    """`(dev, ino, size, mtime_ns)` каждого файла идентичности."""
    файлы = identity_files(корень)
    if not файлы:
        raise FileNotFoundError(нет_идентичности(корень.name))
    отпечатки: list[tuple[int, int, int, int]] = []
    for путь in файлы:
        stat = путь.stat()
        отпечатки.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return tuple(отпечатки)


def identity_digest(корень: Path) -> str:
    """sha256 содержимого файлов идентичности, с именем файла как разделителем."""
    файлы = identity_files(корень)
    if not файлы:
        raise FileNotFoundError(нет_идентичности(корень.name))
    digest = hashlib.sha256()
    for путь in файлы:
        digest.update(путь.name.encode("utf-8"))
        digest.update(b"\0")
        with путь.open("rb") as поток:
            for блок in iter(lambda: поток.read(1 << 20), b""):
                digest.update(блок)
        digest.update(b"\0")
    return digest.hexdigest()


def listing_size(корень: Path) -> int:
    """Сумма файлов идентичности для показа в списке.

    Дерево выгрузки не обходится: отпечаток и размер каталога — только
    `ConfigDumpInfo.xml` или `Configuration.xml` и, если есть, `VERSION`.
    """
    всего = 0
    for путь in identity_files(корень):
        try:
            всего += путь.stat().st_size
        except OSError:
            continue
    return всего


def _отобрать(карта: dict[str, object]) -> tuple[dict[str, object], str]:
    """Последняя безопасная запись на каждую реальную цель и формат."""
    формат = detect_format(list(карта))
    результат: dict[str, object] = {}
    for имя, payload in карта.items():
        if not is_wanted(имя, формат):
            continue
        результат[имя] = payload
    return результат, формат


def _отобранные_записи(
    zf: zipfile.ZipFile, *, карта: dict[str, zipfile.ZipInfo] | None = None,
) -> tuple[dict[str, zipfile.ZipInfo], str]:
    """Последняя безопасная запись на каждую реальную цель и формат.

    Единый расчёт используют предпроверка, план места и распаковка. Поэтому
    дубль имени и опасный путь не могут занимать байты только в одном из трёх
    представлений.
    """
    записи, формат = _отобрать(
        карта if карта is not None else карта_архива(zf)  # type: ignore[arg-type]
    )
    return записи, формат  # type: ignore[return-value]


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
    if архив.is_dir():
        записи, формат = _отобрать(карта_каталога(архив))  # type: ignore[arg-type]
        отобрано = 0
        for путь in записи.values():
            assert isinstance(путь, Path)
            отобрано += путь.stat().st_size
        return отобрано + _резерв(отобрано), формат
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
SELECTION_VERSION = 5


def extract(архив: Path, корень: Path) -> tuple[int, int]:
    """Достать из архива или каталога модули и формы. Возвращает (файлов, байт).

    Читается членом за членом: развёрнутого архива на диске не возникает.
    Счётчики отражают количество файлов и байт, реально лежащих на диске:
    при дублирующихся имёнах в архиве последняя запись побеждает, и считается
    только фактическое содержимое на диске.

    Обёртка архива (единственный каталог верхнего уровня — см.
    единой картой в `карта_архива`) в целевом пути не воспроизводится: член
    `Обёртка/Catalogs/…` ложится как `<корень>/Catalogs/…`, а не
    `<корень>/Обёртка/Catalogs/…`. Санитизация (`safe_target`) применяется
    ПОСЛЕ снятия обёртки, а не вместо неё — снимается только обёртка из
    имени, правило безопасности остаётся прежним. Каталог отбирается тем же
    правилом: на диск ложится только код, не вся выгрузка.
    """
    корень.mkdir(parents=True, exist_ok=True)
    if архив.is_dir():
        записи, _формат = _отобрать(карта_каталога(архив))  # type: ignore[arg-type]
        байт = 0
        for имя, путь in записи.items():
            assert isinstance(путь, Path)
            цель = safe_target(имя, корень)
            assert цель is not None
            цель.parent.mkdir(parents=True, exist_ok=True)
            with путь.open("rb") as входящий, цель.open("wb") as исходящий:
                shutil.copyfileobj(входящий, исходящий, length=1 << 20)
            байт += путь.stat().st_size
        return len(записи), байт
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
