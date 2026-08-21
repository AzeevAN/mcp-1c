"""Реестр источников: что загружено, откуда и что из этого следует.

Реестр отвечает на единственный по-настоящему важный вопрос — **что доступно
для конкретной конфигурации**. Метаданные есть всегда; справка платформы может
быть новее, старше или отсутствовать; индекс модулей может быть не подключён.
Инструменты сервера не решают это сами, а спрашивают реестр и получают готовый
контекст вместе с фильтром по версии платформы.

Отдельно важно, чего реестр не делает: он не подставляет конфигурацию молча.
Если загружено несколько, а какая нужна — не сказано, будет ошибка со списком.
Тихий выбор «первой попавшейся» приводит к тому, что агент пишет код по чужой
конфигурации и об этом никто не узнаёт.

Данных в памяти немного: две конфигурации — около 85 МБ, справка — около
160 МБ. База данных не нужна, диск используется только чтобы не платить за
разбор при каждом старте.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import index_cache, modules_index
from .dictionary import Dictionary
from .graph import Graph
from .loader import ExportError, load
from .model import Configuration
from .module_content import LocatorIdentity
from .query_parser import looks_like_query_help
from .query_parser import parse_hbk as parse_query_hbk
from .search_keys import coverage as key_coverage
from .search import (
    SearchIndex,
    index_configuration,
    index_fields,
    index_syntax,
    iter_field_refs,
)
from .store import load_syntax, save_syntax
from .syntax_merge import merge_syntax
from .syntax_model import SyntaxIndex, SyntaxItem, parse_version, release
from .syntax_parser import open_file_storage, parse_hbk
from .virtual_tables import TableTemplate, build_table_index

REGISTRY_VERSION = 1

logger = logging.getLogger(__name__)

KIND_CONFIGURATION = "configuration"
KIND_SYNTAX = "syntax"
KIND_QUERY = "query"
KIND_MODULES = "modules"
KIND_EXTENSION = "extension"

STATUS_LOADING = "loading"
STATUS_READY = "ready"
STATUS_ERROR = "error"

# Соотношение версии справки и платформы конфигурации.
RELATION_EXACT = "exact"
RELATION_NEWER = "newer"
RELATION_OLDER = "older"
RELATION_NONE = "none"

_RE_PLATFORM = re.compile(r"\b(\d+\.\d+\.\d+(?:\.\d+)?)\b")


class RegistryError(Exception):
    """Источник не загружается или запрошено то, чего нет."""


class _ModuleOperationCancelled(RegistryError):
    """Устаревший foreground add/reparse отменён новым поколением."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _отпечаток_архива(path: Path) -> tuple[int, int, int, int]:
    """Личность файла на время выбора вида, расчёта места и распаковки."""
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _архив_не_изменился(
    path: Path, ожидался: tuple[int, int, int, int]
) -> bool:
    try:
        return _отпечаток_архива(path) == ожидался
    except OSError:
        return False


def _ошибка_файловой_системы(error: BaseException) -> OSError | None:
    """Первый ``OSError`` в цепочке причин, без показа его текста наружу."""
    текущая: BaseException | None = error
    просмотрено: set[int] = set()
    while текущая is not None and id(текущая) not in просмотрено:
        просмотрено.add(id(текущая))
        if isinstance(текущая, OSError):
            return текущая
        следующая = текущая.__cause__ or текущая.__context__
        текущая = следующая if isinstance(следующая, BaseException) else None
    return None


def _похоже_на_выгрузку_в_файлы(path: Path) -> bool:
    """Выгрузка в файлы против выгрузки schema v1.

    Выгрузок в файлы две: иерархическая (модули в .bsl, .Form) и плоская
    (модули в .txt). Смотрим только имена членов: тело не читается,
    центрального каталога достаточно.
    """
    from . import intake

    try:
        with zipfile.ZipFile(path) as zf:
            карта = intake.карта_архива(zf)
            имена = tuple(карта)
            записи, _ = intake._отобранные_записи(zf, карта=карта)
            есть_код = bool(записи)
    except (OSError, zipfile.BadZipFile):
        return False
    есть_манифест = any(
        и.endswith(("manifest.json", "manifest.xml")) for и in имена
    )
    return есть_код and not есть_манифест


# Пространство имён `Configuration.xml` выгрузки в файлы — как у метаданных,
# так и у расширения: оба используют один формат MDClasses.
_NS_MDCLASSES = "http://v8.1c.ru/8.3/MDClasses"

# `Configuration.xml` боевой конфигурации весит 847 КБ (замер на «Рознице»,
# CHANGELOG → «Найдено»). Запас на порядок сверху — 8 МБ: больше него это уже
# не файл метаданных, а что-то, чему предпочтительнее не верить целиком в
# память по одному объявленному размеру из центрального каталога.
_MAX_CONFIGURATION_XML_SIZE = 8 * 1024 * 1024


def _найти_configuration_xml(zf: zipfile.ZipFile) -> zipfile.ZipInfo | None:
    """`Configuration.xml` из единой нормализованной карты архива."""
    from . import intake

    return intake.карта_архива(zf).get("Configuration.xml")


def _сведения_о_выгрузке(path: Path) -> tuple[bool, str, str]:
    """Расширение это или конфигурация, как зовут (расширение) и версия кода.

    Версия — тег `Version` из тех же `Properties`, что и `Name`/
    `CompatibilityMode`: у выгрузки в файлы это версия конфигурации (или
    расширения), которую разработчик проставил в конфигураторе, а не версия
    платформы (той для точной сборки в Configuration.xml вовсе нет — см.
    комментарий у `add_modules`). Пустая строка — тега нет или он пуст;
    так бывает, и подставлять сюда нечего.

    Смотрит только `Configuration.xml` архива (в корне или в единственном
    каталоге верхнего уровня) — тело не читается: имя расширения и признаки
    лежат в его `Properties`, а модулей в архиве могут быть тысячи.

    Порядок проверки — часть контракта, а не случайность:

    1. **Сначала сильные признаки расширения** (`ObjectBelonging`,
       `ConfigurationExtensionPurpose`). Виден хоть один — дальше выбор
       только между «расширение» (сошлись все четыре условия: оба сильных
       признака, непустой `NamePrefix`, отсутствие `CompatibilityMode`) и
       отказом с объяснением. **В ветку модулей отсюда хода нет ни при
       каком `CompatibilityMode`**: у настоящей конфигурации ни одного из
       двух сильных тегов не бывает вовсе — значит перед нами расширение,
       собранное не так, как ожидалось (или подделка), а не конфигурация.
       Регрессионный тест воспроизводит нарушение именно этого порядка:
       манифест со всеми четырьмя признаками расширения плюс непустой
       `CompatibilityMode`, если `CompatibilityMode` проверялся раньше
       сильных признаков, читался как конфигурация и сносил уже разобранные
       модули.
    2. **Сильных признаков нет — конфигурация, если и только если
       `CompatibilityMode` непустой.** Замер на обеих реальных
       конфигурациях: `Version8_3_21` у иерархической выгрузки, `DontUse` у
       плоской (8.3.5). `NamePrefix` для этого решения ненадёжен: у плоской
       8.3.5 тега нет вовсе. `ConfigurationExtensionCompatibilityMode` —
       ложный признак, он стоит у обеих сторон, отличить по нему нельзя.
    3. **Иначе — отказ (`RegistryError`), без исключений.** Архив без
       `Configuration.xml` вовсе (ни в корне, ни в обёртке), нечитаемый
       (`BadZipFile`/`OSError`), неразбираемый как XML, без узнаваемых
       `Properties`, или без единого положительного признака ни с одной
       стороны — во всех случаях причина отказа называется в тексте.

    Цена ошибки здесь несимметрична: принять расширение (или что угодно
    нечитаемое) за конфигурацию — значит вызывающий пойдёт в ветку модулей
    и снесёт уже разобранный код конфигурации. Поэтому решает только явный
    положительный признак одной из двух сторон, а не отсутствие признаков
    другой — и первым проверяется тот признак, ошибка на котором дороже.
    """

    def отказ(причина: str) -> RegistryError:
        return RegistryError(
            f"{path.name}: {причина} Определить, конфигурация это или "
            "расширение, нечем."
        )

    try:
        with zipfile.ZipFile(path) as zf:
            сведения = _найти_configuration_xml(zf)
            if сведения is None:
                raise отказ(
                    "В архиве не нашлось Configuration.xml ни в корне, ни в "
                    "единственном каталоге верхнего уровня."
                )
            if сведения.file_size > _MAX_CONFIGURATION_XML_SIZE:
                raise RegistryError(
                    f"{path.name}: Configuration.xml весит "
                    f"{сведения.file_size / 1024 / 1024:.1f} МБ по "
                    "заявленному размеру в архиве — это на порядок больше "
                    "боевой конфигурации (847 КБ), похоже на повреждённый "
                    "или поддельный архив."
                )
            содержимое = zf.read(сведения)
    except zipfile.BadZipFile as ошибка:
        raise отказ(
            "Архив ZIP повреждён или его центральный каталог не читается."
        ) from ошибка
    except OSError as ошибка:
        # Валидный центральный каталог, но битый CRC именно у Configuration.xml
        # (обрезанная на записи выгрузка) — тоже сюда: без перехвата человек
        # увидел бы голое исключение вместо объяснения.
        raise отказ(
            "Configuration.xml или архив недоступны для чтения; проверьте "
            "файл и права процесса."
        ) from ошибка

    try:
        корень = ET.fromstring(содержимое)
    except ET.ParseError as ошибка:
        raise отказ(f"Configuration.xml не разбирается как XML ({ошибка}).") from ошибка

    свойства = корень.find(
        f"{{{_NS_MDCLASSES}}}Configuration/{{{_NS_MDCLASSES}}}Properties"
    )
    if свойства is None:
        raise отказ("В Configuration.xml нет узнаваемых Properties (MDClasses).")

    def значение(тег: str) -> str:
        узел = свойства.find(f"{{{_NS_MDCLASSES}}}{тег}")
        return (узел.text or "").strip() if узел is not None and узел.text else ""

    # Сильный признак проверяется ПЕРВЫМ и закрывает дорогу в ветку модулей
    # насовсем, каким бы ни был CompatibilityMode — см. пункт 1 докстроки.
    сильный_признак_расширения = bool(значение("ObjectBelonging")) or bool(
        значение("ConfigurationExtensionPurpose")
    )

    if сильный_признак_расширения:
        это_расширение = (
            not значение("CompatibilityMode")
            and bool(значение("ObjectBelonging"))
            and bool(значение("ConfigurationExtensionPurpose"))
            and bool(значение("NamePrefix"))
        )
        if это_расширение:
            return True, значение("Name"), значение("Version")
        raise отказ(
            "Похоже на выгрузку расширения (есть ObjectBelonging или "
            "ConfigurationExtensionPurpose), но набор признаков неполный — "
            f"CompatibilityMode={значение('CompatibilityMode') or '—'}, "
            f"NamePrefix={значение('NamePrefix') or '—'}."
        )

    if значение("CompatibilityMode"):
        # Единственный положительный признак конфигурации — см. докстроку.
        return False, "", значение("Version")

    raise отказ(
        "Не нашли ни одного из двух сильных признаков расширения "
        "(ObjectBelonging, ConfigurationExtensionPurpose), ни непустого "
        "CompatibilityMode конфигурации — набор признаков неполный."
    )


def _нет_модулей(архив: Path) -> "RegistryError":
    """Отказ архиву, из которого нечего взять. Текст один на обе проверки."""
    return RegistryError(
        f"{архив.name}: в архиве не нашлось ни модулей, ни форм. "
        "Похоже, это выгрузка структуры метаданных — её подают не "
        "через data/incoming/, а формой «Загрузить» на странице "
        "«Источники» (или командой reg-add)."
    )


def _отбираемых_членов(архив: Path) -> int:
    """Сколько членов архива попадёт в отбор. Тело архива не читается.

    Нужно, чтобы отвергнуть негодный архив до того, как `add_modules` снесёт
    прежний разбор: центральный каталог zip знает имена всех членов, а правило
    отбора живёт в `intake` — второго правила здесь не заводится. Обёртка
    архива снимается той же единой картой `intake.карта_архива`, что и в
    `intake.extract`/`planned_size`: без этого предпроверка
    считала бы по сырым именам, а `extract` — по именам без обёртки, и на
    члене вроде `Обёртка/__MACOSX/x.bsl` (после снятия обёртки распознаётся
    как мусор Finder и отбрасывается, а по сырому имени — нет) числа
    разошлись бы. Расхождение само по себе не роняло архив — вторая проверка
    в `_extract_to_temp` (`if not файлов`) ловит пустой результат уже после
    попытки распаковки, — но три вызывающих одного правила обязаны считать
    одно и то же, а не полагаться на подстраховку следующего слоя.
    """
    from . import intake

    with zipfile.ZipFile(архив) as zf:
        записи, _формат = intake._отобранные_записи(zf)
        return len(записи)


def _sweep_stale_extract_tmp(родитель: Path, имя: str) -> None:
    """Осиротевшие временные каталоги `.<имя>.tmp-*` — убрать перед новой
    попыткой распаковки.

    Они остаются, только если процесс погиб между `mkdtemp` и `finally` в
    `_extract_to_temp` или в `_swap_code` (обычный `try/except` такое не
    ловит — не осталось ни одного кода возврата, который можно было бы
    поймать). Следующий разбор той же конфигурации или расширения — первый
    момент, когда есть возможность прибраться.

    `.<имя>.retired-*` уже отвязан от реестра операцией `remove`,
    поэтому его можно дочистить после аварийной остановки. `.<имя>.old-*`
    этой чисткой НЕ трогается: это отставленная копия
    прежнего разбора, уцелевшая после отказа отката (см. `_swap_code`,
    ветку с `RegistryError`), и её удаление — решение человека, а не
    молчаливое действие сервера при следующем нажатии кнопки.
    """
    if not родитель.is_dir():
        return
    for шаблон in (f".{имя}.tmp-*", f".{имя}.retired-*"):
        for путь in родитель.glob(шаблон):
            if путь.is_dir():
                shutil.rmtree(путь, ignore_errors=True)


def _combined_sha256(sources: Iterable["Source"]) -> str:
    """Штамп набора справок: слитый вид зависит от всех, а не от последней."""
    digest = hashlib.sha256()
    for source in sorted(sources, key=lambda s: s.id):
        digest.update(f"{source.id}:{source.sha256}|".encode())
    return digest.hexdigest()


def _is_query_hbk(source_path: Path) -> bool:
    """Вид `.hbk` — по содержимому `FileStorage`, а не по имени файла.

    Проверяется до разбора и до требования версии: у языка запросов версии
    платформы в данных нет по устройству формата (`__categories__` несёт
    только нулевые отметки), и угадывать вид позже, разбором, уже поздно —
    к этому моменту отсутствие версии успело бы завернуть файл как ошибку.

    Файл `.hbk` из-за этого открывается дважды: здесь — только заголовок и
    список страниц, затем ещё раз внутри `parse_hbk`/`parse_query_hbk` — уже
    разбор содержимого. Замер: 8 мс на языке запросов и 154 мс на справке
    платформы (40 МБ) — заметно, но платит это только загрузка источника
    человеком; при старте поднимается уже разобранный `.json.gz`, второго
    открытия `.hbk` там нет вовсе. Экономить эти миллисекунды за счёт
    единого прохода не стали — усложнение того не стоит.
    """
    with open_file_storage(source_path) as storage:
        return looks_like_query_help(storage.namelist())


def _platform_from_path(path: Path) -> str:
    """Версия платформы из пути: data/hbk/8.3.27.2130/shcntx_ru.hbk."""
    for part in (path.name, *reversed(path.parts[:-1])):
        match = _RE_PLATFORM.search(part)
        if match:
            return match.group(1)
    return ""


