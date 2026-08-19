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

import hashlib
import json
import re
import shutil
import threading
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import index_cache
from .dictionary import Dictionary
from .graph import Graph
from .loader import ExportError, load
from .model import Configuration
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _похоже_на_выгрузку_в_файлы(path: Path) -> bool:
    """Выгрузка в файлы против выгрузки schema v1.

    Выгрузок в файлы две: иерархическая (модули в .bsl, .Form) и плоская
    (модули в .txt). Смотрим только имена членов: тело не читается,
    центрального каталога достаточно.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            имена = zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return False
    есть_код = any(и.endswith((".bsl", ".Form", ".txt")) for и in имена)
    есть_манифест = any(
        и.endswith(("manifest.json", "manifest.xml")) for и in имена
    )
    return есть_код and not есть_манифест


# Пространство имён `Configuration.xml` выгрузки в файлы — как у метаданных,
# так и у расширения: оба используют один формат MDClasses.
_NS_MDCLASSES = "http://v8.1c.ru/8.3/MDClasses"


def _сведения_о_выгрузке(path: Path) -> tuple[bool, str]:
    """Расширение это или конфигурация, и как расширение зовут.

    Смотрит только `Configuration.xml` в корне архива — тело не читается: имя
    расширения и признаки лежат в его `Properties`, а модулей в архиве могут
    быть тысячи.

    Правило распознавания (проверено на живой паре «конфигурация + её
    расширение», CHANGELOG → «Найдено»): у расширения есть `ObjectBelonging`
    и `ConfigurationExtensionPurpose`, непустой `NamePrefix`, и **нет**
    `CompatibilityMode`. У конфигурации наоборот: `CompatibilityMode` есть, а
    `NamePrefix` присутствует, но пустой. `ConfigurationExtensionCompatibilityMode`
    — ложный признак, он стоит у обеих, отличить по нему нельзя.

    `(False, "")` — это не выгрузка расширения: нет `Configuration.xml`, он
    битый, в нём нет `Properties` (синтетические архивы тестов модулей несут
    `<x/>`), или признаки не сошлись. Так же обрабатывается настоящая
    выгрузка конфигурации — вызывающий продолжает её как модули.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            содержимое = zf.read("Configuration.xml")
    except (OSError, zipfile.BadZipFile, KeyError):
        return False, ""
    try:
        корень = ET.fromstring(содержимое)
    except ET.ParseError:
        return False, ""
    свойства = корень.find(
        f"{{{_NS_MDCLASSES}}}Configuration/{{{_NS_MDCLASSES}}}Properties"
    )
    if свойства is None:
        return False, ""

    def значение(тег: str) -> str:
        узел = свойства.find(f"{{{_NS_MDCLASSES}}}{тег}")
        return (узел.text or "").strip() if узел is not None and узел.text else ""

    это_расширение = (
        not значение("CompatibilityMode")
        and bool(значение("ObjectBelonging"))
        and bool(значение("ConfigurationExtensionPurpose"))
        and bool(значение("NamePrefix"))
    )
    if not это_расширение:
        return False, ""
    return True, значение("Name")


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
    отбора живёт в `intake` — второго правила здесь не заводится.
    """
    from . import intake

    with zipfile.ZipFile(архив) as zf:
        записи = [i for i in zf.infolist() if not i.is_dir()]
        формат = intake.detect_format([i.filename for i in записи])
        return sum(1 for i in записи if intake.is_wanted(i.filename, формат))


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
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Source":
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

    def notes(self, *, critical_only: bool = False) -> list[str]:
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
        self.dictionary_path = self.data_dir / "dictionary.json"
        self.dictionary = Dictionary.load(self.dictionary_path)

        self._lock = threading.RLock()
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
        # Соотношение версий не меняется, пока не сменились источники, а его
        # вычисление — перебор всех элементов справки. Без кэша это давало
        # 16 мс на каждый вызов любого инструмента.
        self._relation_cache: dict[str, tuple[str, int]] = {}

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
        KIND_MODULES: ("modules",),
        # Тот же вид кэша, что у модулей конфигурации: провайдер поиска по
        # коду, когда появится, не должен различать их источники. Без этой
        # записи `sweep` счёл бы кэш расширения ничьим на первом же старте —
        # ровно то, от чего предупреждает комментарий выше для KIND_QUERY.
        KIND_EXTENSION: ("modules",),
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
        for index_kind in self.CACHE_KINDS.get(kind, ()):
            self._cache_path(source_id, index_kind).unlink(missing_ok=True)

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

    def _extension_root(self, configuration: str, extension: str) -> Path:
        """Каталог кода расширения: `extensions_dir/<Конфигурация>/<Расширение>`.

        Два уровня, а не склейка имён в одно: `index_cache.safe_name`
        схлопывает необычные символы в подчёркивание, и склейка вида
        `Розница@ЮТД` дала бы `Розница_ЮТД` — путь, который может совпасть с
        конфигурацией, названной так же. Каждый уровень чистится тем же
        правилом, что и `_modules_root`, и путь так же проверяется на
        принадлежность корню: имя расширения приходит из выгрузки человека,
        а не проверено заранее.
        """
        корень = (
            self.extensions_dir
            / index_cache.safe_name(configuration)
            / index_cache.safe_name(extension)
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
        from . import intake

        архив = Path(path)
        if configuration not in self.configurations:
            raise RegistryError(
                f"{архив.name}: конфигурация «{configuration}» не загружена."
            )

        это_расширение, имя_расширения = _сведения_о_выгрузке(архив)
        if это_расширение:
            if not имя_расширения:
                raise RegistryError(
                    f"{архив.name}: похоже на выгрузку расширения, но тег "
                    "Name в Configuration.xml пуст — имя расширения взять "
                    "неоткуда."
                )
            return self._add_extension(
                архив, configuration=configuration, extension=имя_расширения
            )

        корень = self._modules_root(configuration)
        # Годность архива выясняется ДО того, как что-то удалено. Выгрузка
        # метаданных (`СтруктураКонфигурации_*.zip`) — тоже .zip, и отбор не
        # находит в ней ничего; ровно на эту ошибку человека и рассчитан текст
        # отказа. Проверка после очистки означала бы, что ошибочное нажатие
        # сносит уже разобранные 351 МБ кода, а взять их заново неоткуда, если
        # гигабайтный архив из `incoming/` уже убран. Считаем по центральному
        # каталогу zip: тело архива не читается.
        if not _отбираемых_членов(архив):
            raise _нет_модулей(архив)
        # Хеш считается после проверки годности: это полный проход по файлу,
        # и платить им за архив, который мы всё равно не возьмём, незачем.
        digest = _sha256(архив)
        # Корень чистится перед распаковкой: `extract` пишет поверх, и файлы,
        # которых в новой выгрузке нет (удалённый объект, переименованный
        # модуль), остались бы навсегда — переразбор молча смешивал бы две
        # выгрузки в одном каталоге.
        self._drop_modules_root(корень)
        файлов, байт = intake.extract(архив, корень)
        if not файлов:
            # Предпроверка считает по именам из центрального каталога, а
            # `extract` прогоняет каждый член ещё и через `intake.safe_target`
            # — тот отвергает абсолютные пути и `..`. Архив, у которого все
            # отбираемые члены такие, предпроверку проходит, а на диск не
            # кладёт ничего: без этой проверки завёлся бы источник со
            # `status=ready` при пустом каталоге. Обычным архиватором такое не
            # собирается, но проверка стоит пять строк, а счётчик уже на руках.
            self._drop_modules_root(корень)
            raise _нет_модулей(архив)
        # Выгрузка в файлы точной сборки платформы не содержит: в
        # Configuration.xml лежит только режим совместимости
        # (CompatibilityMode), а это другое число — на Рознице 2.3.10.5 в
        # выгрузке стоит Version8_3_21, тогда как фактическая платформа по
        # выгрузке метаданных — 8.3.23.1997. Берём её у привязанной
        # конфигурации — она знает свою точную сборку; пустая — оставляем
        # пустой, ничего не выдумываем. Между первичной проверкой членства
        # выше и этой строкой прошли секунды распаковки в отдельном потоке —
        # конфигурацию могли снять параллельным запросом. `.get(...)` вместо
        # `[...]` не роняет разбор голым `KeyError`: пропавшая конфигурация
        # даёт тот же результат, что и отсутствие платформы у неё.
        привязанная = self.configurations.get(configuration)
        платформа = привязанная.source.platform if привязанная else ""
        source = Source(
            id=f"{configuration}:modules",
            kind=KIND_MODULES,
            origin=архив.name,
            sha256=digest,
            loaded_at=_now(),
            platform=платформа,
            status=STATUS_READY,
            items_total=файлов,
            stored_path=self._relative(корень),
        )
        with self._lock:
            self.sources[source.id] = source
        return source

    def _add_extension(
        self, архив: Path, *, configuration: str, extension: str
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
        """
        from . import intake

        корень = self._extension_root(configuration, extension)
        # Тот же порядок, что у add_modules: годность архива выясняется до
        # того, как что-то удалено, — иначе ошибочное нажатие сносит уже
        # разобранное расширение, а взять его заново неоткуда, если исходник
        # из incoming/ уже убран.
        if not _отбираемых_членов(архив):
            raise _нет_модулей(архив)
        digest = _sha256(архив)
        self._drop_extension_root(корень)
        файлов, байт = intake.extract(архив, корень)
        if not файлов:
            self._drop_extension_root(корень)
            raise _нет_модулей(архив)
        # Платформа — как у модулей: выгрузка расширения точной сборки не
        # содержит (тег Version пуст), берём у привязанной конфигурации.
        привязанная = self.configurations.get(configuration)
        платформа = привязанная.source.platform if привязанная else ""
        source = Source(
            id=f"{configuration}:ext:{extension}",
            kind=KIND_EXTENSION,
            origin=архив.name,
            sha256=digest,
            loaded_at=_now(),
            platform=платформа,
            status=STATUS_READY,
            items_total=файлов,
            stored_path=self._relative(корень),
        )
        with self._lock:
            self.sources[source.id] = source
        return source

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
        # (работа без справки платформы — задача 4, но собрать индекс нужно
        # уже здесь).
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
        каталог_модулей: Path | None = None
        with self._lock:
            source = self.sources.pop(source_id, None)
            if source is None:
                raise RegistryError(f"Источник не зарегистрирован: {source_id}")
            self._drop_cache(source_id, source.kind)
            if source.kind == KIND_CONFIGURATION:
                self.configurations.pop(source_id, None)
                self._relation_cache.pop(source_id, None)
                return
            if source.kind in (KIND_MODULES, KIND_EXTENSION):
                # Каталог с кодом — всё, что этот источник занимает на диске
                # (351 МБ на живой конфигурации, меньше у расширения).
                # `orphan_sources` его не покажет: тот обходит только
                # `sources_dir`. Не снести значит занять место невидимо и
                # навсегда — вернуть его через интерфейс было бы нечем. Но
                # сносится он ПОСЛЕ выхода из-под замка: это тысячи файлов, а
                # тот же замок берут `resolve()` и все инструменты MCP — на
                # время удаления встали бы и страницы, и `/health`, и ответы
                # инструментов.
                каталог_модулей = (
                    self._absolute(source.stored_path) if source.stored_path else None
                )
            elif source.kind == KIND_QUERY:
                # В `syntax_versions` источника нет, но его элементы сидят в
                # поисковом индексе и таблице имён `self.syntax` — без
                # пересборки они останутся там до перезапуска.
                self.query_source = None
                self._relation_cache.clear()
                snapshot = dict(self.syntax_versions)
            elif self.syntax_versions.pop(source_id, None) is None:
                return
            else:
                self._relation_cache.clear()
                snapshot = dict(self.syntax_versions)

        if source.kind in (KIND_MODULES, KIND_EXTENSION):
            if каталог_модулей is not None:
                if source.kind == KIND_MODULES:
                    self._drop_modules_root(каталог_модулей)
                else:
                    self._drop_extension_root(каталог_модулей)
            return

        self._apply_syntax(snapshot)

    # ------------------------------------------------------------- разрешение

    def resolve(
        self, name: str | None = None, *, require_configuration: bool = True
    ) -> ResolvedContext:
        """Контекст для инструментов.

        `require_configuration=False` — для инструментов синтаксиса: справка
        полезна и без единой загруженной конфигурации, просто без фильтрации
        по версии платформы.
        """
        with self._lock:
            names = sorted(self.configurations)

        if not names:
            if not require_configuration and self.syntax is not None:
                return ResolvedContext(
                    configuration=None,
                    syntax=self.syntax,
                    syntax_relation=RELATION_NONE,
                )
            # На пустом сервере не хватает не только выгрузки: справок тоже
            # нет, и человек у первого запуска узнавал об этом лишь со второго
            # захода — отказ приходит отсюда раньше, чем до дела доходит текст
            # про источники. Называем всё недостающее сразу.
            чего_нет = ["выгрузка структуры конфигурации"]
            if self.syntax is None:
                чего_нет.append("справка платформы")
            if self.query_source is None:
                чего_нет.append("справка по языку запросов")
            raise RegistryError(
                "Не загружено ни одной конфигурации. Не хватает: "
                + ", ".join(чего_нет)
                + ". Выгрузку готовит обработка из `exporter-1c/`, справки "
                "берутся из каталога установки платформы (`shcntx_ru.hbk`, "
                "`shquery_ru.hbk`); загружаются командой `reg-add`."
            )

        if name is None:
            if len(names) > 1:
                raise RegistryError(
                    "Загружено несколько конфигураций, укажите нужную явно: "
                    + ", ".join(names)
                )
            name = names[0]

        loaded = self.configurations.get(name)
        if loaded is None:
            raise RegistryError(
                f"Конфигурация не загружена: {name}. Доступны: {', '.join(names)}"
            )

        relation, hidden = self._compare_platforms(loaded)
        return ResolvedContext(
            configuration=loaded,
            syntax=self.syntax,
            syntax_relation=relation,
            syntax_hidden=hidden,
        )

    def _compare_platforms(self, loaded: LoadedConfiguration) -> tuple[str, int]:
        cached = self._relation_cache.get(loaded.config.name)
        if cached is not None:
            return cached
        result = self._compute_relation(loaded)
        self._relation_cache[loaded.config.name] = result
        return result

    def _compute_relation(self, loaded: LoadedConfiguration) -> tuple[str, int]:
        if self.syntax is None:
            return RELATION_NONE, 0

        config_platform = parse_version(loaded.config.platform)[:3]
        syntax_platform = parse_version(self.syntax.source.platform)[:3]

        # Справок может быть несколько: если среди них есть справка релиза
        # конфигурации, ответ по ней точный — независимо от того, какая из
        # загруженных самая свежая.
        if config_platform and self.syntax.syntax.has_help_for(loaded.config.platform):
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
            hidden = self.syntax.syntax.hidden_for(loaded.config.platform)
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
                        "modules": False,
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

        for raw in payload.get("sources") or []:
            source = Source.from_dict(raw)
            stored = self._absolute(source.stored_path)
            if not stored.exists():
                problems.append(f"{source.id}: файл источника пропал ({stored})")
                continue
            try:
                if source.kind == KIND_CONFIGURATION:
                    self.add_configuration(
                        stored, keep_source=False, known_sha256=source.sha256
                    )
                elif source.kind in (KIND_MODULES, KIND_EXTENSION):
                    # Код уже лежит на диске — его положил `add_modules`
                    # (или `_add_extension`) до остановки, а индекс по коду
                    # этот план не строит. Перечитывать архив здесь нечем
                    # (человек мог его уже убрать) и незачем: без этой ветки
                    # запись выпала бы из `self.sources` молча, и после
                    # рестарта выглядело бы так, будто выгрузку вообще не
                    # разбирали, — гоняли бы заново гигабайтный архив там,
                    # где хватило бы записи из `registry.json`.
                    with self._lock:
                        self.sources[source.id] = source
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
        used = {
            self._absolute(source.stored_path).resolve()
            for source in self.sources.values()
            if source.stored_path
        }
        orphans: list[tuple[Path, int]] = []
        if not self.sources_dir.is_dir():
            return orphans
        for path in sorted(self.sources_dir.rglob("*")):
            if not path.is_file() or path.resolve() in used:
                continue
            orphans.append((path, path.stat().st_size))
        return orphans

    def startup(self) -> list[str]:
        # Каталог приёма создаёт сервер, как и каталог данных в `save()`.
        # Пока каталога нет, `scan()` возвращает пустой список, блок
        # «Входящие выгрузки» на странице не рисуется вовсе — и человек не
        # видит даже подсказки, куда класть архив. В боевом `data/` каталога
        # нет: `mkdir` из `Dockerfile` на bind-mount не действует.
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        # Словарь перечитывается первым: правки в нём должны применяться
        # перезагрузкой, без пересборки образа и рестарта контейнера.
        self.dictionary = Dictionary.load(self.dictionary_path)
        messages = self.restore()
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
