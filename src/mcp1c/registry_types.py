"""Типы-данные реестра: учётные записи, снимки, контекст и ошибки.

Вынесены из `registry.py`, чтобы модуль реестра держал поведение, а не
ещё шестьсот строк описаний. Модуль не зависит от `registry.py`;
`registry.py` реэкспортирует имена, которыми пользуется сам, и
`from mcp1c.registry import X` у потребителей продолжает работать. Ни одно из этих значений не пиклится и не пишется в
кэш как объект: `Source` уходит на диск через `to_dict()`, поэтому смена
модуля определения ничего не ломает на диске.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from . import modules_index, source_ids, structure_origin
from .extension_runtime import ExtensionRuntimeSnapshot
from .graph import Graph
from .intake_v2 import ExportIdentity, GenerationManifest, SourceKind
from .intake_v2_extensions import ExtensionResolution
from .intake_v2_registry import (
    GenerationConflictError,
    GenerationPointer,
    GenerationView,
    legacy_generation_view,
    native_generation_view,
)
from .model import Configuration
from .role_access import LoadedRoleAccess
from .search import SearchIndex
from .syntax_model import SyntaxIndex, SyntaxItem, parse_version
from .virtual_tables import TableTemplate


KIND_CONFIGURATION = "configuration"
KIND_SYNTAX = "syntax"
KIND_MODULES = "modules"
KIND_EXTENSION = "extension"
KIND_EXTENSION_RUNTIME = "extension-runtime"

STATUS_LOADING = "loading"
STATUS_READY = "ready"
STATUS_ERROR = "error"

INCOMPLETE_CONFIGURATION_WARNING = (
    "Источник опубликован в явном режиме неполной выгрузки: truncated=true; "
    "отсутствие объекта или связи ничего не доказывает."
)

# Соотношение версии справки и платформы конфигурации.
RELATION_EXACT = "exact"
RELATION_NEWER = "newer"
RELATION_OLDER = "older"
RELATION_NONE = "none"
RELATION_UNKNOWN = "unknown"

class RegistryError(Exception):
    """Источник не загружается или запрошено то, чего нет."""


class SourceInUseError(RegistryError):
    """Файл снова используется или ещё готовится к публикации."""


# Потребители выбирают поведение по типу: HTTP-статус дашборда, скрытие
# имён конфигураций в ответе MCP. Раньше они сравнивали начало текста, и
# правка формулировки молча меняла контракт API. Тексты остались прежними.
class ReloadRequired(RegistryError):
    """Активное поколение требует перезагрузки до обслуживания чтения."""


class ConfigurationNotLoaded(RegistryError):
    """Операции над конфигурацией нужна загруженная конфигурация (код ошибки)."""


class UnknownConfiguration(RegistryError):
    """Запрошенной по имени конфигурации нет; текст называет имена."""


class AmbiguousConfiguration(RegistryError):
    """Конфигурация не указана, а загружено несколько; текст называет имена."""


class SourcesChanged(RegistryError):
    """Снимок источников дважды подряд не прошёл поздний CAS; запрос повторяют."""

class GenerationPublishConflict(RegistryError, GenerationConflictError):
    """Active generation или legacy view изменились после preview."""


class RegistryRecoveryBlocked(RegistryError):
    """Журнал незавершённой операции не восстановлен; запись в data/ закрыта."""


class RegistryChangedExternally(RegistryError):
    """registry.json записал другой процесс после того, как его прочитал этот."""


class RegistryOwnedElsewhere(RegistryError):
    """Операция требует владения data/, а им владеет работающий сервер."""

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
    # из которого её брал прежний приём кода, на диск не копировался — без
    # этого поля `restore()` старой строки registry.json не смог бы
    # восстановить её ни из чего.
    code_version: str = ""
    # Администратор явно разрешил публикацию schema v1 с truncated=true.
    # Старые записи без поля восстанавливаются fail-closed.
    incomplete: bool = False

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
            "incomplete": self.incomplete,
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
            incomplete=raw.get("incomplete") is True,
        )


@dataclass(slots=True)
class LoadedConfiguration:
    source: Source
    config: Configuration
    graph: Graph
    index: SearchIndex
    field_index: SearchIndex
    # Для legacy совпадает с source.sha256. Native resolved-view зависит от
    # двух структурных слоёв, поэтому кэш нельзя штамповать одним base SHA.
    structure_sha256: str = ""


@dataclass(slots=True)
class LoadedExtensionRuntime:
    """Отдельный point-in-time снимок расширений одного сеанса 1С."""

    source: Source
    snapshot: ExtensionRuntimeSnapshot


@dataclass(slots=True)
class LoadedSyntax:
    source: Source
    syntax: SyntaxIndex
    index: SearchIndex
    # Имя в нижнем регистре -> элементы. Без него точное совпадение искалось
    # перебором всех 25 тысяч элементов на каждый вызов get_syntax.
    by_name: dict[str, list[SyntaxItem]] = field(default_factory=dict)
    # Вид объекта -> шаблоны таблиц запроса. По той же причине, что `by_name`:
    # без него `get_object` по регистру перебирал всю справку и стоил 14,8 мс
    # против 0,04 мс на объекте без таблиц.
    tables: dict[str, list[TableTemplate]] = field(default_factory=dict)

    def find_exact(self, name: str) -> list[SyntaxItem]:
        return self.by_name.get(name.strip().lower(), [])

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
    # Компактное доказательство происхождения структуры публикуется тем же
    # поколением и тем же swap, что код. Исходный ZIP после приёма не нужен.
    структура: structure_origin.StructureCatalog | None = None
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
class SourceSnapshot:
    """Неизменяемая учётная запись одного поколения ``Source``."""

    id: str
    kind: str
    origin: str
    sha256: str
    loaded_at: str
    platform: str
    status: str
    error: str
    warnings: tuple[str, ...]
    items_total: int
    stored_path: str
    selection_version: int
    locator_generation: int
    code_version: str
    incomplete: bool

    @classmethod
    def capture(cls, source: Source) -> "SourceSnapshot":
        return cls(
            id=source.id,
            kind=source.kind,
            origin=source.origin,
            sha256=source.sha256,
            loaded_at=source.loaded_at,
            platform=source.platform,
            status=source.status,
            error=source.error,
            warnings=tuple(source.warnings),
            items_total=source.items_total,
            stored_path=source.stored_path,
            selection_version=source.selection_version,
            locator_generation=source.locator_generation,
            code_version=source.code_version,
            incomplete=source.incomplete,
        )


@dataclass(frozen=True, slots=True)
class RegistryCodeSnapshot:
    """Ссылка на пакет индексов и скопированное состояние его публикации."""

    source: SourceSnapshot | None
    loaded: LoadedModules | None
    ready: bool
    status: str
    error: str
    stage: tuple[int, int]
    stage_title: str
    progress: tuple[int, int]


@dataclass(frozen=True, slots=True)
class RegistryExtensionRuntimeSnapshot:
    """Неизменяемая пара identity источника и разобранного снимка."""

    source: SourceSnapshot
    snapshot: ExtensionRuntimeSnapshot


@dataclass(frozen=True, slots=True)
class RegistryGenerationSnapshot:
    """Неизменяемая согласованная пара active pointer и его manifest."""

    pointer: GenerationPointer
    manifest: GenerationManifest

    def __post_init__(self) -> None:
        if self.pointer.identity != self.manifest.identity:
            raise RegistryError("generation snapshot смешивает identity")
        if self.pointer.generation_id != self.manifest.generation_id:
            raise RegistryError("generation snapshot смешивает поколения")
        if self.pointer.manifest_sha256 != self.manifest.sha256:
            raise RegistryError("generation snapshot смешивает pointer и manifest")

    @property
    def view(self) -> GenerationView:
        return native_generation_view(
            self.manifest,
            self.pointer.activation,
            legacy_barrier=(
                self.pointer.activation is None
            ),
        )


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """Один структурный снимок реестра, целиком снятый под его lock.

    Карты закрыты ``MappingProxyType``, а изменяемые поля ``Source`` и
    прогресса кода скопированы в frozen-значения. Тяжёлые индексы не
    копируются: ``LoadedConfiguration``, ``LoadedSyntax`` и ``LoadedModules``
    публикуются поколениями и используются потребителями только для чтения.
    Поздний ``Registry.snapshot_is_current`` проверяет, что такое поколение
    не сменилось, пока ответ строился без удержания lock.
    """

    configurations: Mapping[str, LoadedConfiguration]
    syntax_versions: Mapping[str, SourceSnapshot]
    syntax: LoadedSyntax | None
    sources: Mapping[str, SourceSnapshot]
    modules: Mapping[str, RegistryCodeSnapshot]
    extension_runtime: Mapping[str, RegistryExtensionRuntimeSnapshot]
    generations: Mapping[tuple[str, ...], RegistryGenerationSnapshot]
    source_lifecycle_token: tuple[str, int]
    _owner: object = field(repr=False, compare=False)
    _source_identities: tuple[Source, ...] = field(repr=False, compare=False)
    _fingerprint: tuple = field(repr=False, compare=False)

    @property
    def configuration_names(self) -> tuple[str, ...]:
        return tuple(self.configurations)

    def extension_names(self, configuration: str) -> tuple[str, ...]:
        prefix = source_ids.extension_prefix(configuration)
        legacy = {
            source_id[len(prefix):]
            for source_id, source in self.sources.items()
            if source.kind == KIND_EXTENSION and source_id.startswith(prefix)
        }
        native = {
            generation.manifest.identity.extension_name
            for generation in self.generations.values()
            if generation.manifest.identity.source_kind is SourceKind.EXTENSION
            and generation.manifest.identity.parent_configuration == configuration
        }
        return tuple(sorted(legacy | native, key=lambda value: (value.casefold(), value)))

    def generation(
        self, identity: ExportIdentity
    ) -> RegistryGenerationSnapshot | None:
        if not isinstance(identity, ExportIdentity):
            raise TypeError("identity должен быть ExportIdentity")
        return self.generations.get(identity.grouping_key)

    def generation_view(self, configuration: str) -> GenerationView:
        """Вернуть native или legacy view только из этого frozen snapshot."""
        identity = ExportIdentity.configuration(configuration)
        native = self.generation(identity)
        if native is not None:
            return native.view
        loaded = self.configurations.get(configuration)
        if loaded is None:
            raise UnknownConfiguration(f"Конфигурация не загружена: {configuration}.")
        base_source = self.sources.get(loaded.source.id)
        if base_source is None:
            raise RegistryError(
                f"Снимок конфигурации повреждён: {configuration}."
            )
        modules = self.modules.get(source_ids.modules_id(configuration))
        code_source = (
            modules.source
            if modules is not None and modules.ready
            else None
        )
        return legacy_generation_view(
            identity,
            base_sha256=base_source.sha256,
            base_items_total=base_source.items_total,
            code_sha256=code_source.sha256 if code_source else "",
            code_items_total=code_source.items_total if code_source else 0,
        )


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
    extension_runtime: LoadedExtensionRuntime | None = None
    roles: LoadedRoleAccess | None = None
    extension_roles: LoadedRoleAccess | None = None
    extension_resolution: ExtensionResolution | None = None
    platform_override: str = ""

    @property
    def name(self) -> str:
        return self.configuration.config.name if self.configuration else ""

    @property
    def platform(self) -> str:
        if self.configuration is None:
            return ""
        return self.platform_override or self.configuration.config.platform

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
                notes.append(
                    "Конфигурация не загружена — фильтрация по версии платформы "
                    "выключена. В выдаче есть всё, что описано в справке "
                    f"{self.syntax.source.platform}, включая методы, которых "
                    "может не быть в вашей версии."
                )
            return notes

        config = self.configuration.config

        if not parse_version(self.platform):
            notes.append(
                "Фактическая версия платформы неизвестна: фильтрация синтаксиса "
                "по версии выключена, доступность методов в вашей базе не подтверждена. "
                "Загрузите Source A из работающей информационной базы."
            )
        if config.truncated:
            notes.append(
                "Выгрузка конфигурации неполная — сделана с ограничением числа "
                "объектов. Ответы могут быть неверными."
            )
        if not config.predefined_available:
            notes.append(
                "Сведения о предопределённых элементах не получены: имена вида "
                "`Справочники.X.Y` проверить нечем. Пустой список не доказывает "
                "отсутствие элементов в базе."
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