@dataclass(slots=True)
class Source:
    """Учётная запись источника — отдельно от самих данных.

    Файл может разбираться минуту; всё это время инструменты обязаны отвечать
    по уже загруженным источникам и честно говорить про этот — «загружается».
    """

    id: str
    kind: str
    origin: str = ""
    sha256: str = ""
    loaded_at: str = ""
    platform: str = ""
    status: str = STATUS_LOADING
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    items_total: int = 0
    stored_path: str = ""
    # Версия правила отбора (`intake.SELECTION_VERSION`), под которой код
    # был разобран. 0 — «неизвестно»: так выглядят и записи, сделанные до
    # появления этого поля, и любой источник, для которого версию никто не
    # проставил явно. `intake._состояние` (incoming.py) считает ноль
    # устаревшим, а не свежим — см. комментарий там же.
    selection_version: int = 0
    # Стабильное поколение каталога локаторов. В отличие от внутреннего
    # `_modules_generation`, оно переживает restart и потому позволяет
    # доказать, что warm-кэш и Source относятся к одному снимку кода.
    locator_generation: int = 0
    # Версия конфигурации (или расширения) из Configuration.xml выгрузки в
    # файлы — только у KIND_MODULES/KIND_EXTENSION. Пустая строка везде
    # больше нигде не значит ошибку: у остальных родов источника поля попросту
    # нет. Хранится на Source (не только на LoadedModules), потому что архив,
    # из которого она берётся, на диск не копируется (`add_modules` этого не
    # делает — исходник у человека) — без этого поля `restore()` после
    # рестарта не смог бы восстановить её ни из чего.
    code_version: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "origin": self.origin,
            "sha256": self.sha256,
            "loaded_at": self.loaded_at,
            "platform": self.platform,
            "status": self.status,
            "error": self.error,
            "warnings": list(self.warnings),
            "items_total": self.items_total,
            "stored_path": self.stored_path,
            "selection_version": self.selection_version,
            "locator_generation": self.locator_generation,
            "code_version": self.code_version,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Source":
        locator_generation = raw.get("locator_generation", 0)
        if type(locator_generation) is not int or locator_generation < 0:
            locator_generation = 0
        return cls(
            id=raw["id"],
            kind=raw["kind"],
            origin=raw.get("origin", ""),
            sha256=raw.get("sha256", ""),
            loaded_at=raw.get("loaded_at", ""),
            platform=raw.get("platform", ""),
            status=raw.get("status", STATUS_READY),
            error=raw.get("error", ""),
            warnings=list(raw.get("warnings") or []),
            items_total=raw.get("items_total", 0),
            stored_path=raw.get("stored_path", ""),
            # Отсутствующий ключ — 0, а не текущая SELECTION_VERSION: запись
            # без поля пришла из кода, который его ещё не писал, и о её
            # фактической версии отбора ничего не известно. Подставить
            # текущую версию значило бы соврать «отбор свежий» про запись, о
            # которой мы ничего не знаем, — человек никогда не увидел бы
            # «отбор устарел» для такого источника.
            selection_version=raw.get("selection_version", 0),
            locator_generation=locator_generation,
            code_version=raw.get("code_version", ""),
        )


@dataclass(slots=True)
class LoadedConfiguration:
    source: Source
    config: Configuration
    graph: Graph
    index: SearchIndex
    field_index: SearchIndex


@dataclass(slots=True)
class LoadedSyntax:
    source: Source
    syntax: SyntaxIndex
    index: SearchIndex
    # Имя в нижнем регистре -> элементы. Без него точное совпадение искалось
    # перебором всех 25 тысяч элементов на каждый вызов get_syntax.
    by_name: dict[str, list[SyntaxItem]] = field(default_factory=dict)
    # Язык запросов держится отдельно от слитого вида: у него нет версий, а
    # `merge_syntax` требует их у каждого участника и проставляет `until`
    # тем, кого нет в свежей справке. `index`/`by_name` выше уже включают его
    # элементы вперемешку с платформой — `query` нужен только чтобы отличить
    # «языка запросов нет вовсе» от «загружен, но пуст».
    query: SyntaxIndex | None = None
    # Вид объекта -> шаблоны таблиц запроса. По той же причине, что `by_name`:
    # без него `get_object` по регистру перебирал всю справку и стоил 14,8 мс
    # против 0,04 мс на объекте без таблиц.
    tables: dict[str, list[TableTemplate]] = field(default_factory=dict)

    def find_exact(self, name: str) -> list[SyntaxItem]:
        return self.by_name.get(name.strip().lower(), [])


def _build_name_lookup(syntax: SyntaxIndex) -> dict[str, list[SyntaxItem]]:
    lookup: dict[str, list[SyntaxItem]] = {}
    for item in syntax.items.values():
        # Прежние имена — тоже ключи: агент, работающий на старой платформе,
        # спрашивает так, как элемент назывался там (`Жирный`, а не
        # `Полужирный`), и обязан его найти.
        прежние = [facts.name_ru for facts in item.older if facts.name_ru]
        прежние_полные = [
            f"{item.parent_ru}.{name}" if item.parent_ru else name for name in прежние
        ]
        for key in (
            item.name_ru,
            item.name_en,
            item.full_ru,
            item.full_en,
            *прежние,
            *прежние_полные,
        ):
            if not key:
                continue
            bucket = lookup.setdefault(key.lower(), [])
            if item not in bucket:
                bucket.append(item)
    return lookup


@dataclass(slots=True)
class LoadedModules:
    """Индекс кода одной выгрузки — конфигурации или расширения.

    Второе измерение `ResolvedContext` (design doc, раздел 7): конфигурация
    и её код — `ResolvedContext.modules`, привязанное расширение — отдельным
    полем `ResolvedContext.extension`, тем же типом. Четыре структуры внутри
    — то, что строит `modules_index.построить`/поднимает `поднять_индексы`;
    `LoadedModules` их просто держит вместе с источником и корнем на диске,
    откуда `get_procedure` читает сигнатуры и тела по номеру строки.
    """

    source: Source
    корень: Path
    # При холодном старте запись появляется раньше самих структур: так
    # инструменты отличают «код не загружен» от «индекс строится». Ни одна
    # частичная структура наружу не публикуется — готовый комплект заменяет
    # эту запись целиком под замком.
    оглавление: modules_index.Оглавление | None
    вызовы: modules_index.Вызовы | None
    формы: modules_index.Формы | None
    поиск: SearchIndex | None
    # Версия конфигурации/расширения из Configuration.xml выгрузки в файлы —
    # то же значение, что и `source.code_version`, но под публичным именем
    # провайдера. Сверяется в `ResolvedContext.notes()` с
    # версией загруженных метаданных.
    версия_кода: str
    каталог: modules_index.ModuleCatalog | None = None
    готов: bool = True
    прогресс: tuple[int, int] = (0, 0)
    этап: tuple[int, int] = (4, 4)
    название_этапа: str = "готово"

    def __post_init__(self) -> None:
        # Обычная загрузка и подъём из кэша создают уже готовый комплект.
        # Число модулей берётся из оглавления, а не `Source.items_total`:
        # последнее включает Form.xml и служебные файлы выгрузки.
        if self.готов and self.оглавление is not None and self.прогресс == (0, 0):
            всего = len(self.оглавление.модули)
            self.прогресс = (всего, всего)


@dataclass(frozen=True, slots=True)
class _ModuleOperation:
    """Ранняя reservation живого add/reparse источника кода."""

    source_id: str
    configuration: str
    configuration_source: Source
    generation: int
    locator_generation: int
    lifecycle_lock: threading.Lock


@dataclass(slots=True)
class ResolvedContext:
    """Что доступно для одной конфигурации. То, что получают инструменты.

    Конфигурации может не быть вовсе: справка платформы — самостоятельный
    источник и полезна сама по себе. В этом случае работают инструменты
    синтаксиса, но без фильтрации по версии — не от чего отталкиваться.
    """

    configuration: LoadedConfiguration | None = None
    syntax: LoadedSyntax | None = None
    syntax_relation: str = RELATION_NONE
    syntax_hidden: int = 0
    modules: LoadedModules | None = None
    extension: LoadedModules | None = None

    @property
    def name(self) -> str:
        return self.configuration.config.name if self.configuration else ""

    @property
    def platform(self) -> str:
        return self.configuration.config.platform if self.configuration else ""

    @property
    def syntax_platform(self) -> str:
        """Справка, по которой на самом деле строится ответ этой конфигурации.

        `source.platform` называет объединённый источник, то есть самую свежую
        из загруженных справок. Пока справка была одна, это совпадало; со
        слитым видом конфигурация 8.3.5 получала строку «справка 8.3.27 —
        версия совпадает с конфигурацией»: соотношение верное (справка её
        релиза загружена), номер чужой, и фраза противоречит сама себе.
        """
        if self.syntax is None:
            return ""
        if self.configuration is not None:
            точная = self.syntax.syntax.help_for(self.platform)
            if точная:
                return точная
        return self.syntax.source.platform

    def syntax_filter(self) -> Callable[[SyntaxItem], bool]:
        """Предикат «элемент существует в платформе этой конфигурации».

        Отсекаем, а не предупреждаем: предупреждение агент может пропустить,
        отсутствующий в выдаче метод — нет.

        Фильтр работает всегда, когда известна версия конфигурации. Прежде он
        включался только для справки новее — у одной справки других поводов не
        было. В слитом виде появилась вторая граница: элементы, которых в
        версии конфигурации уже нет (`КаноническаяЗаписьXML` живёт до 8.3.5),
        и их точно так же нельзя показывать.
        """
        if self.configuration is None or self.syntax is None:
            return lambda item: True
        target = parse_version(self.platform)
        if not target:
            return lambda item: True
        return lambda item: item.available_in_tuple(target)

    def code_notes(self) -> list[str]:
        """Оговорки, относящиеся только к ответам по коду."""
        if self.configuration is None or self.modules is None:
            return []
        config = self.configuration.config
        if (
            self.modules.версия_кода
            and config.version
            and self.modules.версия_кода != config.version
        ):
            return [
                f"Код модулей выгружен для версии {self.modules.версия_кода}, "
                f"загруженные метаданные — версии {config.version}: "
                "конфигурация и код разошлись, номера строк и состав "
                "процедур могут не совпадать с тем, что видит платформа."
            ]
        return []

    def notes(
        self, *, critical_only: bool = False, include_code: bool = True
    ) -> list[str]:
        """Оговорки, которые сервер обязан передать агенту.

        `critical_only` — только то, что влияет на достоверность ответа.
        Сообщение «справка новее, скрыто N элементов» на каждый вызов не
        нужно: фильтрация уже отработала, и повторять её в каждом ответе —
        значит приучить агента пролистывать предупреждения.
        """
        notes: list[str] = []

        if self.configuration is None:
            if self.syntax is not None:
                if self.syntax.syntax.platforms:
                    notes.append(
                        "Конфигурация не загружена — фильтрация по версии платформы "
                        "выключена. В выдаче есть всё, что описано в справке "
                        f"{self.syntax.source.platform}, включая методы, которых "
                        "может не быть в вашей версии."
                    )
                else:
                    # Справки платформы нет вовсе — загружен только язык
                    # запросов, у которого версий не бывает по устройству
                    # формата. Прежний текст подставлял сюда пустую строку
                    # («справке , включая методы») — врал сразу по двум
                    # пунктам: имени нет, и версии тут в принципе не бывает.
                    notes.append(
                        "Синтаксис платформы недоступен — подключён только "
                        "язык запросов. Загрузите `shcntx_ru.hbk`, чтобы "
                        "искать по методам и свойствам платформы."
                    )
            return notes

        config = self.configuration.config

        if config.truncated:
            notes.append(
                "Выгрузка конфигурации неполная — сделана с ограничением числа "
                "объектов. Ответы могут быть неверными."
            )
        if not config.predefined_available:
            notes.append(
                "В выгрузке нет предопределённых элементов: имена вида "
                "`Справочники.X.Y` проверить нечем."
            )
        notes.extend(config.warnings)

        if include_code:
            # Загрузка новой выгрузки метаданных под тем же именем подменяет
            # конфигурацию (`add_configuration`), а индекс кода остаётся
            # прежним — они молча расходятся, если не сказать вслух (см.
            # `docs/modules-provider-design.md`, раздел 9).
            notes.extend(self.code_notes())

        if self.syntax is None:
            notes.append(
                "Справка платформы не подключена — синтаксис недоступен."
            )
        elif self.syntax_relation == RELATION_NONE:
            # Эта ветка недостижима иначе как через язык запросов: справку
            # без версии `add_syntax` не принимает (см. проверку в
            # `registry.py`), а случай «синтаксиса нет вовсе» отсекла ветка
            # выше. Единственный источник, которому неоткуда взять версию
            # платформы, — язык запросов, и старый текст («версия … не
            # определена») звучал так, будто платформенная справка есть, но
            # с дефектом, — тогда как её нет вовсе.
            notes.append(
                "Синтаксис платформы недоступен — подключён только язык "
                "запросов. Методы и свойства платформы не найдутся, пока не "
                "загружен `shcntx_ru.hbk`."
            )
        elif self.syntax_relation == RELATION_OLDER:
            загружены = ", ".join(self.syntax.syntax.platforms)
            notes.append(
                f"Справки платформы {self.platform} нет — загружены {загружены}. "
                "Наличие элементов отфильтровано, но сигнатуры и контексты "
                "доступности могли измениться. Загрузите справку "
                f"{self.platform}, чтобы ответы стали точными."
            )
        elif (
            not critical_only
            and self.syntax_relation == RELATION_NEWER
            and self.syntax_hidden
        ):
            notes.append(
                f"Справка платформы {self.syntax.source.platform} новее "
                f"конфигурации ({self.platform}): скрыто "
                f"{self.syntax_hidden} элементов, которых в её версии ещё нет."
            )
        return notes


class Registry:
    """Всё загруженное и правила сопоставления версий."""

    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self.sources_dir = self.data_dir / "sources"
        self.index_dir = self.data_dir / "index"
        self.cache_dir = self.index_dir / "cache"
        self.bootstrap_dir = self.data_dir / "bootstrap"
        # Каталог, содержимое которого разбирается по команде, а не при
        # старте: гигабайтную выгрузку так разбирать нельзя. Сервер его
        # НЕ удаляет — исходник принадлежит человеку.
        self.incoming_dir = self.data_dir / "incoming"
        self.modules_dir = self.data_dir / "modules"
        # Каталог кода расширений — рядом с `modules_dir`, но не внутри него:
        # у расширения свой ключ и своя жизнь, а не подкаталог конфигурации.
        self.extensions_dir = self.data_dir / "extensions"
        self.registry_path = self.data_dir / "registry.json"
        # Малый write-ahead marker делает рокировку каталога кода
        # восстанавливаемой после SIGKILL между rename и registry.json.
        self._module_swap_path = self.data_dir / ".modules-swap.json"
        self.dictionary_path = self.data_dir / "dictionary.json"
        self.dictionary = Dictionary.load(self.dictionary_path)

        self._lock = threading.RLock()
        # Повторные startup/reload сериализуются отдельно: долгий reload не
        # держит основной замок и не мешает `resolve()`, но два восстановления
        # не могут одновременно резервировать одну фоновую сборку.
        self._startup_lock = threading.Lock()
        # Фоновая сборка может закончиться раньше последовательного restore.
        # Поколения различают успешно завершённый полный startup и аварийно
        # оборванный частичный снимок: второй нельзя писать в registry.json.
        self._startup_generation = 0
        self._last_successful_startup_generation = 0
        # Все writers кэша модулей и смена поколения источника идут через
        # один порядок `cache -> registry`. Так старый writer не успевает
        # записать свой пакет после рокировки нового кода.
        self._modules_cache_lock = threading.Lock()
        self.configurations: dict[str, LoadedConfiguration] = {}
        # Справки версий — учётными записями, а не содержимым: разобранная
        # справка весит около 60 МБ, и держать их все ради редких пересборок
        # значит платить памятью, кратной числу версий. Содержимое лежит на
        # диске и поднимается на время сборки слитого вида.
        self.syntax_versions: dict[str, Source] = {}
        self.syntax: LoadedSyntax | None = None
        # Справка по языку запросов — учётная запись отдельно от версий
        # платформы: она в `syntax_versions` не попадает (см. `add_syntax`),
        # поэтому нужен свой якорь, чтобы её не потерять.
        self.query_source: Source | None = None
        self.sources: dict[str, Source] = {}
        # Индекс кода — по id источника (`<Имя>:modules` и `<Имя>:ext:<Имя>`),
        # тот же ключ, что и в `self.sources`: у конфигурации ровно одна
        # выгрузка кода, у расширений их может быть несколько, и `resolve()`
        # достаёт нужную запись напрямую по составленному ключу.
        self.modules: dict[str, LoadedModules] = {}
        # Сильная ссылка нужна не потоку (он живёт сам), а повторному
        # `startup()`: пока предыдущая сборка ещё идёт, reload не должен
        # запускать второй разбор того же корпуса и удваивать расход памяти.
        self._module_builds: dict[str, threading.Thread] = {}
        self._modules_generation: dict[str, int] = {}
        # Stable locator identity тоже монотонна в пределах процесса и не
        # удаляется вместе с Source: иначе remove -> add того же ZIP дал бы
        # прежнее поколение и создал ABA для уже начатого чтения.
        self._locator_generation: dict[str, int] = {}
        # Диагностика пишется при публикации поколения, а не на каждом запросе.
        # Множество также страхует повторный путь публикации от дубля строки.
        self._logged_module_limitations: set[tuple[str, int, str]] = set()
        self._module_recovery_blocked = False
        # Один постоянный lock на source_id защищает весь foreground
        # lifecycle: sweep -> extract -> build -> publish/cleanup. Записи не
        # удаляются: удаление и новый setdefault между двумя waiter
        # создали бы ABA и снова два одновременных tmp.
        self._module_operation_locks: dict[str, threading.Lock] = {}
        # Foreground add/reparse резервирует source_id до долгой
        # распаковки. Restore видит reservation и не публикует поверх
        # неё старую запись из registry.json.
        self._module_operations: dict[str, _ModuleOperation] = {}
        # Соотношение версий не меняется, пока не сменились источники, а его
        # вычисление — перебор всех элементов справки. Без кэша это давало
        # 16 мс на каждый вызов любого инструмента.
        self._relation_cache: dict[
            str,
            tuple[LoadedConfiguration, LoadedSyntax | None, str, int],
        ] = {}

    # ------------------------------------------------------------- источники

    def _relative(self, path: Path) -> str:
        """Путь для записи в реестр — относительно каталога данных.

        Каталог данных монтируется в контейнер как /data, а на машине
        разработчика лежит в ./data. Абсолютные пути сделали бы реестр
        непереносимым между этими двумя случаями.
        """
        try:
            return str(Path(path).resolve().relative_to(self.data_dir.resolve()))
        except ValueError:
            return str(path)

    def _absolute(self, stored: str) -> Path:
        path = Path(stored)
        return path if path.is_absolute() else self.data_dir / path

    # --------------------------------------------------------------- кэш

    # Виды индексов, которые кэшируются для каждого рода источника.
    #
    # `KIND_QUERY` нужен здесь, даже когда справок платформы нет вовсе: тогда
    # `LoadedSyntax.source` в `_prepare_syntax` — сам источник языка запросов,
    # и кэш поиска/таблицы имён ложится под его id. Без записи здесь
    # `_cached_names()` не признал бы эти файлы своими, и `sweep` на первом
    # же старте сносил бы кэш, который сам только что построил `restore()`.
    CACHE_KINDS = {
        KIND_CONFIGURATION: ("objects", "fields"),
        KIND_SYNTAX: ("syntax", "lookup"),
        KIND_QUERY: ("syntax", "lookup"),
        # Четыре структуры — четыре имени, не одно общее. `sweep` считает
        # своим только то, что названо здесь; одно общее имя на все четыре
        # значило бы, что три индекса из четырёх
        # сносятся на первом же старте, молча, и пересобираются заново
        # каждый раз (`tests/test_index_cache_modules.py`).
        KIND_MODULES: ("modules-toc", "modules-calls", "modules-forms", "modules-search"),
        # Тот же набор видов, что у модулей конфигурации: провайдер поиска
        # по коду не должен различать их источники. Без этой записи `sweep`
        # счёл бы кэш расширения ничьим на первом же старте — ровно то, от
        # чего предупреждает комментарий выше для KIND_QUERY.
        KIND_EXTENSION: ("modules-toc", "modules-calls", "modules-forms", "modules-search"),
    }

    def _cache_path(self, source_id: str, kind: str) -> Path:
        return index_cache.path_for(self.cache_dir, source_id, kind)

    def _cached_names(self) -> set[str]:
        """Имена файлов, которые кэшу разрешено иметь прямо сейчас."""
        return {
            self._cache_path(source.id, kind).name
            for source in self.sources.values()
            for kind in self.CACHE_KINDS.get(source.kind, ())
        }

    def _drop_cache(self, source_id: str, kind: str) -> None:
        """Снести все файлы расходного кэша, какие позволяет том.

        Ни один отказ `unlink` не прерывает снятие Source: кэш можно
        пересобрать, а оставшийся файл без Source никто не поднимет.
        Ошибка одного файла не мешает попробовать удалить остальные.
        """
        for index_kind in self.CACHE_KINDS.get(kind, ()):
            try:
                self._cache_path(source_id, index_kind).unlink(missing_ok=True)
            except OSError:
                continue

    def _configuration_index(self, config: Configuration, source: Source) -> SearchIndex:
        path = self._cache_path(source.id, "objects")
        synonyms = self.dictionary.synonyms()
        aliases = self.dictionary.aliases_for(config.name)

        cached = index_cache.load(
            path,
            config.objects,
            source_sha256=source.sha256,
            kind="objects",
            synonyms=synonyms,
            aliases=aliases,
        )
        if cached is not None:
            return cached

        index = index_configuration(config, synonyms=synonyms, aliases=aliases)
        index_cache.save(index, path, source_sha256=source.sha256, kind="objects")
        return index

    def _field_index(self, config: Configuration, source: Source) -> SearchIndex:
        path = self._cache_path(source.id, "fields")
        synonyms = self.dictionary.synonyms()

        # Полезная нагрузка собирается только когда есть что поднимать: на
        # промахе она всё равно построится внутри index_fields.
        if path.exists():
            payloads = {ref.full_name: ref for ref in iter_field_refs(config)}
            cached = index_cache.load(
                path,
                payloads,
                source_sha256=source.sha256,
                kind="fields",
                synonyms=synonyms,
            )
            if cached is not None:
                return cached

        index = index_fields(config, synonyms=synonyms)
        index_cache.save(index, path, source_sha256=source.sha256, kind="fields")
        return index

    def _syntax_index(self, syntax: SyntaxIndex, source: Source) -> SearchIndex:
        path = self._cache_path(source.id, "syntax")
        cached = index_cache.load(
            path, syntax.items, source_sha256=source.sha256, kind="syntax"
        )
        if cached is not None:
            return cached

        index = index_syntax(syntax)
        index_cache.save(index, path, source_sha256=source.sha256, kind="syntax")
        return index

    def _syntax_lookup(
        self, syntax: SyntaxIndex, source: Source
    ) -> dict[str, list[SyntaxItem]]:
        """Словарь имён справки. В кэше — идентификаторы, не сами элементы."""
        path = self._cache_path(source.id, "lookup")
        raw = index_cache.load_blob(path, source_sha256=source.sha256, kind="lookup")
        if raw is not None:
            try:
                return {key: [syntax.items[i] for i in ids] for key, ids in raw.items()}
            except (KeyError, AttributeError, TypeError):
                # Кэш и справка разошлись — строим заново, это дешевле разбора.
                pass

        lookup = _build_name_lookup(syntax)
        index_cache.save_blob(
            {key: [item.id for item in items] for key, items in lookup.items()},
            path,
            source_sha256=source.sha256,
            kind="lookup",
        )
        return lookup

    def _store_source(self, path: Path, subdir: str) -> Path:
        """Положить исходник рядом с индексом — чтобы пересобрать без 1С."""
        target_dir = self.sources_dir / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if path.resolve() != target.resolve():
            shutil.copy2(path, target)
        return target

    def add_configuration(
        self,
        path: str | Path,
        *,
        keep_source: bool = True,
        known_sha256: str = "",
    ) -> Source:
        source_path = Path(path)
        # При восстановлении читается сохранённая копия, но хеш должен остаться
        # от файла, который пользователь положил изначально. Иначе bootstrap
        # сочтёт исходник новым и разберёт его заново — при каждом старте.
        digest = known_sha256 or _sha256(source_path)

        try:
            config = load(source_path)
        except ExportError as error:
            raise RegistryError(f"{source_path.name}: {error}") from error

        if not config.name:
            raise RegistryError(f"{source_path.name}: в манифесте нет имени конфигурации.")

        stored = self._store_source(source_path, "configurations") if keep_source else source_path

        source = Source(
            id=config.name,
            kind=KIND_CONFIGURATION,
            origin=source_path.name,
            sha256=digest,
            loaded_at=_now(),
            platform=config.platform,
            status=STATUS_READY,
            warnings=list(config.warnings),
            items_total=len(config),
            stored_path=self._relative(stored),
        )

        # Индексы строятся до подмены: наружу не должно попасть полусобранное.
        loaded = LoadedConfiguration(
            source=source,
            config=config,
            graph=Graph(config),
            index=self._configuration_index(config, source),
            field_index=self._field_index(config, source),
        )
        with self._lock:
            self.configurations[config.name] = loaded
            self.sources[source.id] = source
            self._relation_cache.pop(config.name, None)
        return source

    def _modules_root(self, configuration: str) -> Path:
        """Каталог кода конфигурации внутри `modules_dir`.

        Имя чистится тем же правилом, что и имена файлов кэша
        (`index_cache.safe_name`): оно приходит из манифеста, а там встречается
        и косая черта, и двоеточие.

        Чистки мало: точку правило сохраняет, поэтому имя «..» проходит через
        неё неизменным. Каталог мы теперь удаляем перед распаковкой и при
        снятии источника — промах увёл бы удаление за пределы `modules_dir`,
        вплоть до соседнего `incoming/`, который сервер трогать не вправе.
        Поэтому путь проверяется, а не предполагается верным.
        """
        корень = (self.modules_dir / index_cache.safe_name(configuration)).resolve()
        база = self.modules_dir.resolve()
        if корень == база or база not in корень.parents:
            raise RegistryError(
                f"Имя конфигурации «{configuration}» не годится для каталога "
                "кода: путь уходит за пределы data/modules."
            )
        return корень

    def _drop_modules_root(self, корень: Path) -> None:
        """Снести каталог кода. Только внутри `modules_dir` и только его.

        Проверка повторяется здесь намеренно: путь может прийти из
        `registry.json`, где его правил кто угодно, а рядом с `data/modules/`
        лежит `data/incoming/` с исходником человека.
        """
        цель = корень.resolve()
        база = self.modules_dir.resolve()
        if цель == база or база not in цель.parents:
            return
        if цель.is_dir():
            shutil.rmtree(цель, ignore_errors=True)

    # Сегменты, которые после чистки нельзя пускать в путь ни на одном
    # уровне: пустая строка — от имени, целиком состоящего из символов,
    # которые `index_cache.safe_name` заменяет; `.` и `..` — потому что
    # `pathlib` схлопывает их при `resolve()` ДО сравнения с базой. Один
    # уровень (`_modules_root`) это уже ловит побочно: `корень == база`
    # верно и когда единственный сегмент — `.`. Два уровня (расширение) —
    # нет: `extensions_dir/<Конфигурация>/.` резолвится в
    # `extensions_dir/<Конфигурация>` — существующий чужой каталог, а не в
    # `extensions_dir`, и проверка `корень == база` его пропускает. Отсюда и
    # ловится явно, до `resolve()`, а не полагаясь на побочный эффект.
    _НЕГОДНЫЕ_СЕГМЕНТЫ = ("", ".", "..")

    def _extension_root(self, configuration: str, extension: str) -> Path:
        """Каталог кода расширения: `extensions_dir/<Конфигурация>/<Расширение>`.

        Два уровня, а не склейка имён в одно: `index_cache.safe_name`
        схлопывает необычные символы в подчёркивание, и склейка вида
        `Розница@Доп` дала бы `Розница_Доп` — путь, который может совпасть с
        конфигурацией, названной так же. Каждый уровень чистится тем же
        правилом, что и `_modules_root`, каждый сегмент после чистки
        проверяется на `_НЕГОДНЫЕ_СЕГМЕНТЫ`, и путь так же проверяется на
        принадлежность корню: имя расширения приходит из выгрузки человека,
        а не проверено заранее.
        """
        сегмент_конфигурации = index_cache.safe_name(configuration)
        сегмент_расширения = index_cache.safe_name(extension)
        for сегмент, кого, имя in (
            (сегмент_конфигурации, "конфигурации", configuration),
            (сегмент_расширения, "расширения", extension),
        ):
            if сегмент in self._НЕГОДНЫЕ_СЕГМЕНТЫ:
                raise RegistryError(
                    f"Имя {кого} «{имя}» не годится для каталога кода: после "
                    f"чистки от него остаётся «{сегмент or '(пусто)'}» — с "
                    "таким сегментом путь мог бы указать на чужой каталог."
                )
        корень = (
            self.extensions_dir / сегмент_конфигурации / сегмент_расширения
        ).resolve()
        база = self.extensions_dir.resolve()
        if корень == база or база not in корень.parents:
            raise RegistryError(
                f"Расширение «{extension}» конфигурации «{configuration}» не "
                "годится для каталога кода: путь уходит за пределы "
                "data/extensions."
            )
        return корень

    def _drop_extension_root(self, корень: Path) -> None:
        """Снести каталог кода расширения. Только внутри `extensions_dir`.

        Проверка повторяется, как и в `_drop_modules_root`: путь может прийти
        из `registry.json`, где его правил кто угодно.
        """
        цель = корень.resolve()
        база = self.extensions_dir.resolve()
        if цель == база or база not in цель.parents:
            return
        if цель.is_dir():
            shutil.rmtree(цель, ignore_errors=True)

    def _retire_code_root(self, корень: Path, kind: str) -> Path | None:
        """Быстро отвязать старое поколение от canonical path.

        Вызывается под `cache -> registry` до снятия Source. Долгий
        `rmtree` потом работает только по возвращённому уникальному пути и
        физически не может задеть новое поколение на `корень`.
        """
        цель = корень.resolve()
        база = (
            self.modules_dir.resolve()
            if kind == KIND_MODULES
            else self.extensions_dir.resolve()
        )
        if цель == база or база not in цель.parents or not цель.is_dir():
            return None
        отставленный = цель.parent / f".{цель.name}.retired-{uuid.uuid4().hex}"
        try:
            цель.rename(отставленный)
        except OSError as error:
            raise RegistryError(
                f"Каталог кода «{цель}» не снят: не удалось "
                f"отставить его в «{отставленный}»: {error}"
            ) from error
        return отставленный

    def _extract_to_temp(self, архив: Path, корень: Path) -> tuple[Path, str, int]:
        """Хеш и распаковка кода — во временный каталог рядом с `корень`, без
        рокировки: та — отдельно, в `_swap_code`.

        Разделены намеренно: индекс провайдера `modules` обязан строиться по
        `временный`, пока `корень` (если уже существовал) ещё несёт прежний
        код — построй его ПОСЛЕ рокировки, и всё время сборки (секунды на
        живой выгрузке) на диске уже новый код, а в памяти ещё старое
        оглавление: `get_procedure` в это окно отдавал бы куски чужих
        процедур по старым номерам строк, без единой пометки (см.
        `docs/modules-provider-design.md`, раздел 9). Вызывающий
        (`add_modules`/`_add_extension`) строит индекс между этим вызовом и
        `_swap_code` — здесь только распаковка.

        Что гарантируется: ошибка `intake.extract` (битый CRC у модуля, а не
        у манифеста; то же дали бы кончившееся место или права) и пустой
        результат (все члены отсеклись санитизацией `safe_target`) убирают
        временный каталог и пробрасывают исключение — до `корень` дело не
        доходит вовсе, он этим вызовом не тронут ни при первом разборе, ни
        при переразборе.
        """
        from . import intake

        # Хеш считается после проверки годности архива (она — на вызывающей
        # стороне, до этого метода): полный проход по файлу, и платить им за
        # архив, который мы всё равно не возьмём, незачем.
        временный: Path | None = None
        try:
            digest = _sha256(архив)
            корень.parent.mkdir(parents=True, exist_ok=True)
            _sweep_stale_extract_tmp(корень.parent, корень.name)
            временный = Path(
                tempfile.mkdtemp(dir=корень.parent, prefix=f".{корень.name}.tmp-")
            )
        # Права — как у прежнего разбора, а не 0700, которые ставит
        # `mkdtemp`: без этой строки человек на хосте (bind-mount, контейнер
        # под uid 10001) после переразбора вдруг перестал бы заходить в
        # каталог, который до этого читался свободно. Прежнего разбора нет
        # — обычные 0755, как у каталога, который раньше создавала сама
        # распаковка через `mkdir`.
            try:
                режим = корень.stat().st_mode & 0o777 if корень.exists() else 0o755
            except OSError:
                режим = 0o755
            временный.chmod(режим)

            файлов, _байт = intake.extract(архив, временный)
            if not файлов:
                # Предпроверка считает по именам из центрального каталога, а
                # `extract` прогоняет каждый член ещё и через
                # `intake.safe_target` — тот отвергает абсолютные пути и
                # `..`. Архив, у которого все отбираемые члены такие,
                # предпроверку проходит, а на диск не кладёт ничего: без
                # этой проверки завёлся бы источник со `status=ready` при
                # пустом каталоге.
                raise _нет_модулей(архив)
        except BaseException as error:
            if временный is not None:
                shutil.rmtree(временный, ignore_errors=True)
            if isinstance(error, OSError) and error.errno == errno.ENOSPC:
                raise RegistryError(
                    f"{архив.name}: свободное место закончилось во время "
                    "распаковки; прежний разбор сохранён. Освободите место "
                    "и повторите."
                ) from error
            raise
        assert временный is not None
        return временный, digest, файлов

    def _swap_code(
        self, архив: Path, корень: Path, временный: Path
    ) -> Path | None:
        """Рокировка: уже проверенный `временный` (индекс над ним, если он
        строится, к этому моменту уже построен вызывающим) встаёт на место
        `корень`.

        Не снос на месте, а рокировка: если `корень` уже существует, он
        сначала отставляется в сторону (`корень -> отставленный`), затем
        распакованное встаёт на его место (`временный -> корень`), и только
        потом отставленное возвращается вызывающему и сносится только после
        успешной записи кэша и `registry.json`. Это не
        гипотетика: `_drop_modules_root`/`_drop_extension_root` зовут
        `rmtree(..., ignore_errors=True)`, и частичный отказ по правам
        (bind-mount, контейнер под uid 10001 — то, о чём предупреждает форма
        приёма) оставил бы каталог непустым; снос НА МЕСТЕ КОРНЯ уронил бы
        rename с `ENOTEMPTY` и наполовину снёс прежний разбор.

        Если это (второе) переименование не удалось, отставленное
        возвращается на место корня — это тоже гарантия, но не безусловная:
        если и ОБРАТНОЕ переименование не удалось (природа отказа у обоих
        одна — те же права, тот же bind-mount, «два подряд» не выдумка),
        прежний разбор физически цел, но лежит не там, где его ждёт реестр
        (`отставленный`), а на месте `корня` — пустота или частичная
        распаковка. Молчать здесь нельзя: наверх летит `RegistryError` с
        обоими безопасными путями относительно `data/`, чтобы человек мог
        вернуть каталог руками, но без абсолютного пути хоста и текста ОС.
        Это единственный путь рокировки, который не восстанавливает состояние
        сам, — и единственный, где это явно названо (исключением с текстом),
        а не подразумевается докстрочной оговоркой.

        Сами два `rename` не атомарны вместе. Поэтому вызывающий до первого
        из них пишет `.modules-swap.json`; startup сравнивает записанное там
        новое поколение с `registry.json` и либо завершает публикацию, либо
        возвращает `.old-*` на canonical path. Маркер удаляется только после
        успешной записи реестра и уборки отставленного корня.

        Вызывающий держит эту функцию под `self._lock` (`add_modules`/
        `_add_extension`) вместе с обновлением `self.sources`/`self.modules`
        — «каталог и индекс меняются вместе под замком» в буквальном смысле,
        а не только в смысле порядка действий: сама операция быстрая
        (переименования, не копирование), долгая часть (сборка индекса) к
        этому моменту уже позади и прошла БЕЗ замка.
        """
        try:
            if корень.exists():
                # Суффикс переиспользует случайную часть имени `временный`
                # — она уже гарантированно уникальна, второй `mkdtemp` ради
                # того же не нужен: `отставленный` только резервирует имя
                # под `rename`, создавать его заранее незачем.
                случайное = временный.name.rsplit("-", 1)[-1]
                отставленный = корень.parent / f".{корень.name}.old-{случайное}"
                корень.rename(отставленный)
                try:
                    временный.rename(корень)
                except BaseException:
                    try:
                        отставленный.rename(корень)
                    except OSError as ошибка_отката:
                        # Оба переименования не удались — прежний разбор
                        # физически цел, но лежит не там, где его ждёт
                        # реестр. Молчать нельзя: без явного текста человек
                        # узнал бы об этом только когда `restore()` после
                        # рестарта не найдёт `stored_path` и тихо выкинет
                        # источник — а данные всё это время были на месте,
                        # просто под другим именем.
                        if корень.parent == self.modules_dir:
                            безопасный_каталог = "data/modules"
                        elif корень.parent.parent == self.extensions_dir:
                            безопасный_каталог = (
                                f"data/extensions/{корень.parent.name}"
                            )
                        else:
                            безопасный_каталог = "каталог кода"
                        прежний_путь = (
                            f"{безопасный_каталог}/{отставленный.name}"
                        )
                        новый_путь = f"{безопасный_каталог}/{корень.name}"
                        raise RegistryError(
                            f"{архив.name}: переразбор не удался, и откат "
                            f"тоже. Прежний разбор остался в «{прежний_путь}»"
                            f", в «{новый_путь}» — пустой или частично "
                            "распакованный новый. Разберитесь руками: "
                            f"который из каталогов верный, переименуйте в "
                            f"«{корень.name}», лишний удалите."
                        ) from ошибка_отката
                    raise
                else:
                    return отставленный
            else:
                временный.rename(корень)
                return None
        finally:
            shutil.rmtree(временный, ignore_errors=True)

    def _начать_рокировку_кода(
        self, source: Source, корень: Path, временный: Path
    ) -> Path | None:
        """Записать намерение до первого rename и вернуть путь старого root."""
        отставленный: Path | None = None
        if корень.exists():
            случайное = временный.name.rsplit("-", 1)[-1]
            отставленный = корень.parent / f".{корень.name}.old-{случайное}"
        payload = {
            "version": 1,
            "source_id": source.id,
            "new_sha256": source.sha256,
            "new_selection_version": source.selection_version,
            "root": self._relative(корень),
            "temporary": self._relative(временный),
            "detached": (
                self._relative(отставленный) if отставленный is not None else None
            ),
        }
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._module_swap_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(self._module_swap_path)
        except OSError as error:
            raise RegistryError(
                "Не удалось записать журнал рокировки кода; прежний "
                "источник сохранён. Проверьте каталог данных и права процесса."
            ) from error
        return отставленный

    def _путь_из_журнала_рокировки(self, raw: object) -> Path:
        if not isinstance(raw, str) or not raw:
            raise ValueError("invalid transaction path")
        path = self._absolute(raw).resolve()
        path.relative_to(self.data_dir.resolve())
        return path

    def _восстановить_рокировку_кода(self) -> list[str]:
        """Завершить или откатить пережившую процесс рокировку по registry."""
        if not self._module_swap_path.exists():
            return []
        try:
            marker = json.loads(
                self._module_swap_path.read_text(encoding="utf-8")
            )
            if not isinstance(marker, dict):
                raise ValueError("transaction marker is not an object")
            if marker.get("version") != 1:
                raise ValueError("invalid transaction version")
            source_id = marker["source_id"]
            new_sha256 = marker["new_sha256"]
            selection_version = marker["new_selection_version"]
            if (
                not isinstance(source_id, str)
                or not isinstance(new_sha256, str)
                or isinstance(selection_version, bool)
                or not isinstance(selection_version, int)
            ):
                raise ValueError("invalid transaction identity")
            корень = self._путь_из_журнала_рокировки(marker["root"])
            временный = self._путь_из_журнала_рокировки(marker["temporary"])
            raw_detached = marker.get("detached")
            отставленный = (
                self._путь_из_журнала_рокировки(raw_detached)
                if raw_detached is not None
                else None
            )
            try:
                if source_id.endswith(":modules"):
                    configuration = source_id[: -len(":modules")]
                    if not configuration:
                        raise ValueError("empty configuration")
                    ожидаемый_корень = self._modules_root(configuration)
                else:
                    configuration, separator, extension = source_id.partition(
                        ":ext:"
                    )
                    if not separator or not configuration or not extension:
                        raise ValueError("invalid source id")
                    ожидаемый_корень = self._extension_root(
                        configuration, extension
                    )
            except RegistryError as error:
                raise ValueError("invalid source root") from error
            if корень != ожидаемый_корень:
                raise ValueError("root does not match source")
            prefix = f".{корень.name}.tmp-"
            if временный.parent != корень.parent or not временный.name.startswith(
                prefix
            ):
                raise ValueError("invalid temporary path")
            token = временный.name[len(prefix) :]
            if not token or временный == корень:
                raise ValueError("invalid temporary token")
            if отставленный is not None and (
                отставленный.parent != корень.parent
                or отставленный.name != f".{корень.name}.old-{token}"
                or отставленный in (корень, временный)
            ):
                raise ValueError("invalid detached path")

            registry_payload = {}
            if self.registry_path.exists():
                registry_payload = json.loads(
                    self.registry_path.read_text(encoding="utf-8")
                )
            if not isinstance(registry_payload, dict):
                raise ValueError("registry is not an object")
            raw_sources = registry_payload.get("sources") or []
            if not isinstance(raw_sources, list):
                raise ValueError("registry sources are not a list")
            записанный_источник = next(
                (
                    raw
                    for raw in raw_sources
                    if isinstance(raw, dict) and raw.get("id") == source_id
                ),
                None,
            )
            новое_записано = (
                записанный_источник is not None
                and записанный_источник.get("sha256") == new_sha256
                and записанный_источник.get("selection_version")
                == selection_version
            )

            if новое_записано:
                if not корень.exists() and временный.exists():
                    временный.rename(корень)
                if not корень.exists():
                    raise OSError("published root is missing")
                if отставленный is not None:
                    shutil.rmtree(отставленный, ignore_errors=True)
                shutil.rmtree(временный, ignore_errors=True)
            else:
                if (отставленный is None) != (записанный_источник is None):
                    raise ValueError("root history does not match registry")
                if отставленный is not None and отставленный.exists():
                    shutil.rmtree(корень, ignore_errors=True)
                    отставленный.rename(корень)
                elif отставленный is not None and not корень.exists():
                    raise OSError("previous root is missing")
                elif отставленный is None:
                    shutil.rmtree(корень, ignore_errors=True)
                shutil.rmtree(временный, ignore_errors=True)

            self._module_swap_path.unlink(missing_ok=True)
            self._module_swap_path.with_suffix(".tmp").unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return [
                "Незавершённую рокировку кода не удалось восстановить; "
                "источник кода не следует использовать до проверки каталога данных."
            ]
        return []

    def _заблокировать_источники_кода_после_ошибки_рокировки(self) -> None:
        """Не публиковать ни один code root, пока WAL не исправлен."""
        with self._lock:
            code_ids = {
                source_id
                for source_id, source in self.sources.items()
                if source.kind in (KIND_MODULES, KIND_EXTENSION)
            } | set(self.modules)
            for source_id in code_ids:
                self.sources.pop(source_id, None)
                self.modules.pop(source_id, None)
                self._modules_generation[source_id] = (
                    self._modules_generation.get(source_id, 0) + 1
                )

    def _rollback_code_swap(
        self,
        корень: Path,
        временный: Path,
        отставленный: Path | None,
    ) -> None:
        """Вернуть прежний canonical root после отказа записи реестра."""
        try:
            if корень.exists():
                корень.rename(временный)
            if отставленный is not None:
                отставленный.rename(корень)
            shutil.rmtree(временный, ignore_errors=True)
        except OSError as error:
            raise RegistryError(
                "Не удалось вернуть прежний каталог кода после отказа "
                "публикации; проверьте каталог данных вручную."
            ) from error

    @staticmethod
    def _построить_индекс_кода(
        метка: str,
        корень: Path,
        identity: LocatorIdentity | None = None,
        прогресс: Callable[[int, str, int, int], None] | None = None,
        файлы_каталога: tuple[Path, ...] | None = None,
    ) -> modules_index.Индексы:
        """Все четыре структуры провайдера `modules` по коду на `корень`.

        Общее тело для `add_modules`/`_add_extension` (`корень` — временный
        каталог, ДО рокировки) и для восстановления после рестарта (`корень`
        — уже финальный каталог, код туда положил ещё прошлый запуск).
        `метка` — только для текста ошибки: имя архива при живой загрузке,
        `source.origin` при восстановлении, где архива уже может не быть под
        рукой (`registry.json` хранит только имя, каким он назывался тогда).

        Любая ошибка сборки заворачивается в `RegistryError` с причиной —
        отказ сборки называет её вслух, а не
        оставляет молчаливое расхождение.
        """
        try:
            def этап(номер: int, название: str):
                if прогресс is None:
                    return None
                return lambda обработано, всего: прогресс(
                    номер, название, обработано, всего
                )

            p = этап(1, "оглавление")
            каталог = modules_index.build_catalog(
                корень,
                identity or LocatorIdentity("direct", "direct", 0),
                files=файлы_каталога,
                progress=p,
            )
            if прогресс is not None:
                прогресс(
                    1,
                    "оглавление",
                    0,
                    sum(
                        entry.locator is not None
                        for entry in каталог.entries.values()
                    ),
                )
            p = этап(1, "оглавление")
            оглавление = (
                modules_index.Оглавление.построить(
                    корень, каталог=каталог, прогресс=p
                )
                if p is not None
                else modules_index.Оглавление.построить(
                    корень, каталог=каталог
                )
            )
            p = этап(2, "вызовы")
            вызовы = (
                modules_index.Вызовы.построить(
                    корень, оглавление, каталог=каталог, прогресс=p
                )
                if p is not None
                else modules_index.Вызовы.построить(
                    корень, оглавление, каталог=каталог
                )
            )
            p = этап(3, "формы")
            формы = (
                modules_index.Формы.построить(
                    корень, каталог=каталог, прогресс=p
                )
                if p is not None
                else modules_index.Формы.построить(корень, каталог=каталог)
            )
            p = этап(4, "поиск")
            поиск = (
                modules_index.построить_поиск(
                    оглавление, корень, каталог=каталог, прогресс=p
                )
                if p is not None
                else modules_index.построить_поиск(
                    оглавление, корень, каталог=каталог
                )
            )
        except OSError as error:
            raise RegistryError(
                f"{метка}: индекс кода не построился — файл текущей "
                "выгрузки недоступен."
            ) from error
        except Exception as error:
            raise RegistryError(
                f"{метка}: индекс кода не построился — {error}"
            ) from error
        return modules_index.Индексы(
            оглавление=оглавление,
            вызовы=вызовы,
            формы=формы,
            поиск=поиск,
            каталог=каталог,
        )

    @staticmethod
    def _готовые_модули(
        source: Source, корень: Path, индексы: modules_index.Индексы
    ) -> LoadedModules:
        """Один готовый пакет — общий путь для кэша, сборки и живой загрузки."""
        всего = len(индексы.оглавление.модули)
        return LoadedModules(
            source=source,
            корень=корень,
            оглавление=индексы.оглавление,
            вызовы=индексы.вызовы,
            формы=индексы.формы,
            поиск=индексы.поиск,
            версия_кода=source.code_version,
            каталог=индексы.каталог,
            готов=True,
            прогресс=(всего, всего),
        )

    def _журналировать_ограничения_кода(
        self, source: Source, индексы: modules_index.Индексы
    ) -> None:
        """По одной обезличенной строке на категорию нового поколения."""
        catalog_counts = Counter(
            problem.category for problem in индексы.каталог.problems
        )
        form_counts = Counter(
            problem.категория for problem in индексы.формы.проблемы
        )
        counts = {
            category: max(catalog_counts[category], form_counts[category])
            for category in catalog_counts.keys() | form_counts.keys()
        }
        if индексы.каталог.coverage.compiled:
            counts["compiled_without_source"] = индексы.каталог.coverage.compiled

        for category, count in sorted(counts.items()):
            if count <= 0:
                continue
            key = (source.id, source.locator_generation, category)
            with self._lock:
                if key in self._logged_module_limitations:
                    continue
                self._logged_module_limitations.add(key)
            logger.warning(
                "Ограничение корпуса кода: категория=%s; количество=%d; "
                "поколение=%d.",
                category,
                count,
                source.locator_generation,
            )

    def _следующее_поколение_модулей(self, source_id: str) -> int:
        """Вызывается под `_lock`; возвращает новый монотонный номер."""
        поколение = self._modules_generation.get(source_id, 0) + 1
        self._modules_generation[source_id] = поколение
        return поколение

    def _зарезервировать_операцию_модулей(
        self, configuration: str, source_id: str
    ) -> _ModuleOperation:
        """CAS-token создаётся до extract/build, но без долгого lock."""
        with self._modules_cache_lock:
            with self._lock:
                конфигурация = self.configurations.get(configuration)
                if конфигурация is None:
                    raise RegistryError(
                        f"Конфигурация «{configuration}» не загружена."
                    )
                lifecycle_lock = self._module_operation_locks.setdefault(
                    source_id, threading.Lock()
                )
                прежний = self.sources.get(source_id)
                сохраненное = (
                    прежний.locator_generation
                    if прежний is not None
                    and type(прежний.locator_generation) is int
                    and прежний.locator_generation > 0
                    else 0
                )
                поколение_локаторов = max(
                    self._locator_generation.get(source_id, 0), сохраненное
                ) + 1
                self._locator_generation[source_id] = поколение_локаторов
                операция = _ModuleOperation(
                    source_id=source_id,
                    configuration=configuration,
                    configuration_source=конфигурация.source,
                    generation=self._следующее_поколение_модулей(source_id),
                    locator_generation=поколение_локаторов,
                    lifecycle_lock=lifecycle_lock,
                )
                self._module_operations[source_id] = операция
                return операция

    def _операция_модулей_актуальна(self, операция: _ModuleOperation) -> bool:
        """CAS по token, generation и identity привязанной конфигурации."""
        конфигурация = self.configurations.get(операция.configuration)
        return (
            self._module_operations.get(операция.source_id) is операция
            and self._modules_generation.get(операция.source_id)
            == операция.generation
            and конфигурация is not None
            and конфигурация.source is операция.configuration_source
        )

    def _завершить_операцию_модулей(self, операция: _ModuleOperation) -> None:
        with self._lock:
            if self._module_operations.get(операция.source_id) is операция:
                self._module_operations.pop(операция.source_id, None)

    @staticmethod
    def _отмена_операции_модулей(
        операция: _ModuleOperation, архив: Path
    ) -> _ModuleOperationCancelled:
        return _ModuleOperationCancelled(
            f"{архив.name}: разбор отменён — источник "
            f"{операция.source_id} или привязанная конфигурация "
            "уже изменены."
        )

    @contextmanager
    def _lifecycle_операции_модулей(
        self, операция: _ModuleOperation, архив: Path
    ):
        """Один цикл на источник; новая заявка отменяет старую в очереди."""
        with операция.lifecycle_lock:
            try:
                with self._lock:
                    if not self._операция_модулей_актуальна(операция):
                        raise self._отмена_операции_модулей(операция, архив)
                yield
            except BaseException as error:
                with self._lock:
                    устарела = not self._операция_модулей_актуальна(
                        операция
                    )
                if устарела and not isinstance(
                    error, _ModuleOperationCancelled
                ):
                    raise self._отмена_операции_модулей(операция, архив) from error
                raise
            finally:
                self._завершить_операцию_модулей(операция)

    def _опубликовать_операцию_модулей(
        self,
        операция: _ModuleOperation,
        архив: Path,
        корень: Path,
        временный: Path,
        снести: Callable[[Path], None],
        source: Source,
        loaded: LoadedModules,
        индексы: modules_index.Индексы,
    ) -> Source:
        """Единая CAS-публикация для конфигурации и расширения."""
        отставленный: Path | None = None
        временный_кэш: Path | None = None
        try:
            with self._modules_cache_lock:
                # Тяжёлое I/O кэша идёт до основного замка, но в отдельный
                # каталог. До успешных root + registry.json канонические
                # файлы кэша остаются прежними: отказ рокировки не оставляет
                # рядом индекс неопубликованного поколения.
                try:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    временный_кэш = Path(
                        tempfile.mkdtemp(
                            dir=self.cache_dir, prefix=".modules-cache.tmp-"
                        )
                    )
                except OSError:
                    # Кэш расходный: недоступный каталог не мешает принять
                    # источник, следующий старт просто построит индекс снова.
                    временный_кэш = None
                if временный_кэш is not None:
                    modules_index.сохранить_индексы(
                        self,
                        source.id,
                        индексы,
                        source_sha256=source.sha256,
                        selection_version=source.selection_version,
                        cache_dir=временный_кэш,
                    )
                with self._lock:
                    if not self._операция_модулей_актуальна(операция):
                        raise RegistryError(
                            f"{архив.name}: разбор отменён — источник "
                            "или привязанная конфигурация уже изменены."
                        )
                    прежний_source = self.sources.get(source.id)
                    прежние_modules = self.modules.get(source.id)
                    отставленный = self._начать_рокировку_кода(
                        source, корень, временный
                    )
                    try:
                        отставленный = self._swap_code(
                            архив, корень, временный
                        )
                    except BaseException:
                        # registry.json ещё прежний: recovery либо уже
                        # ничего не сделает после внутреннего rollback, либо
                        # вернёт .old-* на canonical path.
                        self._восстановить_рокировку_кода()
                        raise
                    self.sources[source.id] = source
                    self.modules[source.id] = loaded
                    try:
                        # JSON заменяется атомарно своим tmp-файлом. Пока он
                        # не записан, прежний root сохранён рядом и может быть
                        # возвращён без повторного extract/build.
                        self.save()
                    except BaseException:
                        if прежний_source is None:
                            self.sources.pop(source.id, None)
                        else:
                            self.sources[source.id] = прежний_source
                        if прежние_modules is None:
                            self.modules.pop(source.id, None)
                        else:
                            self.modules[source.id] = прежние_modules
                        self._rollback_code_swap(
                            корень, временный, отставленный
                        )
                        отставленный = None
                        try:
                            self._module_swap_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        raise
                if временный_кэш is not None:
                    try:
                        файлы_кэша = tuple(временный_кэш.iterdir())
                    except OSError:
                        файлы_кэша = ()
                    for файл in файлы_кэша:
                        if not файл.is_file():
                            continue
                        try:
                            файл.replace(self.cache_dir / файл.name)
                        except OSError:
                            # Уже опубликованный источник корректен без кэша.
                            continue
            if отставленный is not None:
                снести(отставленный)
            # Если unlink не удался, следующий startup увидит уже новое
            # поколение в registry.json и безопасно завершит уборку.
            try:
                self._module_swap_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._журналировать_ограничения_кода(source, индексы)
            return source
        finally:
            # До swap это убирает отменённый разбор; после swap пути
            # уже нет, и `ignore_errors` ничего не трогает.
            shutil.rmtree(временный, ignore_errors=True)
            if временный_кэш is not None:
                shutil.rmtree(временный_кэш, ignore_errors=True)

    def _выполнить_операцию_модулей(
        self,
        операция: _ModuleOperation,
        архив: Path,
        корень: Path,
        *,
        kind: str,
        версия_кода: str,
        снести: Callable[[Path], None],
        отпечаток_архива: tuple[int, int, int, int],
    ) -> Source:
        """Общий foreground lifecycle модулей и расширения."""
        from . import intake

        with self._lifecycle_операции_модулей(операция, архив):
            try:
                if not _архив_не_изменился(архив, отпечаток_архива):
                    raise RegistryError(
                        f"{архив.name}: архив изменился после выбора вида "
                        "выгрузки; повторите разбор после завершения копирования."
                    )
                нужно, _формат = intake.planned_size(
                    архив, existing=корень.exists()
                )
                хватает, свободно = intake.enough_space(нужно, self.data_dir)
            except RegistryError:
                raise
            except (OSError, zipfile.BadZipFile) as error:
                raise RegistryError(
                    f"{архив.name}: не удалось проверить архив и свободное "
                    "место. Проверьте доступность каталога данных и права: "
                    "процесс контейнера работает от uid 10001."
                ) from error
            if not _архив_не_изменился(архив, отпечаток_архива):
                raise RegistryError(
                    f"{архив.name}: архив изменился во время проверки места; "
                    "повторите разбор после завершения копирования."
                )
            if not хватает:
                mib = 1 << 20
                нужно_мб = (нужно + mib - 1) // mib
                свободно_мб = свободно // mib
                raise RegistryError(
                    f"{архив.name}: недостаточно свободного места: нужно "
                    f"{нужно_мб} МБ, свободно {свободно_мб} МБ."
                )
            # Проверка места сама обращается к файловой системе и может
            # задержаться. За это время remove/reparse способен инвалидировать
            # token: повторный короткий CAS не даёт устаревшей операции даже
            # начать распаковку после уже принятого отказа владельца.
            with self._lock:
                if not self._операция_модулей_актуальна(операция):
                    raise self._отмена_операции_модулей(операция, архив)
            try:
                временный, digest, файлов = self._extract_to_temp(архив, корень)
            except BaseException as error:
                файловая = _ошибка_файловой_системы(error)
                if файловая is None:
                    raise
                if файловая.errno == errno.ENOSPC:
                    raise RegistryError(
                        f"{архив.name}: свободное место закончилось во время "
                        "распаковки; прежний разбор сохранён. Освободите место "
                        "и повторите."
                    ) from error
                raise RegistryError(
                    f"{архив.name}: не удалось подготовить текущую выгрузку "
                    "кода; прежний разбор сохранён. Проверьте доступность "
                    "архива, каталога данных и права процесса."
                ) from error
            try:
                if not _архив_не_изменился(архив, отпечаток_архива):
                    raise RegistryError(
                        f"{архив.name}: архив изменился во время распаковки; "
                        "разбор отменён."
                    )
                индексы = self._построить_индекс_кода(
                    архив.name,
                    временный,
                    LocatorIdentity(
                        операция.source_id,
                        digest,
                        операция.locator_generation,
                    ),
                )
            except BaseException as error:
                shutil.rmtree(временный, ignore_errors=True)
                if isinstance(error, RegistryError):
                    raise
                if _ошибка_файловой_системы(error) is not None:
                    raise RegistryError(
                        f"{архив.name}: не удалось построить индекс текущей "
                        "выгрузки кода; прежний разбор сохранён. Проверьте "
                        "доступность каталога данных и права процесса."
                    ) from error
                raise

            source = Source(
                id=операция.source_id,
                kind=kind,
                origin=архив.name,
                sha256=digest,
                loaded_at=_now(),
                platform=операция.configuration_source.platform,
                status=STATUS_READY,
                items_total=файлов,
                stored_path=self._relative(корень),
                selection_version=intake.SELECTION_VERSION,
                locator_generation=операция.locator_generation,
                code_version=версия_кода,
            )
            loaded = LoadedModules(
                source=source,
                корень=корень,
                оглавление=индексы.оглавление,
                вызовы=индексы.вызовы,
                формы=индексы.формы,
                поиск=индексы.поиск,
                версия_кода=версия_кода,
                каталог=индексы.каталог,
            )
            try:
                return self._опубликовать_операцию_модулей(
                    операция,
                    архив,
                    корень,
                    временный,
                    снести,
                    source,
                    loaded,
                    индексы,
                )
            except BaseException as error:
                if isinstance(error, RegistryError):
                    raise
                файловая = _ошибка_файловой_системы(error)
                if файловая is None:
                    raise
                if файловая.errno == errno.ENOSPC:
                    текст = (
                        f"{архив.name}: свободное место закончилось во время "
                        "публикации кода; прежний разбор сохранён. Освободите "
                        "место и повторите."
                    )
                else:
                    текст = (
                        f"{архив.name}: не удалось опубликовать текущую "
                        "выгрузку кода; прежний разбор сохранён. Проверьте "
                        "доступность каталога данных и права процесса."
                    )
                raise RegistryError(текст) from error

    def _поколение_актуально(
        self,
        source: Source,
        поколение: int,
        loaded: LoadedModules | None = None,
    ) -> bool:
        """CAS-предикат; вызывается под `_lock`."""
        if (
            self.sources.get(source.id) is not source
            or self._modules_generation.get(source.id) != поколение
        ):
            return False
        return loaded is None or self.modules.get(source.id) is loaded

    def _собрать_индексы_фоном(
        self,
        source: Source,
        корень: Path,
        строится: LoadedModules,
        поколение: int,
        файлы_каталога: tuple[Path, ...],
    ) -> None:
        """Собирает четыре структуры и публикует их только единым пакетом."""
        source_id = source.id
        опубликовано = False

        def отметить(
            номер: int, название: str, обработано: int, всего: int
        ) -> None:
            with self._lock:
                if self._поколение_актуально(source, поколение, строится):
                    строится.этап = (номер, 4)
                    строится.название_этапа = название
                    строится.прогресс = (обработано, всего)

        try:
            индексы = self._построить_индекс_кода(
                source.origin,
                корень,
                LocatorIdentity(
                    source.id, source.sha256, source.locator_generation
                ),
                отметить,
                файлы_каталога,
            )
            готовые = self._готовые_модули(source, корень, индексы)
            # Writer и смена поколения сериализованы одним mutex. CAS
            # проверяется внутри него до записи, поэтому после проверки новый
            # reparse/remove не может вклиниться и сменить Source под ногами.
            with self._modules_cache_lock:
                with self._lock:
                    if not self._поколение_актуально(
                        source, поколение, строится
                    ):
                        return
                modules_index.сохранить_индексы(
                    self,
                    source_id,
                    индексы,
                    source_sha256=source.sha256,
                    selection_version=source.selection_version,
                )
                with self._lock:
                    if self._поколение_актуально(source, поколение, строится):
                        source.status = STATUS_READY
                        source.error = ""
                        self.modules[source_id] = готовые
                        опубликовано = True
            if опубликовано:
                self._журналировать_ограничения_кода(source, индексы)
        except Exception as error:
            with self._lock:
                if self._поколение_актуально(source, поколение, строится):
                    source.status = STATUS_ERROR
                    source.error = str(error)
        finally:
            with self._lock:
                if self._module_builds.get(source_id) is threading.current_thread():
                    self._module_builds.pop(source_id, None)
            # Статус готовности/ошибки переживает следующий restart. Пока
            # `startup()` восстанавливает строки registry.json по очереди,
            # его снимок в памяти заведомо неполон: быстрый фоновый поток не
            # должен успеть заменить файл таким промежуточным списком и
            # потерять ещё не обработанный источник при аварийной остановке.
            # Порядок замков совпадает со startup: startup -> registry;
            # `_lock` выше уже отпущен, поэтому взаимного ожидания нет.
            self._сохранить_результат_фоновой_сборки()

    def _сохранить_результат_фоновой_сборки(self) -> bool:
        """Записать статус, только если последний startup завершился целиком."""
        with self._startup_lock:
            if (
                self._last_successful_startup_generation
                != self._startup_generation
            ):
                return False
            self.save()
            return True

    def _поднять_или_построить_модули(
        self,
        source: Source,
        корень: Path,
        *,
        configuration: str,
        expected_configuration_source: Source,
        expected_generation: int,
    ) -> None:
        """Восстановить индекс кода из строки `source`.

        Только для `restore()`: код к этому моменту уже лежит на `корень` —
        никакой рокировки тут нет, `add_modules`/`_add_extension` её уже
        сделали до остановки процесса. Сначала `поднять_индексы` из кэша
        проекта — на живой выгрузке 0,46 с против ~50 с полной
        сборки; не сошлось (штамп разошёлся после коммита, кэш побит, том
        только на чтение) — строим прямо по `корень` и складываем в кэш тем
        же приёмом, что и живая загрузка.

        Исключение из сборки не перехватывается здесь — источник остаётся в
        `self.sources` (код на диске цел, просто не проиндексирован), а
        `restore()` заносит причину в список проблем и продолжает поднимать
        остальные источники, как и для всех прочих родов.
        """
        with self._lock:
            владелец = self.configurations.get(configuration)
            if (
                владелец is None
                or владелец.source is not expected_configuration_source
                or source.id in self._module_operations
                or self._modules_generation.get(source.id, 0)
                != expected_generation
            ):
                raise RegistryError(
                    "восстановление кода отменено — "
                    f"конфигурация «{configuration}» снята, "
                    "заменена или её код уже переразбирается."
                )
            прежний_поток = self._module_builds.get(source.id)
            прежние = self.modules.get(source.id)
            if прежний_поток is not None and прежний_поток.is_alive() and прежние:
                # Текущая сборка владеет своим Source. Подмена его
                # объектом из JSON обесценила бы её CAS-публикацию.
                self.sources[source.id] = прежние.source
                return
            if self.sources.get(source.id) is not None and прежние is not None:
                # Живой reparse мог успеть завершиться до второго
                # прохода restore. Его Source/индекс уже опубликованы;
                # старая строка registry не имеет права их откатывать.
                return
            self.sources[source.id] = source
            поколение = self._следующее_поколение_модулей(source.id)
            if source.locator_generation <= 0:
                # Старые registry.json не знали стабильного поколения. Их
                # прежний кэш всё равно не содержит каталог и будет промахом;
                # новое значение сохранится после фоновой пересборки.
                source.locator_generation = поколение
            self._locator_generation[source.id] = max(
                self._locator_generation.get(source.id, 0),
                source.locator_generation,
            )

        индексы = modules_index.поднять_индексы(
            self,
            source.id,
            source_sha256=source.sha256,
            selection_version=source.selection_version,
        )
        if индексы is not None:
            опубликовано = False
            with self._lock:
                if not self._поколение_актуально(source, поколение):
                    return
                source.status = STATUS_READY
                source.error = ""
                self.modules[source.id] = self._готовые_модули(
                    source, корень, индексы
                )
                опубликовано = True
            if опубликовано:
                self._журналировать_ограничения_кода(source, индексы)
            return

        файлы_каталога = modules_index.catalog_files(корень)
        строится = LoadedModules(
            source=source,
            корень=корень,
            оглавление=None,
            вызовы=None,
            формы=None,
            поиск=None,
            версия_кода=source.code_version,
            готов=False,
            прогресс=(0, len(файлы_каталога)),
            этап=(1, 4),
            название_этапа="оглавление",
        )
        source.status = STATUS_LOADING
        source.error = ""
        поток = threading.Thread(
            target=self._собрать_индексы_фоном,
            args=(source, корень, строится, поколение, файлы_каталога),
            daemon=True,
            name=f"modules:{source.id}",
        )
        # Generation снимается до первой долгой загрузки. Если
        # живой reparse начнётся и даже завершится до второго
        # прохода, монотонный номер всё равно выдаст старую строку.
        with self._lock:
            if not self._поколение_актуально(source, поколение):
                return
            self.modules[source.id] = строится
            self._module_builds[source.id] = поток
        поток.start()

    @staticmethod
    def _владелец_источника_кода(source: Source) -> str | None:
        """Имя конфигурации из стабильной схемы id источника кода."""
        if source.kind == KIND_MODULES:
            суффикс = ":modules"
            if source.id.endswith(суффикс):
                return source.id[: -len(суффикс)] or None
            return None
        if source.kind == KIND_EXTENSION:
            configuration, разделитель, extension = source.id.partition(":ext:")
            if разделитель and configuration and extension:
                return configuration
        return None

    def add_modules(self, path: str | Path, *, configuration: str) -> Source:
        """Выгрузка конфигурации в файлы: код на диск, учётная запись в реестр.

        Выгрузка расширения распознаётся по `Configuration.xml`
        (`_сведения_о_выгрузке`) и уходит в `_add_extension`: другой ключ
        (`:ext:<Имя>` вместо `:modules`), другой каталог, другой вид
        источника. Публичная сигнатура остаётся прежней — на неё опираются
        страница, тесты и `restore()`.

        Ключ источника модулей — не имя конфигурации: под ним уже лежат
        метаданные, и присвоение по тому же ключу вытеснило бы их из
        `self.sources`, а `save()` записал бы реестр уже без них.
        """
        архив = Path(path)
        if configuration not in self.configurations:
            raise RegistryError(
                f"{архив.name}: конфигурация «{configuration}» не загружена."
            )
        try:
            отпечаток_архива = _отпечаток_архива(архив)
        except OSError as error:
            raise RegistryError(
                f"{архив.name}: архив недоступен; проверьте файл и повторите."
            ) from error

        # Годность архива выясняется ПЕРВЫМ делом — до распознавания вида и
        # до удаления чего-либо. Выгрузка метаданных (`СтруктураКонфигурации_
        # *.zip`) — тоже .zip без единого модуля или формы; ровно на эту
        # ошибку человека рассчитан текст отказа `_нет_модулей`, и вид
        # выгрузки для него не важен вовсе. Проверка после очистки означала
        # бы, что ошибочное нажатие сносит уже разобранные 351 МБ кода, а
        # взять их заново неоткуда, если гигабайтный архив из `incoming/` уже
        # убран. Считаем по центральному каталогу zip: тело архива не
        # читается.
        try:
            if not _отбираемых_членов(архив):
                raise _нет_модулей(архив)
        except RegistryError:
            raise
        except zipfile.BadZipFile as error:
            raise RegistryError(
                f"{архив.name}: архив ZIP повреждён или его центральный "
                "каталог не читается."
            ) from error
        except OSError as error:
            raise RegistryError(
                f"{архив.name}: архив недоступен во время предпроверки; "
                "проверьте файл и права процесса."
            ) from error

        # Распознавание — только теперь, когда известно, что в архиве есть
        # что брать: `_сведения_о_выгрузке` сама отказывает («Configuration.xml
        # не найден», «признаки неполные»), когда решить нельзя однозначно —
        # тогда молчаливый переход в ветку модулей стоил бы кода конфигурации.
        # Архив без единого модуля/формы (например, выгрузка структуры
        # метаданных, у которой Configuration.xml и вовсе не бывает) уже
        # отсечён проверкой выше — до него это распознавание не доходит, и
        # текст отказа для него остаётся прежним, специфичным.
        это_расширение, имя_расширения, версия_кода = _сведения_о_выгрузке(архив)
        if это_расширение:
            if not имя_расширения:
                raise RegistryError(
                    f"{архив.name}: похоже на выгрузку расширения, но тег "
                    "Name в Configuration.xml пуст — имя расширения взять "
                    "неоткуда."
                )
            return self._add_extension(
                архив,
                configuration=configuration,
                extension=имя_расширения,
                версия_кода=версия_кода,
                отпечаток_архива=отпечаток_архива,
            )

        корень = self._modules_root(configuration)
        операция = self._зарезервировать_операцию_модулей(
            configuration, f"{configuration}:modules"
        )
        return self._выполнить_операцию_модулей(
            операция,
            архив,
            корень,
            kind=KIND_MODULES,
            версия_кода=версия_кода,
            снести=self._drop_modules_root,
            отпечаток_архива=отпечаток_архива,
        )

    def _add_extension(
        self,
        архив: Path,
        *,
        configuration: str,
        extension: str,
        версия_кода: str = "",
        отпечаток_архива: tuple[int, int, int, int],
    ) -> Source:
        """Выгрузка расширения: код в свой каталог, источник `:ext:<Имя>`.

        Ключ и каталог держат расширение отдельно и от модулей конфигурации,
        и от других расширений той же конфигурации (`_extension_root`).
        Личность расширения задаёт тег `Name` внутри его выгрузки, а не имя
        файла архива: повторный разбор того же расширения под другим именем
        файла переиспользует тот же ключ, тот же каталог, тот же источник —
        `origin` просто обновляется на имя последнего разобранного файла.
        Конфигурацию, к которой расширение принадлежит, называет человек;
        сколько расширений у одной конфигурации — не ограничено, в отличие
        от модулей, которых на конфигурацию ровно одна выгрузка.

        Ключ строится из ТОГО ЖЕ очищенного имени, что и каталог
        (`index_cache.safe_name`), а не из сырого тега `Name`: иначе `a/b` и
        `a:b` дали бы разные ключи при одном и том же каталоге `a_b` —
        второй разбор тихо переписывал бы файлы первого, а оба источника
        остались бы в реестре и врали бы счётчиком. У обычных имён (буквы,
        цифры, дефис, подчёркивание, точка, пробел) чистка ничего не меняет,
        так что для них это не видно. Обратная сторона того же решения:
        два РАЗНЫХ расширения, чьи имена очищаются в одно и то же значение
        (например, `a/b` и `a:b`), схлопнутся в один источник — принято
        осознанно, см. README.
        """
        имя_чисто = index_cache.safe_name(extension)
        корень = self._extension_root(configuration, extension)
        source_id = f"{configuration}:ext:{имя_чисто}"
        операция = self._зарезервировать_операцию_модулей(
            configuration, source_id
        )

        return self._выполнить_операцию_модулей(
            операция,
            архив,
            корень,
            kind=KIND_EXTENSION,
            версия_кода=версия_кода,
            снести=self._drop_extension_root,
            отпечаток_архива=отпечаток_архива,
        )

    def add_syntax(
        self,
        path: str | Path,
        *,
        platform: str = "",
        keep_source: bool = True,
        known_sha256: str = "",
        rebuild: bool = True,
        known_kind: str = "",
    ) -> Source:
        """Принять справку платформы или языка запросов — по содержимому.

        `known_kind` — вид, поднятый из `registry.json` при восстановлении:
        распознавать его заново не нужно (и вредно — см. `restore()`), он уже
        известен из прошлой загрузки.
        """
        source_path = Path(path)
        digest = known_sha256 or _sha256(source_path)
        platform = platform or _platform_from_path(source_path)

        # Вид определяется до разбора и до проверки версии: у языка запросов
        # версии в данных нет вовсе, и проверка `not platform` отвергла бы
        # его раньше, чем выяснится, что версия ему и не нужна.
        is_index = source_path.suffix == ".gz"
        if is_index:
            syntax = load_syntax(source_path)
            if known_kind:
                is_query = known_kind == KIND_QUERY
            else:
                is_query = len(syntax) > 0 and all(
                    item.kind.startswith("query_") for item in syntax.items.values()
                )
            platform = "" if is_query else (platform or syntax.max_platform)
        else:
            if known_kind:
                is_query = known_kind == KIND_QUERY
            else:
                is_query = _is_query_hbk(source_path)
            if is_query:
                syntax = parse_query_hbk(source_path)
                platform = ""
            else:
                syntax = parse_hbk(source_path, platform=platform)
                if not platform:
                    platform = syntax.derived_platform()
                    if platform:
                        syntax.platforms = [platform]

        # Без версии справку платформы принимать нельзя: она задаёт границы
        # применимости всему набору, а пустая граница означает «элемент
        # актуален» — противоположное правде. Вывести версию из данных
        # удаётся не всегда: в справке 8.3.5 отметок «начиная с версии» нет
        # ни на одной из 18 936 страниц, они появились позже. Языка запросов
        # это не касается — версии в нём нет по устройству формата.
        if not is_query and not platform:
            raise RegistryError(
                f"{source_path.name}: не удалось определить версию платформы. "
                "В справках старых платформ версии внутри нет — укажите её при "
                "загрузке или назовите файл так, чтобы версия была в имени: "
                "`syntax-8.3.5.1570.hbk`."
            )

        # Разбор прошёл, элементов ноль. Это контейнер 1С, но не справка
        # синтакс-помощника: так ведёт себя `config_ru.hbk` и прочие справки
        # интерфейса из того же каталога — сигнатура контейнера на месте,
        # `FileStorage` есть, страниц синтакс-помощника внутри нет.
        # Отвергаем до того, как файл скопирован в `data/sources` и записан в
        # реестр: пустая справка, вставшая на место рабочей, ломает поиск
        # молча — в списке источников всё выглядит целым.
        if not len(syntax):
            raise RegistryError(
                f"{source_path.name}: разбор не дал ни одного элемента — это не "
                "справка синтакс-помощника. Нужен `shcntx_ru.hbk` из каталога "
                "установки платформы, он весит десятки МБ; остальные `.hbk` "
                "оттуда — справки интерфейса, они не подходят."
            )

        if is_query:
            # Языконезависимый вариант (`shquery_root.hbk`) устроен структурно
            # так же, как `shquery_ru.hbk`: те же `__categories__` и страницы
            # секций, `looks_like_query_help` их не различает. Отличие — в
            # текстах страниц, тем же приёмом, что и для `shcntx_root.hbk`
            # ниже.
            if not any(item.description for item in syntax.items.values()):
                raise RegistryError(
                    f"{source_path.name}: ни у одной из {len(syntax)} страниц нет "
                    "текста. Так выглядит языконезависимый вариант "
                    "(`shquery_root.hbk`) — в нём только дерево страниц. Нужен "
                    "`shquery_ru.hbk` из каталога установки."
                )
        else:
            # Элементы есть, но ни у одного нет описания — это `shcntx_root.hbk`:
            # языконезависимая часть справки. Дерево страниц и английские
            # идентификаторы там те же (25 508 элементов против 25 511 у
            # `shcntx_ru.hbk`), поэтому проверка на пустоту его пропускает. А
            # искать по нему нечего: ни описаний, ни русских имён, ни версий
            # появления — значит и фильтр по версии платформы не работает.
            if not any(item.description for item in syntax.items.values()):
                raise RegistryError(
                    f"{source_path.name}: ни у одного из {len(syntax)} элементов нет "
                    "описания. Так выглядят языконезависимая часть справки "
                    "(`shcntx_root.hbk` — дерево страниц и английские "
                    "идентификаторы без текстов) и соседние справки платформы "
                    "(`shlang_ru.hbk` и подобные — другая разметка). "
                    "Нужен `shcntx_ru.hbk` из каталога установки."
                )

        if is_index:
            index_path = source_path
        else:
            # Исходный `.hbk` не сохраняем. Восстановление читает разобранный
            # индекс — `stored_path` у справки указывает именно на него, — а
            # исходник не открывался ни разу за всю жизнь реестра. Копия стоила
            # 39 МБ на каждую загруженную справку и не давала ничего: команды
            # «переразобрать из сохранённого» нет, а повторная загрузка того же
            # файла отсекается по хешу. Понадобится другой разбор — файл берут
            # из каталога установки платформы, там он и лежит.
            имя = "query-ru" if is_query else (platform or source_path.stem)
            index_path = self.index_dir / "syntax" / f"{имя}.json.gz"
            save_syntax(syntax, index_path)

        source = Source(
            id="syntax-query" if is_query else f"syntax-{platform or source_path.stem}",
            kind=KIND_QUERY if is_query else KIND_SYNTAX,
            origin=source_path.name,
            sha256=digest,
            loaded_at=_now(),
            platform=platform,
            status=STATUS_READY,
            items_total=len(syntax),
            stored_path=self._relative(index_path),
        )

        if is_query:
            # Разбор страниц справки. Испорченная разметка одну страницу
            # обедняет, а справку в целом оставляет рабочей — поэтому не отказ,
            # а имя страницы вслух. Молчание здесь неотличимо от справки, в
            # которой этого просто нет.
            source.warnings.extend(syntax.warnings)

            # Поисковые ключи привязываются к страницам по идентификатору, а он
            # приходит из имени файла внутри `.hbk`. Справка другой сборки может
            # дать другой набор страниц — тогда часть ключей повиснет, и поиск
            # молча просядет до состояния «как без ключей». Расхождение говорим
            # вслух здесь, при загрузке: молчащая деградация дороже отказа.
            предупреждение = key_coverage(syntax.items.keys()).as_warning()
            if предупреждение:
                source.warnings.append(предупреждение)

            # Экземпляр один на сервер: повторная загрузка заменяет прежний.
            # В `syntax_versions` источник не попадает — у него нет версии, а
            # `merge_syntax` на индексе без версии падает `ValueError`, и
            # `syntax_coverage()` держал бы его вечно «лишней» справкой. В
            # `sources` он обязан остаться — иначе `sweep_syntax` сочтёт его
            # разобранный индекс ничьим и снесёт при следующем старте.
            with self._lock:
                self.sources[source.id] = source
                self.query_source = source
                snapshot = dict(self.syntax_versions)
            if rebuild:
                # Без пересборки язык запросов ляжет в `sources`, но не
                # попадёт в поиск и таблицу имён до перезапуска — `syntax`
                # собирается заново только здесь и в `remove()`.
                self._apply_syntax(snapshot, preloaded_query=syntax)
            return source

        with self._lock:
            # Справки разных версий стоят рядом: та же версия — это исправление
            # и заменяет прежнюю запись, другая версия дополняет набор.
            self.syntax_versions[source.id] = source
            self.sources[source.id] = source
            self._relation_cache.clear()
            snapshot = dict(self.syntax_versions)

        if rebuild:
            # Разобранная справка уже в руках — заново с диска её не читаем.
            self._apply_syntax(snapshot, {source.id: syntax})
        return source

    @staticmethod
    def _fingerprint(
        versions: dict[str, "Source"], query_source: "Source | None" = None
    ) -> tuple:
        """Отпечаток набора источников, решающий, нужна ли пересборка `syntax`.

        Язык запросов в `merge_syntax` не участвует, но подмешивается в
        поисковый индекс и таблицу имён — без его id/sha256 здесь добавление
        или удаление источника языка запросов не меняло бы отпечаток, и
        `_apply_syntax` решил бы, что пересобирать нечего, подняв прежний
        индекс без новых элементов. Тот же класс дефекта, что и с кэшем на
        диске: код изменился, а выдача — нет.
        """
        return (
            tuple(sorted((sid, source.sha256) for sid, source in versions.items())),
            (query_source.id, query_source.sha256) if query_source is not None else None,
        )

    def _prepare_syntax(
        self,
        versions: dict[str, "Source"],
        preloaded: dict[str, SyntaxIndex] | None = None,
        query_source: "Source | None" = None,
        preloaded_query: SyntaxIndex | None = None,
    ) -> tuple[LoadedSyntax | None, list[str]]:
        """Собрать слитый вид. Дорогая часть: слияние, поисковый индекс, кэш.

        Слияние пяти справок — 0,13 с, но построение поискового индекса поверх
        него — около секунды. Поэтому вызывается **вне замка**: пока идёт
        сборка, инструменты отвечают по прежнему слитому виду.

        Возвращает ещё и список поломок: разобранная справка читается с диска,
        а файл может пропасть или побиться. Одна такая справка не должна
        ронять сборку остальных — это то же правило, по которому отдельный
        источник не роняет запуск.
        """
        if not versions and query_source is None:
            return None, []

        preloaded = preloaded or {}
        по_версии = sorted(
            versions.values(), key=lambda source: parse_version(source.platform)
        )
        problems: list[str] = []

        # Готовый слитый вид снимает нужду читать справки версий вовсе — это и
        # есть смысл кэша: на трёх справках старт без него удваивался.
        merged = self._cached_merged(по_версии) if len(по_версии) > 1 else None
        живые = по_версии

        if merged is None:

            def поднять(source: Source) -> SyntaxIndex | None:
                index = preloaded.get(source.id)
                if index is not None:
                    return index
                try:
                    return load_syntax(self._absolute(source.stored_path))
                except Exception as error:
                    problems.append(
                        f"{source.id}: разобранная справка не читается — {error}"
                    )
                    return None

            # Сливаем от свежих к старым и поднимаем справку прямо на её шаге:
            # так в памяти живёт одна разобранная справка плюс накопитель. При
            # загрузке всех разом пик был 493 МБ против 285.
            живые = []
            for source in reversed(по_версии):
                index = поднять(source)
                if index is None:
                    continue
                живые.append(source)
                merged = index if merged is None else merge_syntax([index, merged])
            живые.reverse()

            # Раньше здесь был ранний `return None, problems`: без справок
            # платформы собирать было нечего. Теперь есть второй источник —
            # язык запросов, у него своих версий нет, и отсутствие справок
            # платформы не должно мешать поднять хотя бы его.
            if merged is not None and len(живые) > 1:
                self._save_merged(merged, живые)

        # Язык запросов — не версия справки: `merge_syntax` в этом не
        # участвует (см. `LoadedSyntax`). Поднимается тем же приёмом, что и
        # справки версий — с диска, если не передан уже разобранным.
        query = preloaded_query
        if query is None and query_source is not None:
            try:
                query = load_syntax(self._absolute(query_source.stored_path))
            except Exception as error:
                problems.append(
                    f"{query_source.id}: язык запросов не читается — {error}"
                )
                query = None

        if merged is None and query is None:
            return None, problems

        # Поиск и таблица имён строятся по обоим наборам сразу: агент задаёт
        # один вопрос и не должен выбирать, в каком источнике искать. Слитый
        # вид (`merged`) в это соединение не идёт — он остаётся только
        # справкой платформы и уходит в `_save_merged` без языка запросов.
        для_поиска = merged
        if query is not None:
            для_поиска = SyntaxIndex(
                platforms=list(merged.platforms) if merged else [],
                items={**(merged.items if merged else {}), **query.items},
                source=(merged.source if merged else query.source),
                language=(merged.language if merged else query.language),
            )

        # Без загруженной справки платформы «самая свежая» — это сам источник
        # языка запросов: он тоже должен на что-то отвечать в `LoadedSyntax`
        # даже без справки платформы, поэтому индекс собирается уже здесь.
        newest = живые[-1] if живые else query_source

        # Штамп кэша — по всему набору источников, включая язык запросов: он
        # не входит в слияние, но входит в поисковый индекс, и без его
        # sha256 здесь добавление или удаление языка запросов подняло бы
        # прежний кэш индекса — имя файла кэша от этого не меняется.
        отпечаток = _combined_sha256(живые + ([query_source] if query_source else []))
        stamp = replace(newest, sha256=отпечаток)
        loaded = LoadedSyntax(
            source=newest,
            # Пустой `SyntaxIndex()`, если справок платформы нет вовсе —
            # поле типизировано без `None`, а «нет платформы» и «загружена
            # пустая» тут неразличимы и не нужны разными.
            syntax=merged if merged is not None else SyntaxIndex(language="ru"),
            index=self._syntax_index(для_поиска, stamp),
            by_name=self._syntax_lookup(для_поиска, stamp),
            query=query,
            tables=build_table_index(merged),
        )
        return loaded, problems

    def _merged_path(self, sources: list["Source"]) -> Path:
        """Имя файла слитого вида — по набору справок и по отпечатку кода.

        Отпечаток кода обязателен: слияние правится часто, и кэш, переживший
        правку, тихо отдавал бы результат прежней логики. Тем же штампом
        пользуются остальные производные (`index_cache`), и по той же причине.
        """
        отпечаток = f"{_combined_sha256(sources)}:{index_cache._code_digest()}"
        имя = hashlib.sha256(отпечаток.encode()).hexdigest()[:16]
        return self._syntax_index_dir() / f"merged-{имя}.json.gz"

    def _cached_merged(self, sources: list["Source"]) -> SyntaxIndex | None:
        """Готовый слитый вид с диска. `None` — кэша нет или он не читается."""
        path = self._merged_path(sources)
        if not path.exists():
            return None
        try:
            return load_syntax(path)
        except Exception:
            # Кэш расходный: не прочитался — соберём заново.
            path.unlink(missing_ok=True)
            return None

    def _save_merged(self, merged: SyntaxIndex, sources: list["Source"]) -> None:
        path = self._merged_path(sources)
        try:
            save_syntax(merged, path)
        except OSError:
            # Том только на чтение — работать это не мешает.
            return
        # Прежние слитые виды больше не нужны: набор справок или код изменились,
        # и вернуться к ним нельзя. Копить их — растить каталог молча.
        for прежний in self._syntax_index_dir().glob("merged-*.json.gz"):
            if прежний != path:
                прежний.unlink(missing_ok=True)

    def _apply_syntax(
        self,
        versions: dict[str, "Source"],
        preloaded: dict[str, SyntaxIndex] | None = None,
        preloaded_query: SyntaxIndex | None = None,
    ) -> list[str]:
        """Собрать слитый вид вне замка и подменить ссылку под ним.

        Если за время сборки набор справок изменился — собираем заново по
        новому набору: наружу не должен попасть вид, не соответствующий
        списку источников. Источник языка запросов снят с `self` тем же
        приёмом, что и справки версий: без него добавление или удаление языка
        запросов, случившееся, пока эта сборка уже шла, было бы потеряно.
        """
        with self._lock:
            query_source = self.query_source
        while True:
            prepared, problems = self._prepare_syntax(
                versions, preloaded, query_source, preloaded_query
            )
            with self._lock:
                if self._fingerprint(
                    self.syntax_versions, self.query_source
                ) == self._fingerprint(versions, query_source):
                    self.syntax = prepared
                    self._relation_cache.clear()
                    for problem in problems:
                        source_id = problem.split(":", 1)[0]
                        source = self.sources.get(source_id)
                        if source is not None:
                            source.status = STATUS_ERROR
                            source.error = problem
                    return problems
                versions = dict(self.syntax_versions)
                query_source = self.query_source
                # Разошедшийся снимок больше не годится: набор источников
                # сменился, а разобранный текст был поднят под старый.
                preloaded_query = None

    def remove(self, source_id: str) -> None:
        """Снять источник — а для конфигурации ещё и её код.

        `KIND_CONFIGURATION` без каскада выходил бы раньше, чем дошёл бы до
        `<Имя>:modules` и `<Имя>:ext:*`: индекс кода остался бы в памяти без
        метаданных, на которые ссылается, а на диске — 351 МБ (меньше у
        расширения), вернуть которые через интерфейс уже нечем (см.
        `docs/modules-provider-design.md`, раздел 9). Явный отказ
        «сначала снимите модули» вместо каскада заставлял бы человека делать
        руками то, что реестр обязан гарантировать сам: конфигурация без
        своего кода в реестре не имеет смысла, а не снятый код — это забытый
        каталог, который никто больше не найдёт (`orphan_sources` смотрит
        только `sources_dir`, не `modules_dir`/`extensions_dir`).
        """
        # Каталоги кода сносятся ПОСЛЕ выхода из-под замка (тысячи файлов, а
        # тот же замок берут `resolve()` и все инструменты MCP), справка —
        # тоже (сборка слитого вида недёшева). Собираем всё, что предстоит
        # сделать вне замка, пока сам замок ещё держим.
        отложенные_сносы: list[tuple[Callable[[Path], None], Path]] = []
        отложенная_справка: dict[str, Source] | None = None

        # Тот же порядок, что у writers: cache -> registry. После CAS старый
        # writer не может записать файл между `_drop_cache` и снятием Source.
        with self._modules_cache_lock, self._lock:
            source = self.sources.get(source_id)
            if source is None:
                raise RegistryError(f"Источник не зарегистрирован: {source_id}")

            # Цепочка считается ДО первого удаления: `self.sources` меняется
            # по ходу цикла ниже, и вычислять её позже значило бы гоняться за
            # движущейся целью. Только конфигурация каскадирует — модули и
            # расширения снимаются каждый сам по себе, без цепочки.
            цепочка = [source_id]
            if source.kind == KIND_CONFIGURATION:
                модули_id = f"{source_id}:modules"
                префикс_расширений = f"{source_id}:ext:"
                цепочка += sorted(
                    sid
                    for sid in self.sources
                    if sid == модули_id or sid.startswith(префикс_расширений)
                )

            # Canonical root отставляется ДО изменения реестра. Это
            # быстрый rename под тем же `cache -> registry`, а не rmtree.
            # Если один из rename не удался, все уже отставленные каталоги
            # возвращаются, а Source/индексы остаются нетронутыми.
            отставленные: list[
                tuple[Callable[[Path], None], Path, Path]
            ] = []
            try:
                for sid in цепочка:
                    текущий = self.sources.get(sid)
                    if текущий is None or текущий.kind not in (
                        KIND_MODULES,
                        KIND_EXTENSION,
                    ):
                        continue
                    канонический = (
                        self._absolute(текущий.stored_path)
                        if текущий.stored_path
                        else None
                    )
                    if канонический is None:
                        continue
                    снести = (
                        self._drop_modules_root
                        if текущий.kind == KIND_MODULES
                        else self._drop_extension_root
                    )
                    отставленный = self._retire_code_root(
                        канонический, текущий.kind
                    )
                    if отставленный is not None:
                        отставленные.append(
                            (снести, отставленный, канонический)
                        )
            except BaseException as error:
                ошибки_отката: list[str] = []
                for _, отставленный, канонический in reversed(отставленные):
                    try:
                        отставленный.rename(канонический)
                    except OSError as ошибка_возврата:
                        ошибки_отката.append(
                            f"{отставленный} -> {канонический}: {ошибка_возврата}"
                        )
                if ошибки_отката:
                    raise RegistryError(
                        "Каталоги кода не сняты, и откат отставленных "
                        f"каталогов не удался: {'; '.join(ошибки_отката)}"
                    ) from error
                raise
            отложенные_сносы.extend(
                (снести, отставленный)
                for снести, отставленный, _ in отставленные
            )

            # Foreground reparse может ещё не иметь Source: token уже
            # зарезервирован, а extract/build идут вне замка. Каскад
            # конфигурации обязан увидеть и такую операцию.
            инвалидировать = {
                sid
                for sid, операция in self._module_operations.items()
                if sid == source_id
                or (
                    source.kind == KIND_CONFIGURATION
                    and операция.configuration == source_id
                )
            }

            for sid in цепочка:
                текущий = self.sources.pop(sid, None)
                if текущий is None:
                    continue
                self._drop_cache(sid, текущий.kind)
                self.modules.pop(sid, None)
                if текущий.kind in (KIND_MODULES, KIND_EXTENSION):
                    инвалидировать.add(sid)

                if текущий.kind == KIND_CONFIGURATION:
                    self.configurations.pop(sid, None)
                    self._relation_cache.pop(sid, None)
                elif текущий.kind in (KIND_MODULES, KIND_EXTENSION):
                    pass
                elif текущий.kind == KIND_QUERY:
                    # В `syntax_versions` источника нет, но его элементы сидят
                    # в поисковом индексе и таблице имён `self.syntax` — без
                    # пересборки они останутся там до перезапуска.
                    self.query_source = None
                    self._relation_cache.clear()
                    отложенная_справка = dict(self.syntax_versions)
                elif self.syntax_versions.pop(sid, None) is not None:
                    self._relation_cache.clear()
                    отложенная_справка = dict(self.syntax_versions)

            for sid in инвалидировать:
                # Номер не сбрасывается: иначе remove -> add даст
                # тот же generation и ABA вернёт старому writer право голоса.
                self._следующее_поколение_модулей(sid)
                self._module_operations.pop(sid, None)

        for снести, каталог_кода in отложенные_сносы:
            снести(каталог_кода)
        if отложенная_справка is not None:
            self._apply_syntax(отложенная_справка)

    # ------------------------------------------------------------- разрешение

    def resolve(
        self,
        name: str | None = None,
        *,
        require_configuration: bool = True,
        extension: str | None = None,
    ) -> ResolvedContext:
        """Контекст для инструментов.

        `require_configuration=False` — для инструментов синтаксиса: справка
        полезна и без единой загруженной конфигурации, просто без фильтрации
        по версии платформы.

        `extension` — имя привязанного расширения (design doc, раздел 7):
        область работы — конфигурация плюс ОДНО расширение, а не все сразу.
        Без имени `ResolvedContext.extension` остаётся `None`, даже если у
        конфигурации есть загруженные расширения — молчаливый выбор «первого
        попавшегося» здесь так же не годится, как и для самой конфигурации.
        """
        requested_name = name
        for _ in range(2):
            with self._lock:
                names = sorted(self.configurations)
                syntax = self.syntax
                query_source = self.query_source

                if not names:
                    if not require_configuration and syntax is not None:
                        return ResolvedContext(
                            configuration=None,
                            syntax=syntax,
                            syntax_relation=RELATION_NONE,
                        )
                    чего_нет = ["выгрузка структуры конфигурации"]
                    if syntax is None:
                        чего_нет.append("справка платформы")
                    if query_source is None:
                        чего_нет.append("справка по языку запросов")
                    raise RegistryError(
                        "Не загружено ни одной конфигурации. Не хватает: "
                        + ", ".join(чего_нет)
                        + ". Выгрузку готовит обработка из `exporter-1c/`, "
                        "справки берутся из каталога установки платформы "
                        "(`shcntx_ru.hbk`, `shquery_ru.hbk`); загружаются "
                        "командой `reg-add`."
                    )

                selected_name = requested_name
                if selected_name is None:
                    if len(names) > 1:
                        raise RegistryError(
                            "Загружено несколько конфигураций, укажите нужную "
                            "явно: " + ", ".join(names)
                        )
                    selected_name = names[0]

                loaded = self.configurations.get(selected_name)
                if loaded is None:
                    raise RegistryError(
                        f"Конфигурация не загружена: {selected_name}. "
                        f"Доступны: {', '.join(names)}"
                    )
                modules_key = f"{selected_name}:modules"
                modules = self.modules.get(modules_key)
                extension_key = None
                расширение = None
                if extension is not None:
                    extension_key = (
                        f"{selected_name}:ext:{index_cache.safe_name(extension)}"
                    )
                    расширение = self.modules.get(extension_key)
                cached = self._relation_cache.get(selected_name)
                if (
                    cached is not None
                    and cached[0] is loaded
                    and cached[1] is syntax
                ):
                    relation, hidden = cached[2], cached[3]
                else:
                    relation = None
                    hidden = 0

            if relation is None:
                relation, hidden = self._compute_relation(loaded, syntax)

            with self._lock:
                unchanged = (
                    self.configurations.get(selected_name) is loaded
                    and self.syntax is syntax
                    and self.modules.get(modules_key) is modules
                    and (
                        extension_key is None
                        or self.modules.get(extension_key) is расширение
                    )
                    and (
                        requested_name is not None
                        or sorted(self.configurations) == names
                    )
                )
                if not unchanged:
                    continue
                self._relation_cache[selected_name] = (
                    loaded,
                    syntax,
                    relation,
                    hidden,
                )
                return ResolvedContext(
                    configuration=loaded,
                    syntax=syntax,
                    syntax_relation=relation,
                    syntax_hidden=hidden,
                    modules=modules,
                    extension=расширение,
                )

        raise RegistryError(
            "Источники изменились во время разрешения контекста дважды; "
            "повторите запрос."
        )

    def _compute_relation(
        self,
        loaded: LoadedConfiguration,
        syntax: LoadedSyntax | None,
    ) -> tuple[str, int]:
        if syntax is None:
            return RELATION_NONE, 0

        config_platform = parse_version(loaded.config.platform)[:3]
        syntax_platform = parse_version(syntax.source.platform)[:3]

        # Справок может быть несколько: если среди них есть справка релиза
        # конфигурации, ответ по ней точный — независимо от того, какая из
        # загруженных самая свежая.
        if config_platform and syntax.syntax.has_help_for(loaded.config.platform):
            return RELATION_EXACT, 0

        if not syntax_platform:
            # Версия справки неизвестна — фильтровать нечем. Молчать нельзя:
            # без фильтра агенту покажут методы, которых в его платформе нет.
            return RELATION_NONE, 0
        if not config_platform:
            return RELATION_EXACT, 0
        if syntax_platform == config_platform:
            return RELATION_EXACT, 0
        if syntax_platform > config_platform:
            hidden = syntax.syntax.hidden_for(loaded.config.platform)
            return RELATION_NEWER, hidden
        return RELATION_OLDER, 0

    # ------------------------------------------------------------- обзор

    def syntax_coverage(self) -> dict:
        """Каких справок не хватает и какие лишние.

        Справок нужно столько, сколько различных платформ у загруженных
        конфигураций: расхождение в один релиз стоит примерно 10–15 сигнатур
        и 35–45 контекстов доступности. Сопоставление по релизу, без номера
        сборки: справка 8.3.5.1570 описывает ту же платформу, что
        конфигурация 8.3.5.1234.
        """
        with self._lock:
            платформы_справок = {
                release(parse_version(source.platform)): source.platform
                for source in self.syntax_versions.values()
            }
            нужные: dict[tuple[int, ...], tuple[str, list[str]]] = {}
            for name in sorted(self.configurations):
                platform = self.configurations[name].config.platform
                key = release(parse_version(platform))
                if not key:
                    continue
                нужные.setdefault(key, (platform, []))[1].append(name)

        missing = [
            {"platform": platform, "configurations": names}
            for key, (platform, names) in sorted(нужные.items())
            if key not in платформы_справок
        ]
        unused = [
            platform
            for key, platform in sorted(платформы_справок.items())
            if key not in нужные
        ]
        return {
            "loaded": [platform for _, platform in sorted(платформы_справок.items())],
            "missing": missing,
            "unused": unused,
        }

    def overview(self) -> list[dict]:
        """Что загружено — в виде, пригодном и для дашборда, и для агента."""
        result = []
        for name in sorted(self.configurations):
            context = self.resolve(name)
            config = context.configuration.config
            result.append(
                {
                    "name": config.name,
                    "synonym": config.synonym,
                    "version": config.version,
                    "platform": config.platform,
                    "objects": len(config),
                    "edges": len(context.configuration.graph.edges),
                    "loaded_at": context.configuration.source.loaded_at,
                    "providers": {
                        "metadata": True,
                        "syntax": context.syntax is not None,
                        "modules": context.modules is not None,
                    },
                    "syntax_platform": context.syntax_platform,
                    "syntax_relation": context.syntax_relation,
                    "syntax_hidden": context.syntax_hidden,
                    "notes": context.notes(),
                }
            )
        return result

    # ------------------------------------------------------------- диск

    def save(self) -> None:
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "registry_version": REGISTRY_VERSION,
                "saved_at": _now(),
                "sources": [s.to_dict() for s in self.sources.values()],
            }
            tmp = self.registry_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self.registry_path)

    def reload_dictionary(self) -> None:
        """Перечитать словарь. Индексы при этом не перестраиваются.

        Ни синонимы, ни псевдонимы не участвуют в построении постингов —
        `SearchIndex.add()` их не читает, они нужны только в момент поиска.
        Пересборка стоила бы секунду на каждую правку словаря и обесценивала
        бы кэш; достаточно подменить две ссылки.
        """
        self.dictionary = Dictionary.load(self.dictionary_path)
        synonyms = self.dictionary.synonyms()
        with self._lock:
            for name, loaded in self.configurations.items():
                loaded.index.synonyms = synonyms
                loaded.index.aliases = self.dictionary.aliases_for(name)
                loaded.field_index.synonyms = synonyms

    def restore(self) -> list[str]:
        """Поднять источники, записанные в registry.json."""
        problems: list[str] = []
        if not self.registry_path.exists():
            return problems

        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        if payload.get("registry_version") != REGISTRY_VERSION:
            return [
                f"registry.json версии {payload.get('registry_version')}, "
                f"ожидается {REGISTRY_VERSION} — источники будут перечитаны заново."
            ]

        # Код восстанавливается вторым проходом: порядок строк в
        # registry.json не является контрактом, а модули/расширение
        # без уже восстановленной конфигурации не имеют владельца.
        все_источники = [
            Source.from_dict(raw) for raw in (payload.get("sources") or [])
        ]
        with self._lock:
            отложенный_код = [
                (source, self._modules_generation.get(source.id, 0))
                for source in все_источники
                if source.kind in (KIND_MODULES, KIND_EXTENSION)
                and not self._module_recovery_blocked
            ]
        конфигурации_этого_restore: dict[str, Source] = {}

        for source in все_источники:
            if source.kind in (KIND_MODULES, KIND_EXTENSION):
                continue
            stored = self._absolute(source.stored_path)
            if not stored.exists():
                problems.append(f"{source.id}: файл источника пропал ({stored})")
                continue
            try:
                if source.kind == KIND_CONFIGURATION:
                    with self._lock:
                        живой_reparse = any(
                            операция.configuration == source.id
                            for операция in self._module_operations.values()
                        )
                    if живой_reparse:
                        # Foreground add/reparse привязан к identity
                        # текущей конфигурации. Не подменяем её заново
                        # созданным Source из старого registry.json.
                        continue
                    восстановленный_source = self.add_configuration(
                        stored, keep_source=False, known_sha256=source.sha256
                    )
                    конфигурации_этого_restore[
                        source.id
                    ] = восстановленный_source
                else:
                    # Слитый вид собирается один раз в конце: сборка на каждой
                    # справке дала бы квадрат работы, а видел бы её только
                    # последний прогон. Вид источника передаём как сохранённый
                    # (`known_kind`), а не распознаём заново: угадывание по
                    # содержимому `.gz` на пустом или повреждённом индексе
                    # может ошибиться, а сохранённый вид — уже проверенный факт.
                    self.add_syntax(
                        stored,
                        platform=source.platform,
                        keep_source=False,
                        known_sha256=source.sha256,
                        rebuild=False,
                        known_kind=source.kind,
                    )
            except Exception as error:  # источник не должен ронять запуск
                problems.append(f"{source.id}: {error}")

        for source, expected_generation in отложенный_код:
            stored = self._absolute(source.stored_path)
            if not stored.exists():
                problems.append(f"{source.id}: файл источника пропал ({stored})")
                continue
            configuration = self._владелец_источника_кода(source)
            expected = (
                конфигурации_этого_restore.get(configuration)
                if configuration is not None
                else None
            )
            if configuration is None or expected is None:
                problems.append(
                    f"{source.id}: индекс кода не поднят — "
                    "в этом восстановлении нет его конфигурации."
                )
                continue
            # Код уже лежит на диске; архив могли убрать. Source
            # и generation публикуются атомарно только пока identity
            # владельца совпадает с этим проходом restore.
            try:
                self._поднять_или_построить_модули(
                    source,
                    stored,
                    configuration=configuration,
                    expected_configuration_source=expected,
                    expected_generation=expected_generation,
                )
            except Exception as error:
                problems.append(
                    f"{source.id}: индекс кода не поднят и не "
                    f"построен — {error}"
                )

        if self.syntax_versions or self.query_source is not None:
            # Сборка не должна ронять запуск: испорченная справка версии
            # называется в списке проблем, остальные продолжают работать.
            # Условие срабатывает и когда справок платформы нет вовсе, но
            # загружен язык запросов — иначе после перезапуска он оставался
            # бы в `self.sources`, а `self.syntax` так и не собрался бы.
            try:
                problems += self._apply_syntax(dict(self.syntax_versions))
            except Exception as error:
                problems.append(f"слитый вид не собран: {error}")
        return problems

    def bootstrap(self) -> list[str]:
        """Проиндексировать всё новое из `data/bootstrap/`.

        Так сервер поднимается готовым: файлы кладутся в каталог при сборке
        образа или в volume, руками через дашборд ничего делать не нужно.
        """
        added: list[str] = []
        if not self.bootstrap_dir.exists():
            return added

        known = {s.sha256 for s in self.sources.values()}
        справок = 0
        for path in sorted(self.bootstrap_dir.rglob("*")):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix not in (".zip", ".hbk"):
                continue
            if suffix == ".zip" and _похоже_на_выгрузку_в_файлы(path):
                # Гигабайтную выгрузку нельзя разбирать при каждом старте,
                # и падать на ней вечно — тоже: упавший файл в реестр не
                # попадает, а `known` считается один раз до цикла.
                added.append(
                    f"{path.name}: это выгрузка конфигурации в файлы — "
                    "её кладут в data/incoming/ и разбирают по кнопке."
                )
                continue
            if _sha256(path) in known:
                continue
            try:
                if suffix == ".zip":
                    source = self.add_configuration(path)
                else:
                    source = self.add_syntax(path, rebuild=False)
                    справок += 1
                added.append(source.id)
            except Exception as error:
                added.append(f"{path.name}: ОШИБКА — {error}")

        if справок:
            self._apply_syntax(dict(self.syntax_versions))
        return added

    def _syntax_index_dir(self) -> Path:
        return self.index_dir / "syntax"

    def sweep_syntax(self) -> list[str]:
        """Снести разобранные справки, которых не заявил ни один источник.

        Каталог рос с каждой попыткой: снятая с учёта справка оставляла свой
        `.json.gz` лежать. Индекс производный, восстанавливается разбором
        `.hbk`, поэтому сносится так же, как кэш. Справки версий, стоящие в
        реестре, остаются все — их заявляют свои источники.
        """
        directory = self._syntax_index_dir()
        if not directory.is_dir():
            return []

        allowed = {
            self._absolute(source.stored_path).resolve()
            for source in self.sources.values()
            # Язык запросов лежит в том же каталоге
            # (`index/syntax/query-ru.json.gz`), но в `syntax_versions` не
            # попадает — без этой оговорки его разобранный индекс сочли бы
            # ничьим и снесли на первом же старте.
            if source.kind in (KIND_SYNTAX, KIND_QUERY) and source.stored_path
        }
        # Слитый вид источником не заявлен — он производное от всего набора.
        # Действующий оставляем, устаревшие уходят вместе с прочим мусором.
        with self._lock:
            versions = list(self.syntax_versions.values())
        if versions:
            по_версии = sorted(versions, key=lambda s: parse_version(s.platform))
            allowed.add(self._merged_path(по_версии).resolve())
        removed: list[str] = []
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.resolve() in allowed:
                continue
            try:
                path.unlink()
            except OSError:
                # Том только на чтение. Лишний файл полежит, старт важнее.
                continue
            removed.append(path.name)
        return removed

    def orphan_sources(self) -> list[tuple[Path, int]]:
        """Исходные файлы, на которые не ссылается ни один источник.

        Сюда попадает и исходник действующей справки: `stored_path` источника
        указывает на разобранный индекс, а не на `.hbk`, из которого тот
        получен. Связь теряется при первом же восстановлении с диска, поэтому
        различать «нужный» и «лишний» реестр не берётся — он честно говорит,
        что ни на один из этих файлов не ссылается.

        Не удаляются автоматически: справку от снятой с поддержки платформы
        взять заново негде, и молчаливое удаление стоило бы дороже занятого
        места. Но место они занимают, и человек должен об этом знать.
        """
        # Словарь меняется при remove/readd. Под замком копируем только строки;
        # `resolve`, `rglob`, `is_file` и `stat` остаются снаружи, потому что
        # могут обратиться к файловой системе и надолго задержаться.
        with self._lock:
            stored_paths = tuple(
                source.stored_path
                for source in self.sources.values()
                if source.stored_path
            )
        used = {self._absolute(path).resolve() for path in stored_paths}
        orphans: list[tuple[Path, int]] = []
        if not self.sources_dir.is_dir():
            return orphans
        for path in sorted(self.sources_dir.rglob("*")):
            if not path.is_file() or path.resolve() in used:
                continue
            orphans.append((path, path.stat().st_size))
        return orphans

    def startup(self) -> list[str]:
        """Восстановить реестр один раз, не мешая параллельным `resolve()`."""
        with self._startup_lock:
            self._startup_generation += 1
            поколение = self._startup_generation
            результат = self._startup_once()
            self._last_successful_startup_generation = поколение
            return результат

    def _подмести_временный_кэш_модулей(self) -> None:
        """Удалить staging-каталоги, пережившие аварийную остановку."""
        try:
            кандидаты = tuple(self.cache_dir.glob(".modules-cache.tmp-*"))
        except OSError:
            return
        for path in кандидаты:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    def wait_for_module_builds(self, timeout: float = 90.0) -> bool:
        """Дождаться фоновых индексов кода, но не дольше ``timeout`` секунд.

        Серверу ждать не нужно: во время холодной сборки он показывает
        прогресс. Одноразовый CLI-процесс иначе завершился бы вместе с
        daemon-потоками до результата и до записи четырёх файлов кэша.
        Ожидание публичное и ограниченное; замок реестра на ``join`` не
        удерживается.
        """
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout должен быть неотрицательным числом")
        if timeout < 0:
            raise ValueError("timeout должен быть неотрицательным числом")
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                for source_id, thread in tuple(self._module_builds.items()):
                    if thread.ident is not None and not thread.is_alive():
                        if self._module_builds.get(source_id) is thread:
                            self._module_builds.pop(source_id, None)
                threads = tuple(dict.fromkeys(self._module_builds.values()))
            if not threads:
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            for thread in threads:
                if thread is threading.current_thread():
                    return False
                # Между регистрацией и `start()` есть несколько инструкций.
                # `join()` на ещё не запущенном потоке бросает RuntimeError;
                # коротко уступаем процессор и перечитываем registry.
                if thread.ident is None:
                    time.sleep(min(0.001, remaining))
                    break
                thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def _startup_once(self) -> list[str]:
        # Каталог приёма создаёт сервер, как и каталог данных в `save()`.
        # Пока каталога нет, `scan()` возвращает пустой список, блок
        # «Входящие выгрузки» на странице не рисуется вовсе — и человек не
        # видит даже подсказки, куда класть архив. В боевом `data/` каталога
        # нет: `mkdir` из `Dockerfile` на bind-mount не действует.
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        # Словарь перечитывается первым: правки в нём должны применяться
        # перезагрузкой, без пересборки образа и рестарта контейнера.
        self.dictionary = Dictionary.load(self.dictionary_path)
        # Foreground-публикация держит тот же lock от staging кэша до
        # registry.json. Startup либо ждёт её завершения в живом процессе,
        # либо восстанавливает journal, оставшийся после SIGKILL.
        messages: list[str] = []
        # Живой foreground writer уже сам завершит или откатит операцию;
        # startup не ждёт его тяжёлую запись кэша. После SIGKILL mutex
        # свободен, и новый процесс забирает journal без ожидания.
        if self._modules_cache_lock.acquire(blocking=False):
            try:
                messages = self._восстановить_рокировку_кода()
                self._подмести_временный_кэш_модулей()
            finally:
                self._modules_cache_lock.release()
        self._module_recovery_blocked = bool(messages)
        if self._module_recovery_blocked:
            self._заблокировать_источники_кода_после_ошибки_рокировки()
        messages += self.restore()
        if self._module_recovery_blocked:
            # Сохраняем исходный registry.json и code-cache дословно: после
            # ручного исправления/удаления WAL следующий startup должен
            # суметь поднять прежнее готовое поколение, а не обнаружить, что
            # fail-closed проверка сама его забыла и подмела.
            return messages
        messages += [f"добавлено из bootstrap: {name}" for name in self.bootstrap()]
        # Уборка после загрузки, а не до: снести нужно то, что не заявил ни
        # один источник, а список источников известен только теперь.
        dropped = index_cache.sweep(self.cache_dir, self._cached_names())
        if dropped:
            messages.append(f"убрано из кэша индексов: {', '.join(dropped)}")
        stale = self.sweep_syntax()
        if stale:
            messages.append(f"убрано разобранных справок: {', '.join(stale)}")
        orphans = self.orphan_sources()
        if orphans:
            весом = sum(size for _, size in orphans) / 1024 / 1024
            messages.append(
                f"исходных файлов: {len(orphans)}, {весом:.0f} МБ — для ответов "
                "не нужны, видны на странице источников, удаляются вручную"
            )
        self.save()
        return messages
