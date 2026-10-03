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
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
import weakref
import zipfile
from collections import Counter
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, TypeVar

try:  # pragma: no cover - на Windows межпроцессного flock нет
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from . import coverage_log, index_cache, modules_index, source_ids, structure_origin
from .dictionary import Dictionary, DictionaryError
from .extension_runtime import (
    ExtensionRuntimeError,
    load_extension_runtime,
)
from .graph import Graph
from .intake_v2 import (
    CandidateTransport,
    ExportIdentity,
    GenerationManifest,
    LayerKind,
    LayerManifest,
    LayerProvenance,
    LayerSourceProfile,
    LayerState,
    RecoveryAction,
    RecoveryPhase,
    RecoveryRecord,
    SourceKind,
    decide_recovery,
)
from .source_modes import (
    ActivationComponent,
    ActivationManifest,
    ActivationMode,
    PlatformDeclaration,
)
from .intake_v2_converter import base_layer_data
from .intake_v2_registry import (
    BundleStoreError,
    GenerationBundleStore,
    GenerationPointer,
    GenerationView,
    LayerPayload,
    LayerPayloadSource,
    RecoveryBlocked,
    StagedGeneration,
    hash_layer_payload,
    hash_layer_semantic,
    legacy_generation_view,
    load_layer_payload,
    native_generation_view,
)
from .source_modes import (
    ActivationClassification,
    ActivationStatus,
    classify_activation,
)
from .intake_v2_extensions import (
    ExtensionRelation,
    ExtensionResolutionError,
    ExtensionStructure,
    resolve_extension_relation_map,
    resolve_extension_relations,
    resolve_extension_structure,
)
from .intake_v2_runtime import (
    GenerationRuntimeError,
    NativeGenerationRuntime,
    build_generation_runtime,
    configuration_from_base_layer,
)
from .loader import ExportError, load
from .model import Configuration
from .module_catalog import catalog_files
from .module_content import LocatorIdentity
from .resource_limits import ResourceLimitError
from .role_access import LoadedRoleAccess
from .search_keys import coverage as search_keys_coverage
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
from .syntax_parser import parse_hbk
from .standard_attributes import (
    StandardAttributeError,
    materialize_standard_attributes,
)
from .virtual_tables import build_table_index
from .v8container import V8ContainerError
# Типы-данные живут в registry_types; имена реэкспортируются отсюда, чтобы
# `from mcp1c.registry import X` у потребителей не менялся. `SourcesChanged`
# сам реестр не бросает — его импортируют tools и дашборд, поэтому pyflakes
# видит его неиспользуемым здесь.
from .registry_types import (
    KIND_CONFIGURATION,
    KIND_SYNTAX,
    KIND_MODULES,
    KIND_EXTENSION,
    KIND_EXTENSION_RUNTIME,
    STATUS_LOADING,
    STATUS_READY,
    STATUS_ERROR,
    INCOMPLETE_CONFIGURATION_WARNING,
    RELATION_EXACT,
    RELATION_NEWER,
    RELATION_OLDER,
    RELATION_NONE,
    RELATION_UNKNOWN,
    RegistryError,
    SourceInUseError,
    ReloadRequired,
    ConfigurationNotLoaded,
    UnknownConfiguration,
    AmbiguousConfiguration,
    GenerationPublishConflict,
    RegistryRecoveryBlocked,
    RegistryChangedExternally,
    RegistryOwnedElsewhere,
    Source,
    LoadedConfiguration,
    LoadedExtensionRuntime,
    LoadedSyntax,
    LoadedModules,
    SourcesChanged,
    SourceSnapshot,
    RegistryCodeSnapshot,
    RegistryExtensionRuntimeSnapshot,
    RegistryGenerationSnapshot,
    RegistrySnapshot,
    ResolvedContext,
)

REGISTRY_VERSION = 1
_EXPECTED_GENERATION_UNSET = object()
# registry.json ещё не читался этим экземпляром: сверять не с чем.
_REGISTRY_STAMP_UNSEEN = object()

# Владение каталогом данных — свойство процесса, а не экземпляра Registry:
# сервер держит data/ всё время работы, а второй процесс (CLI внутри
# контейнера) должен это видеть. Несколько Registry одного процесса делят
# один flock; он отпускается, когда исчезает последний из них.
_OWNERSHIP_GUARD = threading.Lock()
_OWNERSHIP: dict[Path, list[int]] = {}


def _open_lock_file(path: Path) -> int:
    """Открыть файл замка: сначала на запись, при чужом файле — на чтение.

    На NFS flock(LOCK_EX) по дескриптору только для чтения даёт EBADF, поэтому
    основной путь — O_RDWR. Файл, созданный root с 0644, сервер под uid 10001
    открыть на запись не может: тогда O_RDONLY, локально flock это допускает.
    Права выставляются fchmod после создания: umask 077 иначе запер бы файл
    для процесса другого пользователя. Именно 0644, а не 0640: файл, первым
    созданный root-CLI (`docker compose exec -u root`) с группой root, сервер
    под uid 10001 иначе не открыл бы вовсе и не стал бы владельцем.

    `O_NOFOLLOW` и проверка обычного файла не дают подложенному в `data/`
    симлинку направить fchmod на чужой файл и открыть его на чтение всем;
    fchmod делается только для своего файла.
    """
    # O_NONBLOCK: FIFO на месте замка иначе подвесил бы open до fstat.
    flags = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | flags, 0o644)
    except PermissionError:
        descriptor = os.open(path, os.O_RDONLY | flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(
                errno.EINVAL, "файл замка не является обычным файлом", str(path)
            )
        if info.st_uid == os.geteuid():
            try:
                os.fchmod(descriptor, 0o644)
            except OSError:
                # Том только на чтение и т.п.: сам замок от прав не зависит.
                pass
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


# Ошибки, по которым flock на томе не работает вовсе (NFS без lockd, SMB,
# FUSE): тогда остаётся только предупредить. Любая другая ошибка — повод
# не считать себя владельцем, а не «работать без замка» вдвоём.
_LOCK_UNSUPPORTED_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, "ENOLCK", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "ENOSYS", None),
    )
    if code is not None
)


def _lock_unsupported(error: OSError, path: Path) -> None:
    logger.warning(
        "flock недоступен для %s (%s): работаем без межпроцессной защиты "
        "каталога данных. Не запускайте второй писатель одновременно.",
        path,
        error,
    )


def _acquire_ownership(data_dir: Path) -> bool:
    """Взять эксклюзивный flock каталога; False — владельцем быть нельзя.

    ``BlockingIOError`` означает «замок занят». ENOLCK, EOPNOTSUPP и ENOSYS
    говорят, что flock здесь не работает: единственный сервер на таком томе
    не должен из-за этого стать гостем. Отказ открыть файл замка и прочие
    ошибки (EBADF, EACCES) владения не дают: иначе два процесса сочли бы
    себя владельцами «без замка».
    """
    if fcntl is None:
        return True
    key = data_dir.resolve()
    with _OWNERSHIP_GUARD:
        held = _OWNERSHIP.get(key)
        if held is not None:
            held[1] += 1
            return True
        key.mkdir(parents=True, exist_ok=True)
        path = key / ".registry.owner.lock"
        try:
            descriptor = _open_lock_file(path)
        except OSError as error:
            logger.warning(
                "Не удалось открыть замок каталога данных %s (%s): процесс "
                "работает без права владельца.",
                path,
                error,
            )
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return False
        except OSError as error:
            os.close(descriptor)
            if error.errno in _LOCK_UNSUPPORTED_ERRNOS:
                _lock_unsupported(error, path)
                _OWNERSHIP[key] = [None, 1]
                return True
            logger.warning(
                "flock замка каталога данных %s отказал (%s): процесс "
                "работает без права владельца.",
                path,
                error,
            )
            return False
        _OWNERSHIP[key] = [descriptor, 1]
        return True


def _release_ownership(key: Path) -> None:
    with _OWNERSHIP_GUARD:
        held = _OWNERSHIP.get(key)
        if held is None:
            return
        held[1] -= 1
        if held[1] <= 0:
            _OWNERSHIP.pop(key, None)
            if held[0] is not None:
                os.close(held[0])


# Сколько ждать короткий замок записи registry.json, прежде чем отказать.
_REGISTRY_FILE_LOCK_TIMEOUT = 10.0
_EXPECTED_GENERATION_VIEW_UNSET = object()

_DictionaryMutationResult = TypeVar("_DictionaryMutationResult")

logger = logging.getLogger(__name__)

_RE_PLATFORM = re.compile(r"\b(\d+\.\d+\.\d+(?:\.\d+)?)\b")


def _protect_source_operation(method):
    @wraps(method)
    def protected(self, path, *args, **kwargs):
        with self._source_file_operation(Path(path)):
            return method(self, path, *args, **kwargs)
    return protected


def _refresh_on_external_change(*, retry: bool = True):
    """Перечитать registry.json, если его сменил второй процесс (CLI).

    У дашборда нет своей ручки reload: без этого после любой записи гостя
    все записи сервера отказывали бы до ручного POST /admin/reload. Перед
    операцией владелец сверяет файл и при расхождении выполняет тот же
    reload; если файл сменился уже во время операции — reload и один повтор.
    Публикация поколения не повторяется: её staging к этому моменту откачен.
    """

    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            if not self._may_refresh():
                return method(self, *args, **kwargs)
            self._refresh_scope.depth = 1
            try:
                self._refresh_if_changed_externally()
                try:
                    return method(self, *args, **kwargs)
                except RegistryChangedExternally:
                    if not retry:
                        raise
                    logger.info(
                        "registry.json изменён другим процессом во время %s: "
                        "перечитываем и повторяем",
                        method.__name__,
                    )
                    self.startup()
                    return method(self, *args, **kwargs)
            finally:
                self._refresh_scope.depth = 0

        return wrapped

    return decorate


class _NativeGenerationActive(RegistryError):
    """Bootstrap не заменяет schema-v1 файлом уже активное native-поколение."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _временный_сбой(error: BaseException) -> bool:
    """Отказ среды (ввод-вывод, память), а не дефект самого источника.

    Идём только по явным цепочкам ``from``: неявный ``__context__`` цепляет
    любой OSError, пойманный по дороге (например, при открытии кэша), и
    битый ZIP выглядел бы временным сбоем, который вечно переносится.
    """
    текущая: BaseException | None = error
    просмотрено: set[int] = set()
    while текущая is not None and id(текущая) not in просмотрено:
        просмотрено.add(id(текущая))
        if isinstance(текущая, FileNotFoundError):
            # Пропавший файл — окончательный вердикт, как и проверка
            # `stored.exists()` до вызова: переносить строку незачем.
            return False
        if isinstance(текущая, (OSError, MemoryError)):
            return True
        текущая = текущая.__cause__
    return False


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


def _combined_sha256(sources: Iterable["Source"]) -> str:
    """Штамп набора справок: слитый вид зависит от всех, а не от последней."""
    digest = hashlib.sha256()
    for source in sorted(sources, key=lambda s: s.id):
        digest.update(f"{source.id}:{source.sha256}|".encode())
    return digest.hexdigest()


def _platform_from_path(path: Path) -> str:
    """Версия платформы из пути: data/hbk/8.3.27.2130/shcntx_ru.hbk."""
    for part in (path.name, *reversed(path.parts[:-1])):
        match = _RE_PLATFORM.search(part)
        if match:
            return match.group(1)
    return ""


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


@dataclass(frozen=True, slots=True)
class _PreparedNativeRuntime:
    """Полностью построенный пакет до durable pointer switch."""

    runtime: NativeGenerationRuntime
    configuration: LoadedConfiguration | None
    module_source: Source | None
    module_indices: modules_index.Индексы | None
    parent_configuration: LoadedConfiguration | None = None
    extension_relations: tuple[ExtensionRelation, ...] = ()
    # Какие кэши поднялись с диска как есть ("objects", "fields",
    # "modules"). Их не переписывают: reload иначе перезаписывал все файлы
    # индексов, из которых только что всё поднял.
    cached_kinds: frozenset[str] = frozenset()


class Registry:
    """Всё загруженное и правила сопоставления версий."""

    def __init__(self, data_dir: str | Path = "data"):
        # Внутренние staging-пути строятся абсолютными методами Path. Нормализуем
        # корень сразу, чтобы проверка границы bundle не сравнивала абсолютный
        # путь с относительным ``data`` и не отклоняла штатную публикацию.
        self.data_dir = Path(data_dir).resolve()
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
        self._generation_store = GenerationBundleStore(self.data_dir)
        self.generation_recovery_path = self._generation_store.recovery_path
        # Write-ahead marker рокировки каталога кода. Новых рокировок нет:
        # приём кода отдельно от структуры удалён, код приходит только
        # native-поколением. Startup ещё дочитывает marker, оставленный
        # процессом прежней версии (окно совместимости, удалить после
        # выпуска 4.3.0 вместе с `_восстановить_рокировку_кода`).
        self._module_swap_path = self.data_dir / ".modules-swap.json"
        self.dictionary_path = self.data_dir / "dictionary.json"
        # Битый словарь не должен останавливать ни сервер, ни CLI: поиск
        # работает на встроенном слое, причина называется в сообщениях
        # startup, а запись закрыта, пока человек не исправит файл.
        self._dictionary_error = ""
        try:
            self.dictionary = Dictionary.load(self.dictionary_path)
        except DictionaryError as error:
            self.dictionary = Dictionary(path=self.dictionary_path)
            self._dictionary_error = str(error)
        # Таблица синонимов действующего словаря. Меняется только под
        # `_lock` вместе с `self.dictionary`; каждая вставка индекса в карты
        # реестра берёт её под тем же замком. Иначе индекс, собранный до
        # правки словаря и опубликованный после неё, жил бы со старой.
        self._synonyms = self.dictionary.synonyms()

        self._lock = threading.RLock()
        self._source_reservations: Counter[Path] = Counter()
        self._source_scopes = threading.local()
        # restore() поднимает уже записанные строки теми же add_*, что и
        # пользовательская загрузка. Fail-closed проверка журналов отличает
        # их по этому признаку потока, а не по параметрам вызова.
        self._restore_scope = threading.local()
        # Гость — процесс, запущенный при живом владельце data/ (сервере).
        # Он не разбирает чужие журналы, не подметает и не публикует
        # поколения: только читает registry.json и дописывает строки под
        # коротким flock с проверкой, что файл не сменился с чтения.
        self._guest = False
        self._owns_data_dir = False
        self._registry_stamp: object = _REGISTRY_STAMP_UNSEEN
        # Текст registry.json, от которого гость считает свои изменения.
        self._registry_read_text: str | None = None
        self._file_lock_warned = False
        self._refresh_scope = threading.local()
        self._startup_scope = threading.local()
        # Staging разных кандидатов может идти параллельно, но pointer и общий
        # WAL переключаются только одним publisher за раз. Startup/reload
        # держит этот замок не целиком, а на двух участках: на recovery WAL
        # с уборкой временных баз schema v1 (иначе живой WAL идущей
        # публикации прочитался бы как аварийный и её staging откатился) и на
        # native-части restore — от чтения registry.json до применения
        # pointers (иначе прочитанные до долгой сборки pointers легли бы
        # поверх параллельной публикации или remove). Legacy-строки restore
        # поднимает уже без него. Замок реентерабельный: restore поднимает
        # строки через add_configuration, который берёт его сам. Порядок:
        # startup -> mutation -> cache -> registry.
        self._generation_mutation_lock = threading.RLock()
        # Версии нужны только для незавершённых публикаций внутри процесса.
        # После удаления счётчик сохраняется: пусто -> B -> пусто тоже смена
        # состояния, которую не обнаружить сравнением одних pointers.
        self._configuration_revisions: dict[str, int] = {}
        # Та же монотонная ревизия для справок и снимков расширений: restore
        # по ней узнаёт, что строку сняли или заменили после чтения
        # registry.json. Счётчик переживает remove по той же причине.
        self._source_revisions: dict[str, int] = {}
        # Маркер UI переживает remove -> add даже при повторном использовании id().
        self._source_lifecycle_nonce = uuid.uuid4().hex
        self._source_lifecycle_revision = 0
        self._generation_pointers: dict[tuple[str, ...], GenerationPointer] = {}
        self._generation_manifests: dict[
            tuple[str, ...], GenerationManifest
        ] = {}
        self._generation_recovery_blocked = False
        # registry.json другой версии формата нельзя ни прочитать, ни молча
        # заменить пустым состоянием: это чужие (обычно более новые) данные.
        self._registry_version_blocked = ""
        # Прежний вердикт «заблокировано» держится до конца restore, даже
        # если журнал уже разобран: память в это время ещё неполна, и
        # параллельный save() из дашборда записал бы её вместо registry.json.
        # Поток самого restore сквозь удержание проходит.
        self._restore_hold = ""
        self._platform_declarations: dict[str, PlatformDeclaration] = {}
        # Правка словаря — одна операция от изменения объекта до публикации
        # перечитанных таблиц. Обычный `_lock` на дисковой записи держать нельзя,
        # поэтому writers сериализуются отдельным реентерабельным замком.
        self._dictionary_mutation_lock = threading.RLock()
        # Повторные startup/reload сериализуются отдельно: долгий reload не
        # держит основной замок и не мешает `resolve()`, но два восстановления
        # не могут одновременно резервировать одну фоновую сборку.
        self._startup_lock = threading.Lock()
        # Сервер вызывает restore() до обслуживания запросов. Синтетические
        # сборщики могут создавать старые индексы до этой границы, но работающий
        # Registry не выдаёт смешанный вид A+код.
        self._restored_for_serving = False
        # Фоновая сборка может закончиться раньше последовательного restore.
        # Поколения различают успешно завершённый полный startup и аварийно
        # оборванный частичный снимок: второй нельзя писать в registry.json.
        self._startup_generation = 0
        self._last_successful_startup_generation = 0
        # Все writers кэша модулей и смена поколения источника идут через
        # один порядок `cache -> registry`. Так старый writer не успевает
        # записать свой пакет после смены поколения кода.
        self._modules_cache_lock = threading.Lock()
        self.configurations: dict[str, LoadedConfiguration] = {}
        # Справки версий — учётными записями, а не содержимым: разобранная
        # справка весит около 60 МБ, и держать их все ради редких пересборок
        # значит платить памятью, кратной числу версий. Содержимое лежит на
        # диске и поднимается на время сборки слитого вида.
        self.syntax_versions: dict[str, Source] = {}
        self.syntax: LoadedSyntax | None = None
        self.sources: dict[str, Source] = {}
        # Runtime-снимок намеренно не входит ни в schema v1, ни в индекс
        # кода: это состояние конкретного сеанса и области данных на момент
        # выгрузки. На конфигурацию публикуется ровно одно последнее поколение.
        self.extension_runtime: dict[str, LoadedExtensionRuntime] = {}
        # Индекс кода — по id источника (`<Имя>:modules` и `<Имя>:ext:<Имя>`),
        # тот же ключ, что и в `self.sources`: у конфигурации ровно одна
        # выгрузка кода, у расширений их может быть несколько, и `resolve()`
        # достаёт нужную запись напрямую по составленному ключу.
        self.modules: dict[str, LoadedModules] = {}
        # Ролевой provider принадлежит тому же native generation, но его
        # большой прямой/обратный индекс остаётся файловой SQLite, а не
        # коллекцией Python-объектов в памяти.
        self.roles: dict[str, LoadedRoleAccess] = {}
        # Native extension snapshot хранит структуру и роли отдельно от базы.
        # Проверенные hard edges пересчитываются при смене базы одним проходом;
        # merged Configuration строится только для явно выбранного расширения.
        self.extension_structures: dict[str, ExtensionStructure] = {}
        self.extension_relations: dict[str, tuple[ExtensionRelation, ...]] = {}
        self.extension_roles: dict[str, LoadedRoleAccess] = {}
        # Сильная ссылка нужна не потоку (он живёт сам), а повторному
        # `startup()`: пока предыдущая сборка ещё идёт, reload не должен
        # запускать второй разбор того же корпуса и удваивать расход памяти.
        self._module_builds: dict[str, threading.Thread] = {}
        self._modules_generation: dict[str, int] = {}
        # Диагностика пишется при публикации поколения, а не на каждом запросе.
        # Множество также страхует повторный путь публикации от дубля строки.
        self._logged_module_limitations: set[tuple[str, int, str]] = set()
        self._module_recovery_blocked = False
        # Строки registry.json, которые restore не поднял из-за временного
        # сбоя (OSError, нехватка памяти). Они не видны в памяти, но
        # переносятся в каждую запись реестра дословно: иначе первый же
        # save() после неудачного старта навсегда снял бы источник с учёта.
        self._carried_source_rows: dict[str, dict] = {}
        # Соотношение версий не меняется, пока не сменились источники, а его
        # вычисление — перебор всех элементов справки. Без кэша это давало
        # 16 мс на каждый вызов любого инструмента.
        self._relation_cache: dict[
            str,
            tuple[LoadedConfiguration, LoadedSyntax | None, str, str, int],
        ] = {}

    def _snapshot_fingerprint_locked(self) -> tuple:
        """Дешёвый CAS-маркер всех полей, видимых через ``snapshot``."""
        generation_rows = tuple(
            (
                key,
                pointer,
                self._generation_manifests.get(key),
            )
            for key, pointer in sorted(self._generation_pointers.items())
        )
        source_rows = tuple(
            (source.id, id(source), SourceSnapshot.capture(source))
            for source in sorted(self.sources.values(), key=lambda item: item.id)
        )
        module_rows = tuple(
            (
                source_id,
                id(loaded),
                id(loaded.source),
                SourceSnapshot.capture(loaded.source),
                loaded.готов,
                loaded.этап,
                loaded.название_этапа,
                loaded.прогресс,
            )
            for source_id, loaded in sorted(self.modules.items())
        )
        runtime_rows = tuple(
            (
                name,
                id(loaded),
                id(loaded.source),
                SourceSnapshot.capture(loaded.source),
                loaded.snapshot,
            )
            for name, loaded in sorted(self.extension_runtime.items())
        )
        syntax_version_rows = tuple(
            (
                platform,
                id(source),
                SourceSnapshot.capture(source),
            )
            for platform, source in sorted(self.syntax_versions.items())
        )
        return (
            tuple(
                (name, id(loaded))
                for name, loaded in sorted(self.configurations.items())
            ),
            id(self.syntax) if self.syntax is not None else None,
            syntax_version_rows,
            source_rows,
            module_rows,
            runtime_rows,
            generation_rows,
        )

    def snapshot(self) -> RegistrySnapshot:
        """Снять все публично читаемые карты и состояния одним поколением."""
        with self._lock:
            configurations = dict(sorted(self.configurations.items()))
            source_objects = tuple(
                sorted(self.sources.values(), key=lambda item: item.id)
            )
            sources = {
                source.id: SourceSnapshot.capture(source)
                for source in source_objects
            }
            syntax_versions = {
                platform: SourceSnapshot.capture(source)
                for platform, source in sorted(self.syntax_versions.items())
            }
            loaded_modules = dict(sorted(self.modules.items()))
            runtime = {
                name: RegistryExtensionRuntimeSnapshot(
                    source=SourceSnapshot.capture(loaded.source),
                    snapshot=loaded.snapshot,
                )
                for name, loaded in sorted(self.extension_runtime.items())
            }
            if set(self._generation_pointers) != set(self._generation_manifests):
                raise RegistryError(
                    "runtime Registry содержит несогласованные generation maps"
                )
            generations = {
                key: RegistryGenerationSnapshot(
                    pointer,
                    self._generation_manifests[key],
                )
                for key, pointer in sorted(self._generation_pointers.items())
            }
            code_ids = sorted(
                {
                    source.id
                    for source in source_objects
                    if source.kind in (KIND_MODULES, KIND_EXTENSION)
                }
                | set(loaded_modules)
            )
            modules: dict[str, RegistryCodeSnapshot] = {}
            source_by_id = {source.id: source for source in source_objects}
            for source_id in code_ids:
                loaded = loaded_modules.get(source_id)
                source = (
                    loaded.source
                    if loaded is not None
                    else source_by_id.get(source_id)
                )
                source_snapshot = (
                    SourceSnapshot.capture(source) if source is not None else None
                )
                modules[source_id] = RegistryCodeSnapshot(
                    source=source_snapshot,
                    loaded=loaded,
                    ready=loaded.готов if loaded is not None else False,
                    status=source.status if source is not None else "",
                    error=source.error if source is not None else "",
                    stage=loaded.этап if loaded is not None else (0, 0),
                    stage_title=(
                        loaded.название_этапа if loaded is not None else ""
                    ),
                    progress=loaded.прогресс if loaded is not None else (0, 0),
                )
            # Тот же вызов, что и в `snapshot_is_current`, под тем же `_lock`:
            # вторая копия набора полей разошлась бы с первой молча при
            # добавлении поля — вечные отказы CAS или пропуск устаревшего.
            fingerprint = self._snapshot_fingerprint_locked()
            return RegistrySnapshot(
                configurations=MappingProxyType(configurations),
                syntax_versions=MappingProxyType(syntax_versions),
                syntax=self.syntax,
                sources=MappingProxyType(sources),
                modules=MappingProxyType(modules),
                extension_runtime=MappingProxyType(runtime),
                generations=MappingProxyType(generations),
                source_lifecycle_token=(
                    self._source_lifecycle_nonce,
                    self._source_lifecycle_revision,
                ),
                _owner=self,
                # Сильные ссылки исключают ABA через повторное использование
                # Python ``id`` после remove/re-add между snapshot и CAS.
                _source_identities=source_objects,
                _fingerprint=fingerprint,
            )

    def has_ready_roles(self) -> bool:
        """Есть ли хотя бы один готовый role-provider в текущем runtime."""
        with self._lock:
            return any(roles.ready for roles in self.roles.values())

    def snapshot_is_current(self, snapshot: RegistrySnapshot) -> bool:
        """Проверить поздний CAS без раскрытия ``_lock`` потребителю."""
        if snapshot._owner is not self:
            return False
        with self._lock:
            return snapshot._fingerprint == self._snapshot_fingerprint_locked()

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
    CACHE_KINDS = {
        KIND_CONFIGURATION: ("objects", "fields"),
        KIND_SYNTAX: ("syntax", "lookup"),
        # Четыре структуры — четыре имени, не одно общее. `sweep` считает
        # своим только то, что названо здесь; одно общее имя на все четыре
        # значило бы, что три индекса из четырёх
        # сносятся на первом же старте, молча, и пересобираются заново
        # каждый раз (`tests/test_index_cache_modules.py`).
        KIND_MODULES: ("modules-toc", "modules-calls", "modules-forms", "modules-search"),
        # Тот же набор видов, что у модулей конфигурации: провайдер поиска
        # по коду не должен различать их источники. Без этой записи `sweep`
        # счёл бы кэш расширения ничьим на первом же старте.
        KIND_EXTENSION: ("modules-toc", "modules-calls", "modules-forms", "modules-search"),
    }

    def _cache_path(self, source_id: str, kind: str) -> Path:
        return index_cache.path_for(self.cache_dir, source_id, kind)

    def _carried_rows_locked(self) -> list[tuple[str, str, str]]:
        """``(id, kind, stored_path)`` строк, перенесённых временным сбоем.

        Вызывается под ``_lock``. Такие строки не видны в ``self.sources``,
        но их файлы, кэш и разобранная справка принадлежат реестру: любая
        уборка, считающая «заявленное» только по памяти, удалила бы их.
        """
        rows: list[tuple[str, str, str]] = []
        for source_id, raw in self._carried_source_rows.items():
            if source_id in self.sources:
                continue
            kind = raw.get("kind")
            stored = raw.get("stored_path")
            rows.append((
                source_id,
                kind if isinstance(kind, str) else "",
                stored if isinstance(stored, str) else "",
            ))
        return rows

    def _carried_stored_paths(self) -> set[Path]:
        """Разрешённые пути файлов перенесённых строк."""
        with self._lock:
            stored = [path for _, _, path in self._carried_rows_locked() if path]
        result: set[Path] = set()
        for path in stored:
            try:
                result.add(self._absolute(path).resolve())
            except (OSError, RegistryError, ValueError):
                continue
        return result

    def _cached_names(self) -> set[str]:
        """Имена файлов, которые кэшу разрешено иметь прямо сейчас."""
        names = {
            self._cache_path(source.id, kind).name
            for source in self.sources.values()
            for kind in self.CACHE_KINDS.get(source.kind, ())
        }
        with self._lock:
            names.update(
                self._cache_path(source_id, cache_kind).name
                for source_id, kind, _ in self._carried_rows_locked()
                for cache_kind in self.CACHE_KINDS.get(kind, ())
            )
        # Native runtime не дублирует Source в registry.json: его identity и
        # hashes уже находятся в generation manifest. Расходный кэш всё же
        # принадлежит активному поколению и не должен сноситься sweep после
        # успешного restore.
        for manifest in self._generation_manifests.values():
            layers = {layer.kind: layer for layer in manifest.layers}
            if manifest.identity.source_kind is SourceKind.EXTENSION:
                source_id = source_ids.extension_modules_id(
                    manifest.identity.parent_configuration,
                    index_cache.safe_name(manifest.identity.extension_name),
                )
                if any(
                    layers.get(kind) is not None
                    and layers[kind].state is LayerState.READY
                    for kind in (LayerKind.CODE, LayerKind.FORMS)
                ):
                    names.update(
                        self._cache_path(source_id, kind).name
                        for kind in self.CACHE_KINDS[KIND_EXTENSION]
                    )
                roles = layers.get(LayerKind.ROLES)
                if roles is not None and roles.state is LayerState.READY:
                    names.add(self._cache_path(source_id, "roles.sqlite").name)
                continue
            configuration = manifest.identity.configuration_name
            base = layers.get(LayerKind.BASE_STRUCTURE)
            if base is not None and base.state is LayerState.READY:
                names.update(
                    self._cache_path(configuration, kind).name
                    for kind in self.CACHE_KINDS[KIND_CONFIGURATION]
                )
            if any(
                layers.get(kind) is not None
                and layers[kind].state is LayerState.READY
                for kind in (LayerKind.CODE, LayerKind.FORMS)
            ):
                source_id = source_ids.modules_id(configuration)
                names.update(
                    self._cache_path(source_id, kind).name
                    for kind in self.CACHE_KINDS[KIND_MODULES]
                )
            roles = layers.get(LayerKind.ROLES)
            if roles is not None and roles.state is LayerState.READY:
                names.add(self._cache_path(configuration, "roles.sqlite").name)
        return names

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

    def _configuration_index(
        self,
        config: Configuration,
        source: Source,
        *,
        write_cache: bool = True,
        cache_sha256: str = "",
        cache_hits: set[str] | None = None,
    ) -> SearchIndex:
        path = self._cache_path(source.id, "objects")
        synonyms = self.dictionary.synonyms()
        aliases = self.dictionary.aliases_for(config.name)
        identity = cache_sha256 or source.sha256

        cached = index_cache.load(
            path,
            config.objects,
            source_sha256=identity,
            kind="objects",
            synonyms=synonyms,
            aliases=aliases,
        )
        if cached is not None:
            if cache_hits is not None:
                cache_hits.add("objects")
            return cached

        index = index_configuration(config, synonyms=synonyms, aliases=aliases)
        if write_cache:
            index_cache.save(index, path, source_sha256=identity, kind="objects")
        return index

    def _field_index(
        self,
        config: Configuration,
        source: Source,
        *,
        write_cache: bool = True,
        cache_sha256: str = "",
        cache_hits: set[str] | None = None,
    ) -> SearchIndex:
        path = self._cache_path(source.id, "fields")
        synonyms = self.dictionary.synonyms()
        identity = cache_sha256 or source.sha256

        # Полезная нагрузка собирается только когда есть что поднимать: на
        # промахе она всё равно построится внутри index_fields.
        if path.exists():
            payloads = {ref.full_name: ref for ref in iter_field_refs(config)}
            cached = index_cache.load(
                path,
                payloads,
                source_sha256=identity,
                kind="fields",
                synonyms=synonyms,
            )
            if cached is not None:
                if cache_hits is not None:
                    cache_hits.add("fields")
                return cached

        index = index_fields(config, synonyms=synonyms)
        if write_cache:
            index_cache.save(index, path, source_sha256=identity, kind="fields")
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

    @staticmethod
    def _source_file_keys(path: Path) -> set[Path]:
        # Нужны оба имени: symlink-вход может читать managed source, а rename
        # сохранённой копии заменяет саму ссылку, оставляя обычный файл.
        return {Path(os.path.abspath(path)), path.resolve()}

    @contextmanager
    def _source_file_operation(self, path: Path):
        previous = getattr(self._source_scopes, "paths", None)
        paths: set[Path] = set()
        self._source_scopes.paths = paths
        try:
            self._reserve_source_file(path)
            yield
        finally:
            with self._lock:
                for reserved in paths:
                    self._source_reservations[reserved] -= 1
                    if not self._source_reservations[reserved]:
                        del self._source_reservations[reserved]
            self._source_scopes.paths = previous

    def _reserve_source_file(self, path: Path) -> None:
        paths = self._source_scopes.paths
        keys = self._source_file_keys(path)
        with self._lock:
            for key in keys - paths:
                self._source_reservations[key] += 1
                paths.add(key)

    def _store_source(
        self,
        path: Path,
        subdir: str,
        *,
        source_id: str,
        digest: str,
    ) -> Path:
        """Атомарно сохранить исходник по его ID и содержимому.

        Basename принадлежит каталогу пользователя и не является личностью
        источника: два разных архива ``same.zip`` раньше затирали друг друга,
        а после restart одна конфигурация тихо исчезала. Хеш ID разделяет
        разные источники, хеш содержимого — их перевыгрузки.

        Каталоги открываются через ``dir_fd`` и ``O_NOFOLLOW``, временный файл
        создаётся эксклюзивно, а итоговое имя заменяется rename. Поэтому ни
        каталог, ни заранее подложенная ссылка с итоговым именем не выводят
        запись за пределы ``data/sources``.
        """
        if not re.fullmatch(r"[a-z0-9-]+", subdir):
            raise RegistryError(f"Недопустимый каталог сохранённых источников: {subdir}")

        identity = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
        suffix = path.suffix if re.fullmatch(r"\.[A-Za-z0-9]{1,12}", path.suffix) else ".source"
        target = f"source-{identity}-{digest}{suffix}"
        temporary = f".{target}.{uuid.uuid4().hex}.tmp"
        target_dir = self.sources_dir / subdir
        # Резерв живёт до завершения всей add-операции, а не только копирования.
        # Временный файл также не должен исчезнуть между open и rename.
        self._reserve_source_file(target_dir / target)
        self._reserve_source_file(target_dir / temporary)
        opened: list[int] = []
        temporary_created = False

        def open_child(parent_fd: int, name: str) -> int:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            return os.open(name, flags, dir_fd=parent_fd)

        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            data_fd = os.open(
                self.data_dir,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            opened.append(data_fd)
            sources_fd = open_child(data_fd, "sources")
            opened.append(sources_fd)
            target_fd = open_child(sources_fd, subdir)
            opened.append(target_fd)

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            file_fd = os.open(temporary, flags, 0o600, dir_fd=target_fd)
            temporary_created = True
            copied = hashlib.sha256()
            # Дескриптор сразу переходит во владение file-объекта: при
            # отказе открыть исходник он закрывается вместе с ним, а не
            # утекает до конца процесса.
            try:
                target_file = os.fdopen(file_fd, "wb", closefd=True)
            except BaseException:
                os.close(file_fd)
                raise
            with target_file as target_stream, path.open("rb") as source_stream:
                for block in iter(lambda: source_stream.read(1 << 20), b""):
                    copied.update(block)
                    target_stream.write(block)
                target_stream.flush()
                os.fsync(target_stream.fileno())

            if copied.hexdigest() != digest:
                raise RegistryError(
                    f"{path.name}: исходник изменился во время сохранения."
                )

            os.replace(
                temporary,
                target,
                src_dir_fd=target_fd,
                dst_dir_fd=target_fd,
            )
            temporary_created = False
            os.fsync(target_fd)
        except OSError as error:
            raise RegistryError(
                f"{path.name}: не удалось безопасно сохранить исходник: {error}"
            ) from error
        finally:
            if temporary_created and opened:
                try:
                    os.unlink(temporary, dir_fd=opened[-1])
                except FileNotFoundError:
                    pass
            for descriptor in reversed(opened):
                os.close(descriptor)
        return target_dir / target

    def _advance_source_lifecycle_revision(self) -> None:
        """Отметить публикацию или снятие источника под ``_lock``."""
        self._source_lifecycle_revision += 1

    def _advance_source_revision(self, source_id: str) -> None:
        """Вызывается под ``_lock`` при публикации или снятии строки."""
        self._source_revisions[source_id] = (
            self._source_revisions.get(source_id, 0) + 1
        )

    def _check_source_revision(
        self, source_id: str, expected: int | None
    ) -> None:
        """CAS restore-строки; вызывается под ``_lock``."""
        if expected is not None and self._source_revisions.get(
            source_id, 0
        ) != expected:
            raise RegistryError(
                f"Источник «{source_id}» снят или заменён во время "
                "восстановления; строка registry.json не публикуется."
            )

    def _advance_configuration_revision(self, name: str) -> None:
        """Отметить смену базы под ``_lock``, включая удаление и restore."""
        self._configuration_revisions[name] = (
            self._configuration_revisions.get(name, 0) + 1
        )
        self._advance_source_lifecycle_revision()

    @_refresh_on_external_change()
    @_protect_source_operation
    def add_configuration(
        self,
        path: str | Path,
        *,
        keep_source: bool = True,
        known_sha256: str = "",
        expected_id: str = "",
        known_origin: str = "",
        allow_truncated: bool = False,
        source_transport: CandidateTransport = CandidateTransport.LOCAL_FILE,
        replace_native: bool = True,
        expected_revision: int | None = None,
    ) -> Source:
        self._ensure_not_blocked(allow_restore=True)
        source_path = Path(path)
        # При restore сохранённая строка registry.json — ожидание, а не
        # доверенный факт. Файл мог быть подменён или повреждён после save;
        # публиковать его с прежним sha256 значит смешать две личности.
        digest = _sha256(source_path)
        if known_sha256 and digest != known_sha256:
            raise RegistryError(
                f"{source_path.name}: контрольная сумма сохранённого источника "
                "не совпала с registry.json."
            )

        try:
            config = load(source_path, allow_truncated=allow_truncated)
        except ExportError as error:
            raise RegistryError(f"{source_path.name}: {error}") from error

        if not config.name:
            raise RegistryError(f"{source_path.name}: в манифесте нет имени конфигурации.")
        if expected_id and config.name != expected_id:
            raise RegistryError(
                f"{source_path.name}: идентификатор конфигурации «{config.name}» "
                f"не совпал с registry.json («{expected_id}»)."
            )

        identity = ExportIdentity.configuration(config.name)
        with self._lock:
            revision = self._configuration_revisions.get(config.name, 0)
            if expected_revision is not None:
                # Restore сверяет ревизию момента снимка registry.json, а не
                # момента своего вызова: remove, прошедший между ними, иначе
                # был бы незаметен и строка воскресла бы.
                if revision != expected_revision:
                    raise RegistryError(
                        f"Конфигурация «{config.name}» снята или заменена "
                        "во время восстановления; строка registry.json не "
                        "публикуется."
                    )
            previous = self.active_generation_pointer(identity)
            active = self.active_generation(identity)
        if previous is not None and not replace_native:
            raise _NativeGenerationActive(
                f"{source_path.name}: у конфигурации «{config.name}» уже есть "
                "активное native-поколение; файл schema v1 его не заменяет."
            )
        if previous is not None and active is not None:
            return self._replace_native_base(
                config,
                previous=previous,
                active=active,
                origin=known_origin or source_path.name,
                digest=digest,
                transport=source_transport,
            )
        if previous is not None:
            raise ReloadRequired("reload_required")

        try:
            materialize_standard_attributes(config)
        except StandardAttributeError as error:
            raise RegistryError(
                f"{source_path.name}: стандартные реквизиты не построены — {error}"
            ) from error

        stored = (
            self._store_source(
                source_path,
                "configurations",
                source_id=config.name,
                digest=digest,
            )
            if keep_source
            else source_path
        )

        source = Source(
            id=config.name,
            kind=KIND_CONFIGURATION,
            origin=known_origin or source_path.name,
            sha256=digest,
            loaded_at=_now(),
            platform=config.platform,
            status=STATUS_READY,
            warnings=[
                *config.warnings,
                *(
                    [INCOMPLETE_CONFIGURATION_WARNING]
                    if config.truncated
                    else []
                ),
            ],
            items_total=len(config),
            stored_path=self._relative(stored),
            incomplete=config.truncated,
        )

        # Индексы строятся до подмены: наружу не должно попасть полусобранное.
        loaded = LoadedConfiguration(
            source=source,
            config=config,
            graph=Graph(config),
            index=self._configuration_index(config, source),
            field_index=self._field_index(config, source),
        )
        # Тяжёлые индексы готовы вне mutation lock. Проверка и подмена вместе
        # сериализуются с native publish/remove, иначе A затрёт новый runtime,
        # оставив на диске прежний pointer B. Автоповтор изменил бы намерение
        # уже выполняющейся загрузки: конфликт требует явного нового запроса.
        with self._generation_mutation_lock, self._lock:
            if self._configuration_revisions.get(config.name, 0) != revision:
                raise RegistryError(
                    f"Конфигурация «{config.name}» изменилась во время загрузки "
                    "Source A. Повторите загрузку."
                )
            self.configurations[config.name] = self._словарь_конфигурации_locked(
                loaded
            )
            self.sources[source.id] = source
            self._relation_cache.pop(config.name, None)
            self._advance_configuration_revision(config.name)
        return source

    def _replace_native_base(
        self,
        config: Configuration,
        *,
        previous: GenerationPointer,
        active: GenerationManifest,
        origin: str,
        digest: str,
        transport: CandidateTransport,
    ) -> Source:
        """Заменить schema-v1 base, не смешивая его с остальными слоями.

        После первой полной Source B загрузки Registry больше не держит
        отдельный legacy-источник структуры. Поэтому следующая быстрая
        schema-v1 загрузка обязана стать новым native generation: иначе она
        видна лишь до рестарта, после чего старый base из manifest молча
        возвращается. Остальные слои берутся только из проверенного active
        bundle и заново из исходного ZIP не читаются.
        """
        if not isinstance(transport, CandidateTransport):
            raise TypeError("source_transport должен быть CandidateTransport")
        if previous.identity != active.identity or active.identity != (
            ExportIdentity.configuration(config.name)
        ):
            raise RegistryError("schema v1 относится к другой active generation")
        if not config.is_complete:
            # Признаки полноты теперь переживают restart внутри base layer,
            # но неполная A всё равно не может стать авторитетной базой
            # единого A/B view: она удалила бы отсутствующие в своём срезе
            # объекты, сохранив код, формы и роли полного B.
            raise RegistryError(
                "неполную schema v1 нельзя наложить на native generation: "
                "активное поколение и его указатель оставлены без изменений"
            )

        semantic = base_layer_data(config)
        content_sha256 = hash_layer_semantic(
            LayerKind.BASE_STRUCTURE,
            semantic,
        )
        active_base = next(
            (
                layer
                for layer in active.layers
                if layer.kind is LayerKind.BASE_STRUCTURE
            ),
            None,
        )
        if (
            previous.activation is not None
            and previous.activation.mode is ActivationMode.A_ONLY
            and active_base is not None
            and active_base.state is LayerState.READY
            and active_base.content_sha256 == content_sha256
        ):
            # Повтор той же A не является новой публикацией: иначе одинаковый
            # base без причины меняет generation и ломает единый no-op контракт.
            with self._lock:
                loaded = self.configurations.get(config.name)
                current = self._generation_pointers.get(active.identity.grouping_key)
            if loaded is None or current != previous:
                raise RegistryError(
                    "active generation изменилась во время проверки schema-v1 no-op"
                )
            return loaded.source
        # Новое поколение публикует только владелец: гость не создаёт
        # временный каталог и staging, которые сервер счёл бы мусором.
        self._ensure_owner("Загрузка schema v1 поверх native-поколения")
        # Временный каталог живёт под тем же замком, что и публикация:
        # startup подметает `.schema-v1-base-*`, оставшиеся после SIGKILL,
        # и не должен удалить каталог идущей замены базы в этом процессе.
        with self._generation_mutation_lock:
            payload = LayerPayload(LayerKind.BASE_STRUCTURE, semantic)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temporary = Path(
                tempfile.mkdtemp(prefix=".schema-v1-base-", dir=self.data_dir)
            )
            staged: StagedGeneration | None = None
            try:
                payload_path = temporary / "base_structure.json"
                with payload_path.open("xb") as stream:
                    stream.write(payload.to_json_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                base = LayerManifest(
                    kind=LayerKind.BASE_STRUCTURE,
                    state=LayerState.READY,
                    content_sha256=content_sha256,
                    payload_sha256=hash_layer_payload(
                        LayerKind.BASE_STRUCTURE,
                        payload_path,
                    ),
                    relative_path="layers/base_structure.json",
                    items_total=len(config),
                    provenance=LayerProvenance(
                        profile=LayerSourceProfile.SCHEMA_V1,
                        transport=transport,
                        origin_name=origin,
                        raw_sha256=digest,
                        parser_version=1,
                        selection_version=1,
                    ),
                )
                if not any(layer.kind is LayerKind.BASE_STRUCTURE for layer in active.layers):
                    raise RegistryError("active generation не содержит base_structure")
                generation_id = f"schema-v1-{uuid.uuid4().hex}"
                manifest = GenerationManifest(
                    format_version=active.format_version,
                    generation_id=generation_id,
                    identity=active.identity,
                    parser_version=active.parser_version,
                    selection_version=active.selection_version,
                    source_transport=transport,
                    origin_name=origin,
                    raw_sha256=digest,
                    layers=(base,),
                )
                activation = ActivationManifest(
                    mode=ActivationMode.A_ONLY,
                    identity_incarnation=generation_id,
                    physical_generation_root_id=generation_id,
                    configuration_version=config.version,
                    main=ActivationComponent(
                        source="source-a",
                        origin=origin,
                        raw_sha256=digest,
                        payload_sha256=hash_layer_payload(
                            LayerKind.BASE_STRUCTURE,
                            payload_path,
                        ),
                    ),
                    extensions=(),
                    expected_previous_activation=(
                        previous.activation.sha256
                        if previous.activation is not None
                        else None
                    ),
                    transaction_id=f"schema-v1-{uuid.uuid4().hex}",
                    recovery_id=uuid.uuid4().hex,
                )
                sources = {LayerKind.BASE_STRUCTURE: LayerPayloadSource(payload_path)}
                staged = self.stage_generation(
                    manifest,
                    sources,
                    activation=activation,
                )
                pointer = self.publish_generation(
                    staged,
                    expected_previous=previous,
                )
                staged = None
                with self._lock:
                    loaded = self.configurations.get(config.name)
                    current = self._generation_pointers.get(active.identity.grouping_key)
                if loaded is None or current != pointer:
                    raise RegistryError("schema-v1 generation опубликована не полностью")
                return loaded.source
            finally:
                if staged is not None:
                    try:
                        self.discard_staged_generation(staged)
                    except (OSError, BundleStoreError):
                        pass
                shutil.rmtree(temporary, ignore_errors=True)

    @_refresh_on_external_change()
    @_protect_source_operation
    def add_extension_runtime(
        self,
        path: str | Path,
        *,
        keep_source: bool = True,
        known_sha256: str = "",
        expected_id: str = "",
        known_origin: str = "",
        expected_revision: int | None = None,
    ) -> Source:
        """Опубликовать отдельный снимок текущего сеанса расширений.

        Сам JSON мал и сохраняется как исходник. Он привязан к уже загруженной
        конфигурации по имени, но несовпадение версий не отвергается: это
        полезный, честно помечаемый ``stale``-снимок, а не повреждённый файл.
        ``expected_revision`` передаёт restore: строка из снимка registry.json
        не публикуется, если источник сняли или заменили после снимка.
        """
        self._ensure_not_blocked(allow_restore=True)
        source_path = Path(path)
        digest = _sha256(source_path)
        if known_sha256 and digest != known_sha256:
            raise RegistryError(
                f"{source_path.name}: контрольная сумма сохранённого источника "
                "не совпала с registry.json."
            )
        try:
            snapshot = load_extension_runtime(source_path)
        except ExtensionRuntimeError as error:
            raise RegistryError(f"{source_path.name}: {error}") from error

        configuration = snapshot.configuration.name
        source_id = source_ids.extension_runtime_id(configuration)
        if expected_id and source_id != expected_id:
            raise RegistryError(
                f"{source_path.name}: идентификатор снимка «{source_id}» "
                f"не совпал с registry.json («{expected_id}»)."
            )
        with self._lock:
            if configuration not in self.configurations:
                raise UnknownConfiguration(
                    f"Конфигурация не загружена: {configuration}. Сначала "
                    "добавьте её структурную выгрузку schema v1."
                )

        stored = (
            self._store_source(
                source_path,
                "extension-runtime",
                source_id=source_id,
                digest=digest,
            )
            if keep_source
            else source_path
        )
        warnings = []
        if snapshot.database_changed_since_session_start is True:
            warnings.append(
                "Набор расширений базы изменён после запуска сеанса; снимок "
                "уже не описывает набор нового сеанса."
            )
        source = Source(
            id=source_id,
            kind=KIND_EXTENSION_RUNTIME,
            origin=known_origin or source_path.name,
            sha256=digest,
            loaded_at=_now(),
            platform=snapshot.configuration.platform,
            status=STATUS_READY,
            warnings=warnings,
            items_total=len(snapshot.by_uuid),
            stored_path=self._relative(stored),
        )
        loaded = LoadedExtensionRuntime(source=source, snapshot=snapshot)
        with self._lock:
            if configuration not in self.configurations:
                raise UnknownConfiguration(
                    f"Конфигурация не загружена: {configuration}. Она была "
                    "снята во время добавления снимка."
                )
            self._check_source_revision(source_id, expected_revision)
            self.extension_runtime[configuration] = loaded
            self.sources[source_id] = source
            self._advance_source_revision(source_id)
            self._advance_source_lifecycle_revision()
        return source

    def _modules_root(self, configuration: str) -> Path:
        """Каталог кода конфигурации внутри `modules_dir`.

        Имя чистится тем же правилом, что и имена файлов кэша
        (`index_cache.safe_name`): оно приходит из манифеста, а там встречается
        и косая черта, и двоеточие.

        Чистки мало: точку правило сохраняет, поэтому имя «..» проходит через
        неё неизменным. Путь сверяется при восстановлении рокировки, а каталог
        удаляется при снятии источника — промах увёл бы удаление за пределы
        `modules_dir`, вплоть до соседнего `incoming/`, который сервер трогать
        не вправе.
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
        родитель = цель.parent
        if родитель.parent != база:
            return
        try:
            родитель.rmdir()
        except OSError:
            # Другие расширения той же конфигурации сохраняют общий каталог.
            pass

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
        # У расширений есть дополнительный managed-уровень с именем базы.
        # Последнее расширение не должно оставлять пустую конфигурацию в
        # `data/extensions`; при конкурентном добавлении или наличии соседа
        # точечный `rmdir` безопасно откажется и ничего не удалит.
        if цель.parent.parent == база:
            try:
                цель.parent.rmdir()
            except OSError:
                pass

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

    def _путь_из_журнала_рокировки(self, raw: object) -> Path:
        if not isinstance(raw, str) or not raw:
            raise ValueError("invalid transaction path")
        path = self._absolute(raw).resolve()
        path.relative_to(self.data_dir.resolve())
        return path

    def _восстановить_рокировку_кода(self) -> list[str]:
        """Завершить или откатить пережившую процесс рокировку по registry.

        Окно совместимости: рокировку писал только прежний приём кода
        отдельно от структуры (`add_modules`), удалённый после 4.2.0. Marker мог
        остаться от процесса прежней версии, убитого посреди операции.
        Удалить после выпуска 4.3.0 вместе с подъёмом строк
        `KIND_MODULES`/`KIND_EXTENSION` без native-поколения.
        """
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
                if source_id.endswith(source_ids.MODULES_SUFFIX):
                    configuration = source_id[: -len(source_ids.MODULES_SUFFIX)]
                    if not configuration:
                        raise ValueError("empty configuration")
                    ожидаемый_корень = self._modules_root(configuration)
                else:
                    configuration, separator, extension = source_id.partition(
                        source_ids.EXTENSION_SEPARATOR
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
                removed_source = self.sources.pop(source_id, None)
                removed_modules = self.modules.pop(source_id, None)
                if removed_source is not None or removed_modules is not None:
                    self._advance_source_lifecycle_revision()
                self._modules_generation[source_id] = (
                    self._modules_generation.get(source_id, 0) + 1
                )

    def _словарь_модулей_locked(self, loaded: LoadedModules) -> LoadedModules:
        """Под `_lock`: поиск кода получает действующую таблицу синонимов."""
        if loaded.поиск is not None:
            loaded.поиск.synonyms = self._synonyms
        return loaded

    def _словарь_конфигурации_locked(
        self, loaded: LoadedConfiguration
    ) -> LoadedConfiguration:
        """Под `_lock`: индексы метаданных и полей — с действующим словарём."""
        loaded.index.synonyms = self._synonyms
        loaded.index.aliases = self.dictionary.aliases_for(loaded.config.name)
        loaded.field_index.synonyms = self._synonyms
        return loaded

    def _с_локальными_синонимами(
        self, индексы: modules_index.Индексы
    ) -> modules_index.Индексы:
        """Свежая сборка иначе знала бы только встроенные синонимы: локальный
        словарь поиск процедур получал лишь при подъёме из кэша."""
        индексы.поиск.synonyms = self.dictionary.synonyms()
        return индексы

    @staticmethod
    def _построить_индекс_кода(
        метка: str,
        корень: Path,
        identity: LocatorIdentity | None = None,
        прогресс: Callable[[int, str, int, int], None] | None = None,
        файлы_каталога: tuple[Path, ...] | None = None,
        каталог: modules_index.ModuleCatalog | None = None,
    ) -> modules_index.Индексы:
        """Все четыре структуры провайдера `modules` по коду на `корень`.

        Общее тело для native-поколения (`корень` — каталог поколения,
        `каталог` уже построен runtime) и для восстановления старой строки
        кода после рестарта (`корень` — каталог, куда код положил прежний
        приём). `метка` — только для текста ошибки: имя выгрузки или
        `source.origin` строки registry.json.

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
            каталог = каталог or modules_index.build_catalog(
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
                f"{метка}: индекс кода не построился — "
                f"{type(error).__name__}: {error}"
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
        source: Source,
        корень: Path,
        индексы: modules_index.Индексы,
        структура: structure_origin.StructureCatalog | None = None,
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
            структура=(
                структура
                if структура is not None
                else structure_origin.load(корень)
            ),
            готов=True,
            прогресс=(всего, всего),
        )

    def _журналировать_ограничения_кода(
        self, source: Source, индексы: modules_index.Индексы
    ) -> None:
        """По одной обезличенной строке на категорию нового поколения."""
        counts = Counter(
            {
                category: count
                for category, count in индексы.каталог.problem_counts
                if category != "missing_body"
            }
        )
        counts.update(
            {
                category: count
                for category, count in индексы.формы.problem_counts
                if category
                not in {
                    "descriptor_only",
                    "event_semantics_deferred",
                    "form_structure_missing",
                    "known_marker_semantics_deferred",
                    "known_marker_semantics_incomplete",
                    "unknown_marker",
                    "unsupported_profile",
                }
            }
        )

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

    def _обновить_журнал_покрытия(
        self,
        source: Source,
        loaded: LoadedModules,
    ) -> bool:
        """Записать журнал только пока Source и готовый пакет ещё актуальны.

        `registry.json` здесь не пишется: предупреждение о сбое журнала
        живёт в памяти до следующей записи реестра, а сам корпус уже
        опубликован предыдущей атомарной записью.
        """
        with self._modules_cache_lock:
            with self._lock:
                if not self._пакет_кода_актуален(source, loaded):
                    return False
            записан = False
            try:
                ожидаемый = coverage_log.build_payload(loaded)
                if coverage_log.load_current(
                    self.data_dir,
                    source,
                    expected=ожидаемый,
                ) is None:
                    coverage_log.write(
                        self.data_dir,
                        loaded,
                        payload=ожидаемый,
                    )
                записан = True
            except (OSError, ValueError, TypeError):
                # Старый файл другого поколения не должен выглядеть
                # актуальным после отказа замены. Удаление тоже best effort:
                # identity при чтении всё равно не позволит его показать.
                try:
                    coverage_log.remove(self.data_dir, source.id)
                except OSError:
                    pass
            with self._lock:
                if not self._пакет_кода_актуален(source, loaded):
                    if записан:
                        try:
                            coverage_log.remove(self.data_dir, source.id)
                        except OSError:
                            pass
                    return False
                source.warnings = [
                    warning
                    for warning in source.warnings
                    if warning != coverage_log.WRITE_WARNING
                ]
                if not записан:
                    source.warnings.append(coverage_log.WRITE_WARNING)
            return записан

    def _пакет_кода_актуален(
        self,
        source: Source,
        loaded: LoadedModules,
    ) -> bool:
        """Проверить под ``_lock`` legacy- или native-владельца корпуса."""
        if (
            self.modules.get(source.id) is not loaded
            or loaded.source is not source
            or not loaded.готов
        ):
            return False
        if self.sources.get(source.id) is source:
            return True
        # Native Source не дублируется строкой ``sources``: его durable-
        # владельцем является active generation pointer. Совпадение exact
        # runtime-объекта, canonical id и root не позволяет отставленному
        # writer записать журнал после переключения поколения.
        for pointer in self._generation_pointers.values():
            identity = pointer.identity
            native_source_id = (
                source_ids.modules_id(identity.configuration_name)
                if identity.source_kind is SourceKind.CONFIGURATION
                else source_ids.extension_modules_id(
                    identity.parent_configuration,
                    index_cache.safe_name(identity.extension_name),
                )
            )
            if (
                native_source_id == source.id
                and pointer.root_path == source.stored_path
            ):
                return True
        return False

    def _следующее_поколение_модулей(self, source_id: str) -> int:
        """Вызывается под `_lock`; возвращает новый монотонный номер."""
        поколение = self._modules_generation.get(source_id, 0) + 1
        self._modules_generation[source_id] = поколение
        return поколение

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
            индексы = self._с_локальными_синонимами(self._построить_индекс_кода(
                source.origin,
                корень,
                LocatorIdentity(
                    source.id, source.sha256, source.locator_generation
                ),
                отметить,
                файлы_каталога,
            ))
            готовые = self._готовые_модули(source, корень, индексы)
            # Writer и смена поколения сериализованы одним mutex. CAS
            # проверяется внутри него до записи, поэтому после проверки
            # remove или повторный restore не может вклиниться и сменить
            # Source под ногами.
            with self._modules_cache_lock:
                with self._lock:
                    if not self._поколение_актуально(
                        source, поколение, строится
                    ):
                        return
                if not self._guest:
                    # Кэш принадлежит владельцу data/: гость не пишет его
                    # поверх файлов, которые читает работающий сервер.
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
                        self.modules[source_id] = self._словарь_модулей_locked(
                            готовые
                        )
                        опубликовано = True
            if опубликовано:
                self._журналировать_ограничения_кода(source, индексы)
                self._обновить_журнал_покрытия(source, готовые)
        except Exception as error:
            logger.exception(
                "Фоновая сборка индекса кода %s завершилась ошибкой.",
                source_id,
            )
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
                or self._generation_recovery_blocked
                or self._module_recovery_blocked
                or self._registry_version_blocked
                or self._restore_hold
                or self._guest
            ):
                return False
            # Под `_startup_lock` reload невозможен: перечитывание внутри
            # save() заклинило бы поток, поэтому оно здесь подавлено.
            self._refresh_scope.depth = 1
            try:
                self.save()
            except Exception:
                # Исключение из finally фоновой сборки иначе терялось бы
                # вместе с потоком. Статус в памяти верен; на диск он попадёт
                # следующей записью реестра.
                logger.warning(
                    "Статус фоновой сборки кода не записан в registry.json.",
                    exc_info=True,
                )
                return False
            finally:
                self._refresh_scope.depth = 0
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

        Только для `restore()` старых строк `KIND_MODULES`/`KIND_EXTENSION`
        без native-поколения: их записывал прежний приём кода отдельно от
        структуры, удалённый после 4.2.0. Окно совместимости — удалить после
        выпуска 4.3.0 вместе с `_восстановить_рокировку_кода` и фоновой
        сборкой. Код к этому моменту уже лежит на `корень`. Сначала
        `поднять_индексы` из кэша проекта — на живой выгрузке 0,46 с против
        ~50 с полной сборки; не сошлось (штамп разошёлся после коммита, кэш побит, том
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
            if (
                прежний_поток is not None
                and self._сборка_жива(прежний_поток)
                and прежние
            ):
                # Текущая сборка владеет своим Source. Подмена его
                # объектом из JSON обесценила бы её CAS-публикацию.
                if self.sources.get(source.id) is not прежние.source:
                    self.sources[source.id] = прежние.source
                    self._advance_source_lifecycle_revision()
                return
            if (
                self.sources.get(source.id) is not None
                and прежние is not None
                and прежние.готов
            ):
                # Сборка прошлого restore могла успеть завершиться до
                # этого прохода. Её Source/индекс уже опубликованы;
                # старая строка registry не имеет права их откатывать.
                # Неготовая заглушка без живой сборки (сбой или отменённая
                # сборка) — не публикация: reload строит её заново.
                return
            self.sources[source.id] = source
            self._advance_source_lifecycle_revision()
            поколение = self._следующее_поколение_модулей(source.id)
            if source.locator_generation <= 0:
                # Старые registry.json не знали стабильного поколения. Их
                # прежний кэш всё равно не содержит каталог и будет промахом;
                # новое значение сохранится после фоновой пересборки.
                source.locator_generation = поколение

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
                self.modules[source.id] = self._словарь_модулей_locked(
                    self._готовые_модули(source, корень, индексы)
                )
                опубликовано = True
            if опубликовано:
                self._журналировать_ограничения_кода(source, индексы)
                готовые = self.modules.get(source.id)
                if готовые is not None:
                    self._обновить_журнал_покрытия(source, готовые)
            return

        self._запустить_фоновую_сборку(source, корень, поколение)

    @staticmethod
    def _сборка_жива(поток: threading.Thread) -> bool:
        """Поток уже зарегистрирован, но ещё не запущен, тоже считается живым.

        Между записью в ``_module_builds`` и ``start()`` у потока нет
        ``ident`` и ``is_alive()`` ложен: reload в это окно запустил бы
        вторую сборку того же корпуса.
        """
        return поток.ident is None or поток.is_alive()

    def _запустить_фоновую_сборку(
        self,
        source: Source,
        корень: Path,
        поколение: int,
    ) -> None:
        """Опубликовать заглушку `готов=False` и начать сборку индекса кода."""
        файлы_каталога = catalog_files(корень)
        строится = LoadedModules(
            source=source,
            корень=корень,
            оглавление=None,
            вызовы=None,
            формы=None,
            поиск=None,
            версия_кода=source.code_version,
            структура=structure_origin.load(корень),
            готов=False,
            прогресс=(0, len(файлы_каталога)),
            этап=(1, 4),
            название_этапа="оглавление",
        )
        source.status = STATUS_LOADING
        source.error = ""
        def собрать() -> None:
            self._собрать_индексы_фоном(
                source, корень, строится, поколение, файлы_каталога
            )

        поток = threading.Thread(
            target=собрать,
            daemon=True,
            name=f"modules:{source.id}",
        )
        # Generation снимается до первой долгой загрузки. Если
        # remove или повторный restore начнётся и даже завершится до
        # публикации, монотонный номер всё равно выдаст старую строку.
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
            суффикс = source_ids.MODULES_SUFFIX
            if source.id.endswith(суффикс):
                return source.id[: -len(суффикс)] or None
            return None
        if source.kind == KIND_EXTENSION:
            configuration, разделитель, extension = source.id.partition(source_ids.EXTENSION_SEPARATOR)
            if разделитель and configuration and extension:
                return configuration
        return None

    @_refresh_on_external_change()
    @_protect_source_operation
    def add_syntax(
        self,
        path: str | Path,
        *,
        platform: str = "",
        keep_source: bool = True,
        known_sha256: str = "",
        rebuild: bool = True,
        expected_revision: int | None = None,
    ) -> Source:
        """Принять версионную справку платформы."""
        self._ensure_not_blocked(allow_restore=True)
        source_path = Path(path)
        digest = known_sha256 or _sha256(source_path)
        platform = platform or _platform_from_path(source_path)

        is_index = source_path.suffix == ".gz"
        if is_index:
            syntax = load_syntax(source_path)
            platform = platform or syntax.max_platform
        else:
            try:
                syntax = parse_hbk(source_path, platform=platform)
                if not platform:
                    platform = syntax.derived_platform()
                    if platform:
                        syntax.platforms = [platform]
            except (ResourceLimitError, V8ContainerError) as error:
                raise RegistryError(f"{source_path.name}: {error}") from error

        # Без версии справку платформы принимать нельзя: она задаёт границы
        # применимости всему набору, а пустая граница означает «элемент
        # актуален» — противоположное правде. Вывести версию из данных
        # удаётся не всегда: в справке 8.3.5 отметок «начиная с версии» нет
        # ни на одной из 18 936 страниц, они появились позже.
        if not platform:
            raise RegistryError(
                f"{source_path.name}: не удалось определить версию платформы. "
                "В справках старых платформ версии внутри нет — укажите её при "
                "загрузке или назовите файл так, чтобы версия была в имени: "
                "`syntax-8.3.5.1570.hbk`."
            )
        if _RE_PLATFORM.fullmatch(platform) is None:
            raise RegistryError(
                f"{source_path.name}: версия платформы {platform!r} должна "
                "состоять из 3 или 4 числовых частей, например 8.3.27.2130."
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

        # Элементы есть, но ни у одного нет описания — это `shcntx_root.hbk`:
        # языконезависимая часть справки. Дерево страниц и английские
        # идентификаторы там те же, поэтому проверка на пустоту его пропускает.
        if not any(item.description for item in syntax.items.values()):
            raise RegistryError(
                f"{source_path.name}: ни у одного из {len(syntax)} элементов нет "
                "описания. Так выглядят языконезависимая часть справки "
                "(`shcntx_root.hbk` — дерево страниц и английские "
                "идентификаторы без текстов) и соседние справки платформы. "
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
            имя = platform or source_path.stem
            index_path = self.index_dir / "syntax" / f"{имя}.json.gz"
            save_syntax(syntax, index_path)

        source = Source(
            id=f"syntax-{platform or source_path.stem}",
            kind=KIND_SYNTAX,
            origin=source_path.name,
            sha256=digest,
            loaded_at=_now(),
            platform=platform,
            status=STATUS_READY,
            items_total=len(syntax),
            stored_path=self._relative(index_path),
        )

        предупреждение = search_keys_coverage(syntax.items.keys()).as_warning()
        if предупреждение:
            source.warnings.append(предупреждение)

        with self._lock:
            self._check_source_revision(source.id, expected_revision)
            self._advance_source_revision(source.id)
            # Справки разных версий стоят рядом: та же версия — это исправление
            # и заменяет прежнюю запись, другая версия дополняет набор.
            self.syntax_versions[source.id] = source
            self.sources[source.id] = source
            self._advance_source_lifecycle_revision()
            self._relation_cache.clear()
            snapshot = dict(self.syntax_versions)

        if rebuild:
            # Разобранная справка уже в руках — заново с диска её не читаем.
            self._apply_syntax(snapshot, {source.id: syntax})
        return source

    @staticmethod
    def _fingerprint(versions: dict[str, "Source"]) -> tuple:
        """Отпечаток набора справок, решающий, нужна ли пересборка."""
        return tuple(
            sorted((sid, source.sha256) for sid, source in versions.items())
        )

    def _prepare_syntax(
        self,
        versions: dict[str, "Source"],
        preloaded: dict[str, SyntaxIndex] | None = None,
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
        if not versions:
            return None, []

        preloaded = preloaded or {}
        по_версии = sorted(
            versions.values(), key=lambda source: parse_version(source.platform)
        )
        problems: list[str] = []

        # Готовый слитый вид снимает нужду читать справки версий вовсе — это и
        # есть смысл кэша: на трёх справках старт без него удваивался.
        # При повторном разборе того же HBK SHA исходника прежний, а уже
        # переданный новый индекс должен заменить содержимое слитого вида.
        merged = (
            self._cached_merged(по_версии)
            if len(по_версии) > 1 and not preloaded else None
        )
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

            if merged is not None and len(живые) > 1:
                self._save_merged(merged, живые)

        if merged is None:
            return None, problems

        newest = живые[-1]
        отпечаток = self._syntax_revision(живые)
        stamp = replace(newest, sha256=отпечаток)
        loaded = LoadedSyntax(
            source=newest,
            syntax=merged,
            index=self._syntax_index(merged, stamp),
            by_name=self._syntax_lookup(merged, stamp),
            tables=build_table_index(merged),
        )
        return loaded, problems

    def _syntax_revision(self, sources: list["Source"]) -> str:
        """Отпечаток разобранных индексов, а не только исходных справок.

        Повторный разбор того же HBK сохраняет исходный SHA, но заменяет
        индекс атомарно. Метаданные файла обесценивают все производные кэши
        без повторного чтения десятков мегабайт на каждом старте.
        """
        digest = hashlib.sha256()
        for source in sorted(sources, key=lambda item: item.id):
            try:
                stat = self._absolute(source.stored_path).stat()
                revision = (
                    f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
                )
            except OSError:
                revision = "missing"
            digest.update(f"{source.id}:{source.sha256}:{revision}|".encode())
        return digest.hexdigest()

    def _merged_path(self, sources: list["Source"]) -> Path:
        """Имя файла слитого вида — по разобранным индексам и коду.

        Отпечаток кода обязателен: слияние правится часто, и кэш, переживший
        правку, тихо отдавал бы результат прежней логики. Тем же штампом
        пользуются остальные производные (`index_cache`), и по той же причине.
        """
        отпечаток = f"{self._syntax_revision(sources)}:{index_cache._code_digest()}"
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
        # и вернуться к ним нельзя. Копить их — растить каталог молча. Гость
        # не удаляет: «прежний» для него может быть живым видом сервера.
        if self._guest:
            return
        for прежний in self._syntax_index_dir().glob("merged-*.json.gz"):
            if прежний != path:
                прежний.unlink(missing_ok=True)

    def _apply_syntax(
        self,
        versions: dict[str, "Source"],
        preloaded: dict[str, SyntaxIndex] | None = None,
    ) -> list[str]:
        """Собрать слитый вид вне замка и подменить ссылку под ним.

        Если за время сборки набор справок изменился — собираем заново по
        новому набору: наружу не должен попасть вид, не соответствующий
        списку источников.
        """
        while True:
            prepared, problems = self._prepare_syntax(versions, preloaded)
            with self._lock:
                if self._fingerprint(self.syntax_versions) == self._fingerprint(versions):
                    if self.syntax is not prepared:
                        self._advance_source_lifecycle_revision()
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
                preloaded = None

    @_refresh_on_external_change()
    def remove(self, source_id: str) -> None:
        """Снять источник; A/B основной конфигурации удаляются агрегатом."""
        # Снятие удаляет каталоги и кэш: в fail-closed режиме это стёрло бы
        # данные, которые ещё нужны для ручного разбора журнала.
        self._ensure_not_blocked()
        self._ensure_owner("Снятие источника")
        if source_id.endswith(source_ids.MODULES_SUFFIX):
            configuration = source_id.removesuffix(source_ids.MODULES_SUFFIX)
            identity = ExportIdentity.configuration(configuration)
            with self._lock:
                aggregate_exists = (
                    configuration in self.configurations
                    or configuration in self.sources
                    or identity.grouping_key in self._generation_pointers
                )
            if aggregate_exists:
                source_id = configuration
        # Publish и remove одной identity не могут пересекаться: иначе remove
        # проверит старый набор pointers, а publisher сразу после этого вернёт
        # уже снятую identity новым поколением.
        with self._generation_mutation_lock:
            self._remove_serialized(source_id)
            # Снятие сохраняется самой операцией: при отдельном save()
            # вызывающего чужая запись между ними оставила бы память без
            # источника, а файл и каталоги — расходящимися.
            self._save_unrefreshed()

    def _remove_serialized(self, source_id: str) -> None:
        """Снять источник; для конфигурации — весь связанный агрегат.

        `remove()` заранее переводит основной `<Имя>:modules` в тот же id
        конфигурации: Source A и Source B не должны оставлять друг от друга
        половину пользовательского объекта. Без каскада индекс кода либо
        metadata остались бы в памяти без второй части, а на диске появились
        бы каталоги, которые уже нельзя вернуть через интерфейс (см.
        `docs/modules-provider-design.md`, раздел 9). Поэтому конфигурация
        снимает base/extended/code/forms/roles, сеансовый снимок и дочерние
        расширения; самостоятельное расширение по-прежнему снимается отдельно.
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
            # Штамп сверяется до первого rename каталогов: отказ записи
            # registry.json после сноса оставил бы снятие применённым наполовину.
            self._check_registry_stamp()
            source = self.sources.get(source_id)
            native_configuration = next(
                (
                    (key, pointer)
                    for key, pointer in self._generation_pointers.items()
                    if pointer.identity.source_kind is SourceKind.CONFIGURATION
                    and source_id == pointer.identity.configuration_name
                ),
                None,
            )
            native_extension = next(
                (
                    (key, pointer)
                    for key, pointer in self._generation_pointers.items()
                    if pointer.identity.source_kind is SourceKind.EXTENSION
                    and source_id
                    == source_ids.extension_modules_id(
                        pointer.identity.parent_configuration,
                        index_cache.safe_name(pointer.identity.extension_name),
                    )
                ),
                None,
            )
            configuration_removal = (
                native_configuration is not None
                or (source is not None and source.kind == KIND_CONFIGURATION)
            )
            if configuration_removal:
                native = [
                    (key, pointer)
                    for key, pointer in self._generation_pointers.items()
                    if (
                        pointer.identity.source_kind is SourceKind.CONFIGURATION
                        and pointer.identity.configuration_name == source_id
                    )
                    or (
                        pointer.identity.source_kind is SourceKind.EXTENSION
                        and pointer.identity.parent_configuration == source_id
                    )
                ]
            elif native_extension is not None:
                native = [native_extension]
            else:
                native = []
            if (
                source is None
                and not native
                and source_id in self._carried_source_rows
            ):
                # Строку, не поднятую временным сбоем, снимают с учёта так же
                # явно, как живую: вместе с перенесёнными строками её кода.
                прежние_перенесённые = self._carried_source_rows
                self._carried_source_rows = {
                    sid: raw
                    for sid, raw in прежние_перенесённые.items()
                    if sid != source_id and not sid.startswith(f"{source_id}:")
                }
                try:
                    self.save()
                except BaseException:
                    self._carried_source_rows = прежние_перенесённые
                    raise
                for sid in set(прежние_перенесённые) - set(self._carried_source_rows):
                    self._advance_source_revision(sid)
                self._advance_configuration_revision(source_id)
                return
            if source is None and not native:
                raise RegistryError(f"Источник не зарегистрирован: {source_id}")

            # Цепочка считается ДО первого удаления: `self.sources` меняется
            # по ходу цикла ниже, и вычислять её позже значило бы гоняться за
            # движущейся целью. Только конфигурация каскадирует; отдельное
            # расширение снимается само по себе, без цепочки.
            цепочка = [source_id]
            if configuration_removal:
                модули_id = source_ids.modules_id(source_id)
                префикс_расширений = source_ids.extension_prefix(source_id)
                runtime_id = source_ids.extension_runtime_id(source_id)
                цепочка += sorted(
                    sid
                    for sid in self.sources
                    if sid in (модули_id, runtime_id)
                    or sid.startswith(префикс_расширений)
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
                if native:
                    next_pointers = dict(self._generation_pointers)
                    for native_key, _native_pointer in native:
                        next_pointers.pop(native_key)
                    next_sources = {
                        sid: current
                        for sid, current in self.sources.items()
                        if sid not in цепочка
                    }
                    next_platform_declarations = dict(self._platform_declarations)
                    if configuration_removal:
                        next_platform_declarations.pop(source_id, None)
                    # Pointer и возможная legacy-строка исчезают одной durable
                    # записью ДО удаления generation root. После SIGKILL
                    # restore уже не попытается открыть снятое поколение.
                    self._write_registry_payload(
                        self._registry_payload(
                            next_pointers,
                            sources=next_sources,
                            platform_declarations=next_platform_declarations,
                        )
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

            инвалидировать: set[str] = set()

            if configuration_removal:
                self._advance_configuration_revision(source_id)
                self._platform_declarations.pop(source_id, None)
            for sid in цепочка:
                self._advance_source_revision(sid)
                текущий = self.sources.pop(sid, None)
                if текущий is None:
                    continue
                self._advance_source_lifecycle_revision()
                if текущий.kind in (KIND_MODULES, KIND_EXTENSION):
                    try:
                        coverage_log.remove(self.data_dir, sid)
                    except OSError:
                        # Журнал диагностический и проверяется по identity;
                        # невозможность удалить его не держит источник живым.
                        logger.warning(
                            "Журнал покрытия снятого корпуса не удалён."
                        )
                self._drop_cache(sid, текущий.kind)
                self.modules.pop(sid, None)
                if текущий.kind in (KIND_MODULES, KIND_EXTENSION):
                    инвалидировать.add(sid)

                if текущий.kind == KIND_CONFIGURATION:
                    self.configurations.pop(sid, None)
                    self.roles.pop(sid, None)
                    self._relation_cache.pop(sid, None)
                elif текущий.kind in (KIND_MODULES, KIND_EXTENSION):
                    pass
                elif текущий.kind == KIND_EXTENSION_RUNTIME:
                    configuration = sid.removesuffix(source_ids.EXTENSION_RUNTIME_SUFFIX)
                    self.extension_runtime.pop(configuration, None)
                elif self.syntax_versions.pop(sid, None) is not None:
                    self._relation_cache.clear()
                    отложенная_справка = dict(self.syntax_versions)

            native_pointers: list[GenerationPointer] = []
            for native_key, native_pointer in native:
                native_pointers.append(native_pointer)
                identity = native_pointer.identity
                self._generation_pointers.pop(native_key, None)
                self._generation_manifests.pop(native_key, None)
                if identity.source_kind is SourceKind.CONFIGURATION:
                    configuration = identity.configuration_name
                    modules_id = source_ids.modules_id(configuration)
                    self.configurations.pop(configuration, None)
                    self.modules.pop(modules_id, None)
                    self.roles.pop(configuration, None)
                    self._relation_cache.pop(configuration, None)
                    self._drop_cache(configuration, KIND_CONFIGURATION)
                    self._drop_cache(modules_id, KIND_MODULES)
                    try:
                        self._cache_path(configuration, "roles.sqlite").unlink(
                            missing_ok=True
                        )
                    except OSError:
                        pass
                    try:
                        coverage_log.remove(self.data_dir, modules_id)
                    except OSError:
                        logger.warning(
                            "Журнал покрытия снятой native-конфигурации не удалён."
                        )
                    инвалидировать.add(modules_id)
                else:
                    native_source_id = source_ids.extension_modules_id(
                        identity.parent_configuration,
                        index_cache.safe_name(identity.extension_name),
                    )
                    self.modules.pop(native_source_id, None)
                    self.extension_structures.pop(native_source_id, None)
                    self.extension_relations.pop(native_source_id, None)
                    self.extension_roles.pop(native_source_id, None)
                    self._relation_cache.pop(identity.parent_configuration, None)
                    self._drop_cache(native_source_id, KIND_EXTENSION)
                    try:
                        self._cache_path(native_source_id, "roles.sqlite").unlink(
                            missing_ok=True
                        )
                    except OSError:
                        pass
                    try:
                        coverage_log.remove(self.data_dir, native_source_id)
                    except OSError:
                        logger.warning(
                            "Журнал покрытия снятого native-расширения не удалён."
                        )
                    инвалидировать.add(native_source_id)

            for sid in инвалидировать:
                # Номер не сбрасывается: иначе remove -> add даст
                # тот же generation и ABA вернёт старому writer право голоса.
                self._следующее_поколение_модулей(sid)

        for снести, каталог_кода in отложенные_сносы:
            снести(каталог_кода)
        if отложенная_справка is not None:
            self._apply_syntax(отложенная_справка)
        for native_pointer in native_pointers:
            try:
                self._generation_store.remove_pointer_root(native_pointer)
            except (OSError, RecoveryBlocked) as error:
                # Pointer уже durable снят. Оставшийся detached bundle не
                # обслуживается и безопаснее лишнего места на диске, чем
                # ложный ответ API о неудавшемся логическом удалении.
                logger.warning(
                    "Каталог снятого native-поколения не удалён: %s",
                    error,
                )

    # ------------------------------------------------------------- разрешение

    def resolve(
        self,
        name: str | None = None,
        *,
        require_configuration: bool = True,
        extension: str | None = None,
        diagnostic: bool = False,
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

        `diagnostic` используется только снимком состояния источников: он
        показывает причину обязательной перезагрузки, не обслуживая MCP-запросы
        к объектам, коду, формам и ролям старого поколения.
        """
        requested_name = name
        for _ in range(2):
            with self._lock:
                names = sorted(self.configurations)
                syntax = self.syntax

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
                    raise RegistryError(
                        "Не загружено ни одной конфигурации. Не хватает: "
                        + ", ".join(чего_нет)
                        + ". Выгрузку готовит обработка из `exporter-1c/`, "
                        "справка берётся из каталога установки платформы "
                        "(`shcntx_ru.hbk`) и загружается "
                        "командой `reg-add`."
                    )

                selected_name = requested_name
                if selected_name is None:
                    if len(names) > 1:
                        raise AmbiguousConfiguration(
                            "Загружено несколько конфигураций, укажите нужную "
                            "явно: " + ", ".join(names)
                        )
                    selected_name = names[0]

                loaded = self.configurations.get(selected_name)
                if loaded is None:
                    raise UnknownConfiguration(
                        f"Конфигурация не загружена: {selected_name}. "
                        f"Доступны: {', '.join(names)}"
                    )
                pointer = self._generation_pointers.get(
                    ExportIdentity.configuration(selected_name).grouping_key
                )
                if pointer is not None:
                    activation = classify_activation(
                        pointer.activation.to_dict()
                        if pointer.activation is not None else pointer.to_dict()
                    )
                    if (
                        activation.status is ActivationStatus.RELOAD_REQUIRED
                        and not diagnostic
                    ):
                        raise ReloadRequired("reload_required")
                elif self._legacy_mixed_locked(selected_name) and not diagnostic:
                    raise ReloadRequired("reload_required")
                declaration = self._platform_declarations.get(selected_name)
                platform_override = declaration.version if declaration else ""
                modules_key = source_ids.modules_id(selected_name)
                modules = self.modules.get(modules_key)
                roles = self.roles.get(selected_name)
                runtime = self.extension_runtime.get(selected_name)
                extension_key = None
                расширение = None
                extension_structure = None
                extension_roles = None
                if extension is not None:
                    extension_key = (
                        source_ids.extension_modules_id(selected_name, index_cache.safe_name(extension))
                    )
                    расширение = self.modules.get(extension_key)
                    extension_structure = self.extension_structures.get(extension_key)
                    extension_roles = self.extension_roles.get(extension_key)
                cached = self._relation_cache.get(selected_name)
                if (
                    cached is not None
                    and cached[0] is loaded
                    and cached[1] is syntax
                ):
                    if cached[2] == platform_override:
                        relation, hidden = cached[3], cached[4]
                    else:
                        relation = None
                        hidden = 0
                else:
                    relation = None
                    hidden = 0

            if relation is None:
                relation, hidden = self._compute_relation(
                    loaded, syntax, platform_override
                )
            extension_resolution = None
            if extension_structure is not None:
                try:
                    extension_resolution = resolve_extension_structure(
                        loaded.config,
                        extension_structure,
                    )
                except ExtensionResolutionError as error:
                    raise RegistryError(str(error)) from error

            with self._lock:
                unchanged = (
                    self.configurations.get(selected_name) is loaded
                    and self._generation_pointers.get(
                        ExportIdentity.configuration(selected_name).grouping_key
                    ) is pointer
                    and self.syntax is syntax
                    and (
                        (
                            self._platform_declarations.get(selected_name).version
                            if self._platform_declarations.get(selected_name)
                            else ""
                        )
                        == platform_override
                    )
                    and self.modules.get(modules_key) is modules
                    and self.roles.get(selected_name) is roles
                    and self.extension_runtime.get(selected_name) is runtime
                    and (
                        extension_key is None
                        or (
                            self.modules.get(extension_key) is расширение
                            and self.extension_structures.get(extension_key)
                            is extension_structure
                            and self.extension_roles.get(extension_key)
                            is extension_roles
                        )
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
                    platform_override,
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
                    extension_runtime=runtime,
                    roles=roles,
                    extension_roles=extension_roles,
                    extension_resolution=extension_resolution,
                    platform_override=platform_override,
                )

        raise RegistryError(
            "Источники изменились во время разрешения контекста дважды; "
            "повторите запрос."
        )

    def _compute_relation(
        self,
        loaded: LoadedConfiguration,
        syntax: LoadedSyntax | None,
        platform_override: str = "",
    ) -> tuple[str, int]:
        if syntax is None:
            return RELATION_NONE, 0

        effective_platform = platform_override or loaded.config.platform
        config_platform = parse_version(effective_platform)[:3]
        syntax_platform = parse_version(syntax.source.platform)[:3]

        # Справок может быть несколько: если среди них есть справка релиза
        # конфигурации, ответ по ней точный — независимо от того, какая из
        # загруженных самая свежая.
        if config_platform and syntax.syntax.has_help_for(effective_platform):
            return RELATION_EXACT, 0

        if not syntax_platform:
            # Версия справки неизвестна — фильтровать нечем. Молчать нельзя:
            # без фильтра агенту покажут методы, которых в его платформе нет.
            return RELATION_NONE, 0
        if not config_platform:
            return RELATION_UNKNOWN, 0
        if syntax_platform == config_platform:
            return RELATION_EXACT, 0
        if syntax_platform > config_platform:
            hidden = syntax.syntax.hidden_for(effective_platform)
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
            unknown_platform = False
            for name in sorted(self.configurations):
                declaration = self._platform_declarations.get(name)
                platform = (
                    declaration.version
                    if declaration
                    else self.configurations[name].config.platform
                )
                key = release(parse_version(platform))
                if not key:
                    unknown_platform = True
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
            if key not in нужные and not unknown_platform
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
                    "platform": context.platform,
                    "compatibility_mode": config.compatibility_mode,
                    "predefined_available": config.predefined_available,
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

    def _registry_payload(
        self,
        generation_pointers: Mapping[
            tuple[str, ...], GenerationPointer
        ] | None = None,
        *,
        sources: Mapping[str, Source] | None = None,
        platform_declarations: Mapping[str, PlatformDeclaration] | None = None,
    ) -> dict[str, object]:
        pointers = (
            self._generation_pointers
            if generation_pointers is None
            else generation_pointers
        )
        source_rows = self.sources if sources is None else sources
        declarations = (
            self._platform_declarations
            if platform_declarations is None
            else platform_declarations
        )
        rows = [source.to_dict() for source in source_rows.values()]
        rows += [
            dict(raw)
            for source_id, raw in self._carried_source_rows.items()
            if source_id not in source_rows
        ]
        payload: dict[str, object] = {
            "registry_version": REGISTRY_VERSION,
            "saved_at": _now(),
            "sources": rows,
        }
        if pointers:
            payload["generation_manifests"] = [
                pointer.to_dict()
                for _key, pointer in sorted(pointers.items())
            ]
        if declarations:
            payload["platform_declarations"] = {
                name: declaration.to_dict()
                for name, declaration in sorted(declarations.items())
            }
        return payload

    def platform_declaration(self, configuration: str) -> PlatformDeclaration | None:
        with self._lock:
            return self._platform_declarations.get(configuration)

    @_refresh_on_external_change()
    def set_platform_version(self, configuration: str, version: str) -> PlatformDeclaration:
        self._ensure_not_blocked()
        declaration = PlatformDeclaration(version)
        identity = ExportIdentity.configuration(configuration)
        with self._lock:
            if (
                identity.grouping_key not in self._generation_pointers
                and configuration not in self.configurations
            ):
                raise ConfigurationNotLoaded("configuration_not_loaded")
            next_declarations = dict(self._platform_declarations)
            next_declarations[configuration] = declaration
            self._write_registry_payload(
                self._registry_payload(platform_declarations=next_declarations)
            )
            self._platform_declarations = next_declarations
        return declaration

    @_refresh_on_external_change()
    def clear_platform_version(self, configuration: str) -> None:
        self._ensure_not_blocked()
        identity = ExportIdentity.configuration(configuration)
        with self._lock:
            if (
                identity.grouping_key not in self._generation_pointers
                and configuration not in self.configurations
            ):
                raise ConfigurationNotLoaded("configuration_not_loaded")
            next_declarations = dict(self._platform_declarations)
            next_declarations.pop(configuration, None)
            self._write_registry_payload(
                self._registry_payload(platform_declarations=next_declarations)
            )
            self._platform_declarations = next_declarations

    def _write_registry_payload(self, payload: Mapping[str, object]) -> None:
        """Durable atomic replace общего Registry pointer."""
        self._ensure_not_blocked()
        with self._registry_file_lock():
            if self._guest:
                # Гость дописывает свои строки в текущий файл, а не снимок
                # своей памяти: сервер мог записать своё после чтения гостем.
                payload = self._guest_patch_locked(payload)
            else:
                self._check_registry_stamp()
            self._write_json_durably(self.registry_path, payload, indent=1)
            self._registry_stamp = self._registry_file_stamp()
            if self._guest:
                self._registry_read_text = json.dumps(payload, ensure_ascii=False)

    def _check_registry_stamp(self) -> None:
        """Отказать, если registry.json сменил другой процесс после чтения."""
        if self._guest or self._registry_stamp is _REGISTRY_STAMP_UNSEEN:
            return
        if self._registry_file_stamp() != self._registry_stamp:
            # Память этого процесса не знает строк, записанных другим
            # (например, `mcp1c.cli reg-add`). Запись поверх стёрла бы их.
            raise RegistryChangedExternally(
                "registry.json изменён другим процессом во время операции. "
                "Повторите её: перед следующей попыткой реестр перечитает файл."
            )

    def _guest_patch_locked(self, payload: Mapping[str, object]) -> dict:
        """Наложить изменённые гостем строки на текущий registry.json."""
        base = json.loads(self._registry_read_text or "{}")
        base_rows = {
            raw.get("id"): raw
            for raw in base.get("sources") or []
            if isinstance(raw, dict)
        }
        current: dict = (
            json.loads(self.registry_path.read_text(encoding="utf-8"))
            if self.registry_path.exists()
            else {"registry_version": REGISTRY_VERSION, "sources": []}
        )
        rows = [raw for raw in current.get("sources") or [] if isinstance(raw, dict)]
        positions = {raw.get("id"): index for index, raw in enumerate(rows)}
        for raw in payload.get("sources") or []:
            if base_rows.get(raw.get("id")) == raw:
                continue
            index = positions.get(raw.get("id"))
            if index is None:
                positions[raw.get("id")] = len(rows)
                rows.append(raw)
            else:
                rows[index] = raw
        current["sources"] = rows
        current["saved_at"] = payload.get("saved_at", _now())
        base_declarations = base.get("platform_declarations") or {}
        declarations = dict(current.get("platform_declarations") or {})
        for name, raw in (payload.get("platform_declarations") or {}).items():
            if base_declarations.get(name) != raw:
                declarations[name] = raw
        if declarations:
            current["platform_declarations"] = declarations
        return current

    @contextmanager
    def _registry_file_lock(self):
        """Короткий межпроцессный замок «прочитать или заменить registry.json».

        Ждёт не дольше ``_REGISTRY_FILE_LOCK_TIMEOUT``: зависший второй
        процесс не должен бесконечно держать запросы сервера. Том без flock
        работает без замка с предупреждением, а не падает на каждом save().
        """
        if fcntl is None:
            yield
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / ".registry.lock"
        try:
            descriptor = _open_lock_file(path)
        except OSError as error:
            raise RegistryError(
                "Не удалось открыть замок записи registry.json: проверьте права "
                "на каталог данных."
            ) from error
        try:
            deadline = time.monotonic() + _REGISTRY_FILE_LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RegistryError(
                            "registry.json дольше "
                            f"{_REGISTRY_FILE_LOCK_TIMEOUT:.0f} с занят другим "
                            "процессом; повторите операцию."
                        ) from None
                    time.sleep(0.01)
                except OSError as error:
                    if error.errno not in _LOCK_UNSUPPORTED_ERRNOS:
                        raise RegistryError(
                            "Замок записи registry.json недоступен; запись "
                            "отклонена, чтобы не писать без защиты."
                        ) from error
                    self._warn_file_lock(error, path)
                    break
            yield
        finally:
            os.close(descriptor)

    def _warn_file_lock(self, error: OSError, path: Path) -> None:
        if not self._file_lock_warned:
            self._file_lock_warned = True
            _lock_unsupported(error, path)

    def _registry_file_stamp(self) -> str | None:
        """sha256 содержимого: не зависит от точности mtime и повтора inode."""
        try:
            return hashlib.sha256(self.registry_path.read_bytes()).hexdigest()
        except FileNotFoundError:
            return None

    def _write_json_durably(
        self,
        target: Path,
        payload: object,
        *,
        indent: int | None = None,
    ) -> None:
        """Записать JSON через tmp + fsync файла + replace + fsync каталога.

        Без fsync после сбоя питания rename может оказаться на диске раньше
        содержимого: вместо прежнего файла остаётся пустой или обрезанный.
        Для журналов восстановления это хуже отсутствия файла — битый журнал
        переводит реестр в fail-closed режим.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Общий `registry.tmp` сталкивался у параллельных writers: первый
        # replace уносил файл, второй получал FileNotFoundError.
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=indent)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
            descriptor = os.open(
                target.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _generation_pointers_from_payload(
        payload: object,
    ) -> dict[tuple[str, ...], GenerationPointer]:
        if not isinstance(payload, dict):
            raise BundleStoreError("registry.json должен быть объектом")
        raw_pointers = payload.get("generation_manifests", [])
        if not isinstance(raw_pointers, list):
            raise BundleStoreError("generation_manifests должен быть массивом")
        result: dict[tuple[str, ...], GenerationPointer] = {}
        for raw in raw_pointers:
            pointer = GenerationPointer.from_dict(raw)
            key = pointer.identity.grouping_key
            if key in result:
                raise BundleStoreError("registry дублирует generation identity")
            result[key] = pointer
        return result

    def stage_generation(
        self,
        manifest: GenerationManifest,
        payloads: Mapping[LayerKind, str | Path | LayerPayloadSource],
        *,
        activation=None,
    ) -> StagedGeneration:
        """Подготовить проверяемый bundle, не меняя active pointer."""
        return self._generation_store.stage(
            manifest, payloads, activation=activation
        )

    def discard_staged_generation(self, staged: StagedGeneration) -> None:
        """Убрать только незапубликованный staging неудачной операции."""
        self._generation_store.discard(staged)

    def active_generation_pointer(
        self, identity: ExportIdentity
    ) -> GenerationPointer | None:
        if not isinstance(identity, ExportIdentity):
            raise TypeError("identity должен быть ExportIdentity")
        with self._lock:
            return self._generation_pointers.get(identity.grouping_key)

    def active_activation(
        self, identity: ExportIdentity
    ) -> ActivationClassification | None:
        """Вернуть mode/barrier active pointer без миграции старого state."""
        pointer = self.active_generation_pointer(identity)
        if pointer is None:
            if identity.source_kind is SourceKind.CONFIGURATION:
                with self._lock:
                    if self._legacy_mixed_locked(identity.configuration_name):
                        return classify_activation({"legacy_mixed": True})
            return None
        if pointer.activation is None:
            return classify_activation(pointer.to_dict())
        return classify_activation(pointer.activation.to_dict())

    def _legacy_mixed_locked(self, configuration: str) -> bool:
        """Старая A-структура с отдельным B-кодом/расширением не является A_ONLY."""
        if not self._restored_for_serving or configuration not in self.configurations:
            return False
        modules_id = source_ids.modules_id(configuration)
        extension_prefix = source_ids.extension_prefix(configuration)
        return (
            modules_id in self.modules
            or any(key.startswith(extension_prefix) for key in self.modules)
            or any(
                source.kind in (KIND_MODULES, KIND_EXTENSION)
                and (source.id == modules_id or source.id.startswith(extension_prefix))
                for source in self.sources.values()
            )
        )

    def require_active_activation(self, identity: ExportIdentity) -> ActivationClassification:
        """Fail-closed guard для consumers, которым нужен новый active contract."""
        classification = self.active_activation(identity)
        if classification is None:
            raise ConfigurationNotLoaded("configuration_not_loaded")
        if classification.status is ActivationStatus.RELOAD_REQUIRED:
            raise ReloadRequired("reload_required")
        return classification

    def active_generation(
        self, identity: ExportIdentity
    ) -> GenerationManifest | None:
        if not isinstance(identity, ExportIdentity):
            raise TypeError("identity должен быть ExportIdentity")
        with self._lock:
            return self._generation_manifests.get(identity.grouping_key)

    def _generation_view_locked(
        self, identity: ExportIdentity
    ) -> GenerationView | None:
        """Native либо legacy view identity; вызывается только под `_lock`."""
        manifest = self._generation_manifests.get(identity.grouping_key)
        if manifest is not None:
            pointer = self._generation_pointers.get(identity.grouping_key)
            return native_generation_view(
                manifest,
                pointer.activation if pointer is not None else None,
                legacy_barrier=(
                    pointer is not None
                    and pointer.activation is None
                ),
            )
        if identity.source_kind is not SourceKind.CONFIGURATION:
            return None
        loaded = self.configurations.get(identity.configuration_name)
        if loaded is None:
            return None
        modules = self.modules.get(source_ids.modules_id(identity.configuration_name))
        code_source = (
            modules.source if modules is not None and modules.готов else None
        )
        return legacy_generation_view(
            identity,
            base_sha256=loaded.source.sha256,
            base_items_total=loaded.source.items_total,
            code_sha256=code_source.sha256 if code_source else "",
            code_items_total=code_source.items_total if code_source else 0,
        )

    def generation_payload_sources(
        self, pointer: GenerationPointer
    ) -> Mapping[LayerKind, LayerPayloadSource]:
        """Открыть active bundle как источники для composition staging."""
        if not isinstance(pointer, GenerationPointer):
            raise TypeError("pointer должен быть GenerationPointer")
        with self._lock:
            current = self._generation_pointers.get(pointer.identity.grouping_key)
            if current != pointer:
                raise RegistryError("active generation изменился до composition")
        return self._generation_store.payload_sources(pointer)

    def preview_active_extension_relations(
        self, expected: GenerationPointer,
    ) -> tuple[ExtensionRelation, ...]:
        """Посчитать влияние раннего no-op под защитой от смены поколения."""
        with self._generation_mutation_lock:
            if self.active_generation_pointer(expected.identity) != expected:
                raise RegistryError("active generation изменился после preview")
            manifest = self.active_generation(expected.identity)
            return self.preview_extension_relations(
                self.data_dir / expected.root_path, manifest,
            )

    def preview_extension_relations(
        self,
        generation_root: str | Path,
        manifest: GenerationManifest,
    ) -> tuple[ExtensionRelation, ...]:
        """Показать влияние кандидата базы без чтения extension ZIP.

        Читается только уже материализованный ``base_structure`` кандидата и
        малые сохранённые карты borrowed edges активных расширений.
        """
        if not isinstance(manifest, GenerationManifest):
            raise TypeError("manifest должен быть GenerationManifest")
        if manifest.identity.source_kind is not SourceKind.CONFIGURATION:
            return ()
        layer = next(
            (
                item
                for item in manifest.layers
                if item.kind is LayerKind.BASE_STRUCTURE
            ),
            None,
        )
        if (
            layer is None
            or layer.state is not LayerState.READY
            or not layer.relative_path
        ):
            raise RegistryError("preview базы не содержит готовый base_structure")
        try:
            payload = load_layer_payload(Path(generation_root) / layer.relative_path)
            candidate = configuration_from_base_layer(
                payload.semantic,
                source_profile=layer.provenance.profile if layer.provenance else None,
            )
        except (OSError, ValueError, BundleStoreError, GenerationRuntimeError) as error:
            raise RegistryError("base_structure preview не прочитан") from error
        with self._lock:
            structures = {
                source_id: structure
                for source_id, structure in self.extension_structures.items()
                if structure.parent_configuration == candidate.name
            }
        try:
            relation_map = resolve_extension_relation_map(candidate, structures)
        except ExtensionResolutionError as error:
            raise RegistryError(str(error)) from error
        relations = [
            relation
            for source_id in sorted(relation_map)
            for relation in relation_map[source_id]
        ]
        return tuple(
            sorted(
                relations,
                key=lambda item: (
                    item.extension.casefold(),
                    item.extension,
                    item.target.casefold(),
                    item.target,
                ),
            )
        )

    def _after_generation_pointer_switch(
        self, _pointer: GenerationPointer
    ) -> None:
        """Точка crash-теста сразу после durable pointer switch."""

    def _after_legacy_files_removed(
        self, _pointer: GenerationPointer
    ) -> None:
        """Точка crash-теста до снятия legacy-строк Registry."""

    def _managed_legacy_location(
        self,
        source: Source,
    ) -> tuple[tuple[str, ...], str, bool] | None:
        """Вернуть только принадлежащее Registry legacy-положение.

        `keep_source=False` оставляет в Source внешний путь. Native commit
        снимает такую строку с учёта, но не получает права удалять файл
        пользователя. Для managed-каталогов принимаются только точные
        раскладки старого Registry без вложенных или родительских сегментов.
        """
        stored = Path(source.stored_path)
        if not source.stored_path or stored.is_absolute():
            return None
        parts = stored.parts
        if source.kind == KIND_CONFIGURATION:
            if len(parts) != 3 or parts[:2] != ("sources", "configurations"):
                return None
            return ("sources", "configurations"), parts[-1], False
        elif source.kind == KIND_MODULES:
            expected = Path("modules") / index_cache.safe_name(
                source.id.removesuffix(source_ids.MODULES_SUFFIX)
            )
            if stored != expected:
                return None
            return ("modules",), parts[-1], True
        return None

    def _remove_managed_legacy_source(self, source: Source) -> bool:
        """Удалить managed-путь через `dir_fd`, не следуя symlink.

        `False` означает внешний или неизвестный путь: строка Registry будет
        снята, но этот файл принадлежит пользователю и не удаляется.
        """
        location = self._managed_legacy_location(source)
        if location is None:
            return False
        parents, name, directory = location
        opened: list[int] = []
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.data_dir, flags)
            opened.append(descriptor)
            for segment in parents:
                descriptor = os.open(segment, flags, dir_fd=descriptor)
                opened.append(descriptor)
            parent_fd = opened[-1]
            try:
                info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return True
            if stat.S_ISLNK(info.st_mode):
                os.unlink(name, dir_fd=parent_fd)
            elif directory:
                if not stat.S_ISDIR(info.st_mode):
                    raise RegistryError(
                        f"Legacy-каталог кода имеет неожиданный тип: {source.stored_path}"
                    )
                shutil.rmtree(name, dir_fd=parent_fd)
            else:
                if not stat.S_ISREG(info.st_mode):
                    raise RegistryError(
                        f"Legacy-источник имеет неожиданный тип: {source.stored_path}"
                    )
                os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            return True
        except FileNotFoundError:
            return True
        except RegistryError:
            raise
        except OSError as error:
            raise RegistryError(
                f"Legacy-путь не удалён безопасно: {source.stored_path}"
            ) from error
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

    def _legacy_cleanup_payload(
        self,
        pointer: GenerationPointer,
    ) -> tuple[
        dict[str, object],
        tuple[tuple[Source, dict[str, object]], ...],
    ]:
        """Прочитать точные legacy-строки только у active native pointer."""
        try:
            payload: object = json.loads(
                self.registry_path.read_text(encoding="utf-8")
            )
            pointers = self._generation_pointers_from_payload(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, BundleStoreError) as error:
            raise RegistryError(
                "legacy cleanup не может проверить active Registry pointer"
            ) from error
        if not isinstance(payload, dict):
            raise RegistryError("legacy cleanup получил неверный Registry")
        if pointers.get(pointer.identity.grouping_key) != pointer:
            return payload, ()
        raw_sources = payload.get("sources")
        if not isinstance(raw_sources, list):
            raise RegistryError("legacy cleanup получил неверный список sources")
        configuration = pointer.identity.configuration_name
        expected = {
            (configuration, KIND_CONFIGURATION),
            (source_ids.modules_id(configuration), KIND_MODULES),
        }
        records: list[tuple[Source, dict[str, object]]] = []
        try:
            for raw in raw_sources:
                if not isinstance(raw, dict):
                    raise TypeError("Source должен быть объектом")
                records.append((Source.from_dict(raw), dict(raw)))
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise RegistryError("legacy cleanup не может прочитать Source") from error
        selected = tuple(
            (source, raw)
            for source, raw in records
            if (source.id, source.kind) in expected
        )
        if len({source.id for source, _raw in selected}) != len(selected):
            raise RegistryError("legacy cleanup обнаружил дубли Source")
        return payload, selected

    def _cleanup_legacy_sources(self, pointer: GenerationPointer) -> int:
        """После native commit удалить legacy base/code с recovery через строки.

        Сначала удаляются только точные managed-файлы, затем атомарно исчезают
        их строки. Поэтому авария до второго шага оставляет в `registry.json`
        полный план повторной очистки; авария после него уже не оставляет
        файлов. Расширения и внешний `keep_source=False` вход не затрагиваются.
        """
        if pointer.identity.source_kind is not SourceKind.CONFIGURATION:
            return 0
        with self._modules_cache_lock:
            with self._lock:
                if (
                    self._generation_pointers.get(pointer.identity.grouping_key)
                    != pointer
                ):
                    return 0
                _payload, records = self._legacy_cleanup_payload(pointer)
                if not records:
                    return 0
                memory_sources = {
                    source.id: self.sources.get(source.id)
                    for source, _raw in records
                }
                modules_id = source_ids.modules_id(pointer.identity.configuration_name)
                self._следующее_поколение_модулей(modules_id)

            for source, _raw in records:
                self._remove_managed_legacy_source(source)
                if source.kind == KIND_MODULES:
                    try:
                        coverage_log.remove(self.data_dir, source.id)
                    except OSError:
                        pass
            self._after_legacy_files_removed(pointer)

            with self._lock:
                if (
                    self._generation_pointers.get(pointer.identity.grouping_key)
                    != pointer
                ):
                    return 0
                payload, current_records = self._legacy_cleanup_payload(pointer)
                captured = {source.id: raw for source, raw in records}
                removable = {
                    source.id
                    for source, raw in current_records
                    if captured.get(source.id) == raw
                }
                if not removable:
                    return 0
                raw_sources = payload["sources"]
                assert isinstance(raw_sources, list)
                payload["sources"] = [
                    raw
                    for raw in raw_sources
                    if not (
                        isinstance(raw, dict)
                        and raw.get("id") in removable
                        and captured.get(raw.get("id")) == raw
                    )
                ]
                payload["saved_at"] = _now()
                self._write_registry_payload(payload)
                for source_id in removable:
                    if self.sources.get(source_id) is memory_sources[source_id]:
                        self.sources.pop(source_id, None)
                        self._advance_source_lifecycle_revision()
                return len(removable)

    def _build_native_generation_runtime(
        self,
        root: Path,
        manifest: GenerationManifest,
    ) -> _PreparedNativeRuntime:
        """Построить полный runtime до изменения active pointer.

        Для расширения parent уже должен обслуживаться Registry. Его структура
        не заменяет базовую: до commit проверяются только сохранённые borrowed
        edges, а код и роли получают отдельный ключ.
        """
        identity = manifest.identity
        cache_owner = (
            identity.configuration_name
            if identity.source_kind is SourceKind.CONFIGURATION
            else source_ids.extension_modules_id(
                identity.parent_configuration,
                index_cache.safe_name(identity.extension_name),
            )
        )
        try:
            runtime = build_generation_runtime(
                root,
                manifest,
                role_cache_path=self._cache_path(
                    cache_owner,
                    "roles.sqlite",
                ),
            )
        except GenerationRuntimeError as error:
            raise RegistryError(
                f"{manifest.origin_name}: runtime поколения не построен — {error}"
            ) from error

        config = runtime.configuration
        pointer = GenerationPointer.for_manifest(manifest)
        cache_hits: set[str] = set()
        loaded_configuration = None
        parent_configuration = None
        extension_relations: tuple[ExtensionRelation, ...] = ()
        if identity.source_kind is SourceKind.CONFIGURATION:
            source = Source(
                id=config.name,
                kind=KIND_CONFIGURATION,
                origin=manifest.origin_name,
                sha256=runtime.base_sha256,
                loaded_at=_now(),
                platform=config.platform,
                status=STATUS_READY,
                items_total=len(config),
                stored_path=pointer.root_path,
                selection_version=manifest.selection_version,
            )
            loaded_configuration = LoadedConfiguration(
                source=source,
                config=config,
                graph=Graph(config),
                # До commit кэш нового поколения читать можно, а записывать
                # нельзя: неудачный preview не должен вытеснить warm-кэш active.
                index=self._configuration_index(
                    config,
                    source,
                    write_cache=False,
                    cache_sha256=runtime.structure_sha256,
                    cache_hits=cache_hits,
                ),
                field_index=self._field_index(
                    config,
                    source,
                    write_cache=False,
                    cache_sha256=runtime.structure_sha256,
                    cache_hits=cache_hits,
                ),
                structure_sha256=runtime.structure_sha256,
            )
        else:
            with self._lock:
                parent = self.configurations.get(identity.parent_configuration)
            if parent is None:
                raise RegistryError(
                    f"Для расширения {identity.extension_name} не загружен "
                    f"родитель {identity.parent_configuration}."
                )
            parent_configuration = parent
            if runtime.extension_structure is None:
                raise RegistryError(
                    f"{manifest.origin_name}: extension structure не построен"
                )
            try:
                extension_relations = resolve_extension_relations(
                    parent.config,
                    runtime.extension_structure,
                )
            except ExtensionResolutionError as error:
                raise RegistryError(str(error)) from error

        module_source: Source | None = None
        module_indices: modules_index.Индексы | None = None
        if runtime.catalog is not None:
            module_source = Source(
                id=runtime.catalog.identity.source_id,
                kind=(
                    KIND_MODULES
                    if identity.source_kind is SourceKind.CONFIGURATION
                    else KIND_EXTENSION
                ),
                origin=manifest.origin_name,
                sha256=runtime.code_sha256,
                loaded_at=_now(),
                status=STATUS_READY,
                items_total=runtime.code_items_total,
                stored_path=pointer.root_path,
                selection_version=manifest.selection_version,
                locator_generation=runtime.locator_generation,
                code_version=(
                    config.version
                    if identity.source_kind is SourceKind.CONFIGURATION
                    else parent.config.version
                ),
            )
            module_indices = modules_index.поднять_индексы(
                self,
                module_source.id,
                source_sha256=module_source.sha256,
                selection_version=module_source.selection_version,
                locator_generation=module_source.locator_generation,
            )
            if module_indices is not None:
                cache_hits.add("modules")
            else:
                module_indices = self._с_локальными_синонимами(self._построить_индекс_кода(
                    manifest.origin_name,
                    root,
                    identity=runtime.catalog.identity,
                    каталог=runtime.catalog,
                ))
        return _PreparedNativeRuntime(
            runtime,
            loaded_configuration,
            module_source,
            module_indices,
            parent_configuration,
            extension_relations,
            frozenset(cache_hits),
        )

    def _native_runtime_maps(
        self,
        prepared: _PreparedNativeRuntime,
        root: Path,
    ) -> tuple[LoadedConfiguration | None, LoadedModules | None]:
        loaded_modules = None
        if prepared.module_source is not None:
            assert prepared.module_indices is not None
            if prepared.parent_configuration is None:
                origin_catalog = structure_origin.base_catalog(
                    prepared.runtime.declared_structure,
                    prepared.module_source.sha256,
                )
            else:
                # Поколение расширения пока не хранит identity базы, с которой
                # была доказана дельта. Вычислять её заново при restart по уже
                # новой базе опасно: смена поколения стала бы незаметной.
                origin_catalog = None
            loaded_modules = self._готовые_модули(
                prepared.module_source,
                root,
                prepared.module_indices,
                структура=origin_catalog,
            )
        return prepared.configuration, loaded_modules

    def _save_native_runtime_cache(
        self,
        prepared: _PreparedNativeRuntime,
    ) -> None:
        """Best-effort кэш уже опубликованного runtime."""
        source = (
            prepared.configuration.source
            if prepared.configuration is not None
            else None
        )
        cached = prepared.cached_kinds
        try:
            if (
                source is not None
                and prepared.configuration is not None
                and "objects" not in cached
            ):
                index_cache.save(
                    prepared.configuration.index,
                    self._cache_path(source.id, "objects"),
                    source_sha256=(
                        prepared.configuration.structure_sha256
                        or source.sha256
                    ),
                    kind="objects",
                )
            if (
                source is not None
                and prepared.configuration is not None
                and "fields" not in cached
            ):
                index_cache.save(
                    prepared.configuration.field_index,
                    self._cache_path(source.id, "fields"),
                    source_sha256=(
                        prepared.configuration.structure_sha256
                        or source.sha256
                    ),
                    kind="fields",
                )
            if (
                prepared.module_source is not None
                and prepared.module_indices is not None
                and "modules" not in cached
            ):
                modules_index.сохранить_индексы(
                    self,
                    prepared.module_source.id,
                    prepared.module_indices,
                    source_sha256=prepared.module_source.sha256,
                    selection_version=prepared.module_source.selection_version,
                )
        except (OSError, TypeError, ValueError):
            # Кэш расходный: pointer и runtime уже опубликованы, поэтому
            # следующий запуск просто построит его заново.
            return

    def _обновить_native_диагностику_кода(
        self,
        prepared: _PreparedNativeRuntime,
        loaded: LoadedModules | None,
    ) -> None:
        """Best-effort диагностика уже опубликованного native-корпуса."""
        if (
            prepared.module_source is None
            or prepared.module_indices is None
            or loaded is None
        ):
            return
        self._журналировать_ограничения_кода(
            prepared.module_source,
            prepared.module_indices,
        )
        self._обновить_журнал_покрытия(prepared.module_source, loaded)

    @_refresh_on_external_change(retry=False)
    def publish_generation(
        self,
        staged: StagedGeneration,
        *,
        expected_previous: GenerationPointer | None | object = (
            _EXPECTED_GENERATION_UNSET
        ),
        expected_active: GenerationView | None | object = (
            _EXPECTED_GENERATION_VIEW_UNSET
        ),
    ) -> GenerationPointer:
        """Переключить active manifest и убрать только detached поколение."""
        # Новый WAL перезаписал бы неразобранный журнал прежней публикации.
        self._ensure_not_blocked()
        # WAL публикации один на каталог: гость затёр бы журнал сервера.
        self._ensure_owner("Публикация поколения")
        if expected_previous is not _EXPECTED_GENERATION_UNSET and (
            expected_previous is not None
            and not isinstance(expected_previous, GenerationPointer)
        ):
            raise TypeError("expected_previous должен быть GenerationPointer или None")
        if expected_active is not _EXPECTED_GENERATION_VIEW_UNSET and (
            expected_active is not None
            and not isinstance(expected_active, GenerationView)
        ):
            raise TypeError("expected_active должен быть GenerationView или None")
        manifest = self._generation_store.verify(staged)
        pointer = staged.pointer
        key = pointer.identity.grouping_key
        if (
            isinstance(expected_active, GenerationView)
            and expected_active.identity != pointer.identity
        ):
            raise TypeError("expected_active относится к другой identity")

        def expectation_changed() -> bool:
            previous = self._generation_pointers.get(key)
            return (
                expected_previous is not _EXPECTED_GENERATION_UNSET
                and previous != expected_previous
            ) or (
                expected_active is not _EXPECTED_GENERATION_VIEW_UNSET
                and self._generation_view_locked(pointer.identity) != expected_active
            )

        # Быстрый отказ до тяжёлой сборки. Повторный CAS под mutation lock
        # остаётся обязательным: pointer или legacy view могли смениться сразу
        # после проверки.
        with self._lock:
            changed = expectation_changed()
        if changed:
            self._generation_store.discard(staged)
            raise GenerationPublishConflict(
                "active generation изменился после построения preview"
            )
        with self._generation_mutation_lock:
            # Ранняя проверка выше шла до замка: startup мог за это время
            # выставить блокировку. Новый WAL поверх неразобранного запрещён.
            try:
                self._ensure_not_blocked()
            except RegistryRecoveryBlocked:
                self._generation_store.discard(staged)
                raise
            with self._lock:
                previous = self._generation_pointers.get(key)
                if expectation_changed():
                    self._generation_store.discard(staged)
                    raise GenerationPublishConflict(
                        "active generation изменился после построения preview"
                    )
                if previous is not None and previous.generation_id == pointer.generation_id:
                    raise RegistryError("generation_id уже является активным")
            prepared = self._generation_store.recovery_for(
                previous,
                staged,
                RecoveryPhase.PREPARED,
            )
            self._generation_store.write_recovery(prepared)
            try:
                # WAL появляется до тяжёлой сборки runtime: SIGKILL не
                # оставит бесхозный staging без доказуемого recovery-path.
                if manifest.identity.source_kind is SourceKind.EXTENSION:
                    parent_key = ExportIdentity.configuration(
                        manifest.identity.parent_configuration
                    ).grouping_key
                    with self._lock:
                        parent = self._generation_pointers.get(parent_key)
                    if (
                        parent is None
                        or parent.activation is None
                        or parent.activation.mode is not ActivationMode.B_FULL
                    ):
                        raise RegistryError(
                            "расширение можно опубликовать только поверх active B_FULL"
                        )
                prepared_runtime = self._build_native_generation_runtime(
                    staged.root,
                    manifest,
                )
                replacement_relations: dict[
                    str, tuple[ExtensionRelation, ...]
                ] = {}
                if prepared_runtime.configuration is not None:
                    candidate_base = prepared_runtime.configuration.config
                    with self._lock:
                        structures = {
                            source_id: structure
                            for source_id, structure in self.extension_structures.items()
                            if structure.parent_configuration == candidate_base.name
                        }
                    try:
                        replacement_relations = dict(
                            resolve_extension_relation_map(
                                candidate_base,
                                structures,
                            )
                        )
                    except ExtensionResolutionError as error:
                        raise RegistryError(str(error)) from error
                self._generation_store.promote(staged)
                runtime_root = self.data_dir / pointer.root_path
                loaded_configuration, loaded_modules = self._native_runtime_maps(
                    prepared_runtime,
                    runtime_root,
                )
                with self._lock:
                    if expectation_changed():
                        raise GenerationPublishConflict(
                            "active generation изменился после построения preview"
                        )
                    if (
                        prepared_runtime.parent_configuration is not None
                        and self.configurations.get(
                            manifest.identity.parent_configuration
                        )
                        is not prepared_runtime.parent_configuration
                    ):
                        raise GenerationPublishConflict(
                            "родитель расширения изменился во время публикации"
                        )
                    detached_extensions: list[GenerationPointer] = []
                    if (
                        manifest.identity.source_kind is SourceKind.CONFIGURATION
                        and pointer.activation is not None
                        and pointer.activation.mode is ActivationMode.A_ONLY
                    ):
                        detached_extensions = [
                            child
                            for child in self._generation_pointers.values()
                            if (
                                child.identity.source_kind is SourceKind.EXTENSION
                                and child.identity.parent_configuration
                                == manifest.identity.configuration_name
                            )
                        ]
                    prepared = replace(
                        prepared,
                        detached=tuple(detached_extensions),
                    )
                    # Точный cleanup-план обязан стать durable до Registry
                    # switch: после switch child pointers уже исчезнут из Registry.
                    self._generation_store.write_recovery(prepared)
                    next_pointers = dict(self._generation_pointers)
                    for child in detached_extensions:
                        next_pointers.pop(child.identity.grouping_key, None)
                    next_pointers[key] = pointer
                    self._write_registry_payload(
                        self._registry_payload(next_pointers)
                    )
                    self._generation_pointers = next_pointers
                    self._generation_manifests[key] = manifest
                    if loaded_configuration is not None:
                        name = loaded_configuration.config.name
                        self.configurations[name] = (
                            self._словарь_конфигурации_locked(loaded_configuration)
                        )
                        self._advance_configuration_revision(name)
                        modules_key = source_ids.modules_id(name)
                        if loaded_modules is None:
                            self.modules.pop(modules_key, None)
                        else:
                            self.modules[modules_key] = (
                                self._словарь_модулей_locked(loaded_modules)
                            )
                        if prepared_runtime.runtime.roles is None:
                            self.roles.pop(name, None)
                        else:
                            self.roles[name] = prepared_runtime.runtime.roles
                        if pointer.activation is not None and pointer.activation.mode is ActivationMode.A_ONLY:
                            for child in detached_extensions:
                                self._generation_manifests.pop(
                                    child.identity.grouping_key, None
                                )
                                extension_key = source_ids.extension_modules_id(
                                    child.identity.parent_configuration,
                                    index_cache.safe_name(child.identity.extension_name),
                                )
                                self.modules.pop(extension_key, None)
                                self.extension_structures.pop(extension_key, None)
                                self.extension_relations.pop(extension_key, None)
                                self.extension_roles.pop(extension_key, None)
                                self._drop_cache(extension_key, KIND_EXTENSION)
                                try:
                                    self._cache_path(extension_key, "roles.sqlite").unlink(
                                        missing_ok=True
                                    )
                                except OSError:
                                    pass
                                try:
                                    coverage_log.remove(self.data_dir, extension_key)
                                except OSError:
                                    pass
                        else:
                            self.extension_relations.update(replacement_relations)
                        self._relation_cache.pop(name, None)
                    else:
                        identity = manifest.identity
                        extension_key = source_ids.extension_modules_id(
                            identity.parent_configuration,
                            index_cache.safe_name(identity.extension_name),
                        )
                        if loaded_modules is None:
                            self.modules.pop(extension_key, None)
                        else:
                            self.modules[extension_key] = (
                                self._словарь_модулей_locked(loaded_modules)
                            )
                        structure = prepared_runtime.runtime.extension_structure
                        assert structure is not None
                        self.extension_structures[extension_key] = structure
                        self.extension_relations[extension_key] = (
                            prepared_runtime.extension_relations
                        )
                        if prepared_runtime.runtime.roles is None:
                            self.extension_roles.pop(extension_key, None)
                        else:
                            self.extension_roles[extension_key] = (
                                prepared_runtime.runtime.roles
                            )
                    self._after_generation_pointer_switch(pointer)
                self._generation_store.write_recovery(
                    replace(prepared, phase=RecoveryPhase.POINTER_SWITCHED)
                )
                self._generation_store.remove_pointer_root(previous)
                for child in detached_extensions:
                    self._generation_store.remove_pointer_root(child)
                self._generation_store.remove_staging(prepared.staging_path)
                self._generation_store.clear_recovery()
                try:
                    self._cleanup_legacy_sources(pointer)
                except Exception as error:
                    # Native pointer и runtime уже durable. Строки legacy
                    # остаются recovery-планом и будут повторены при restore.
                    logger.warning(
                        "Очистка legacy после native commit отложена: %s",
                        error,
                    )
                self._save_native_runtime_cache(prepared_runtime)
                self._обновить_native_диагностику_кода(
                    prepared_runtime,
                    loaded_modules,
                )
                return pointer
            except Exception:
                # Обычный отказ откатывается сразу. SIGKILL/SystemExit
                # намеренно оставляют WAL для следующего процесса.
                try:
                    self.recover_generation_publish()
                except RecoveryBlocked:
                    # Свой WAL не удалось разобрать: память и диск могут
                    # расходиться, дальнейшие записи закрыты до reload.
                    self._generation_recovery_blocked = True
                    raise
                with self._lock:
                    if self._generation_pointers.get(key) == pointer:
                        # Pointer уже durable: recovery успешно закончил
                        # housekeeping, поэтому повторять принятую операцию
                        # опаснее, чем вернуть её фактический результат.
                        return pointer
                raise

    def recover_generation_publish(self) -> list[str]:
        """По WAL доказуемо оставить старое или завершить новое поколение."""
        recovery = self._generation_store.read_recovery()
        if recovery is None:
            return []
        try:
            payload: object = {}
            if self.registry_path.exists():
                payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
            pointers = self._generation_pointers_from_payload(payload)
        except (OSError, ValueError, BundleStoreError) as error:
            # ValueError покрывает и JSONDecodeError, и UnicodeDecodeError:
            # невалидный UTF-8 в registry.json рядом с WAL раньше ронял старт
            # traceback-ом вместо fail-closed блокировки.
            raise RecoveryBlocked(
                "generation recovery неоднозначен: registry pointer не читается"
            ) from error
        key = recovery.staged.identity.grouping_key
        current = pointers.get(key)
        previous_id = (
            recovery.previous.generation_id if recovery.previous else None
        )
        current_id = current.generation_id if current else None
        action = decide_recovery(
            current_id,
            RecoveryRecord(
                previous_generation=previous_id,
                staged_generation=recovery.staged.generation_id,
                phase=recovery.phase,
            ),
        )
        if current_id == recovery.staged.generation_id and current != recovery.staged:
            action = RecoveryAction.BLOCK
        if (
            recovery.previous is not None
            and current_id == recovery.previous.generation_id
            and current != recovery.previous
        ):
            action = RecoveryAction.BLOCK
        if action is RecoveryAction.BLOCK:
            raise RecoveryBlocked(
                "generation recovery неоднозначен: active pointer не равен старому или новому"
            )
        if action is RecoveryAction.ROLLBACK_STAGING:
            self._generation_store.remove_pointer_root(recovery.staged)
            self._generation_store.remove_staging(recovery.staging_path)
            self._generation_store.clear_recovery()
            with self._lock:
                if recovery.previous is None:
                    self._generation_pointers.pop(key, None)
                    self._generation_manifests.pop(key, None)
            return [
                f"generation {recovery.staged.generation_id}: staging откачен"
            ]

        manifest = self._generation_store.verify(recovery.staged)
        detached_keys: set[tuple[str, ...]] = set()
        detached_roots: set[str] = set()
        if recovery.detached:
            activation = recovery.staged.activation
            staged_identity = recovery.staged.identity
            if (
                staged_identity.source_kind is not SourceKind.CONFIGURATION
                or activation is None
                or activation.mode is not ActivationMode.A_ONLY
            ):
                raise RecoveryBlocked(
                    "generation recovery содержит detached roots вне A_ONLY"
                )
            for child in recovery.detached:
                child_key = child.identity.grouping_key
                if (
                    child.identity.source_kind is not SourceKind.EXTENSION
                    or child.identity.parent_configuration
                    != staged_identity.configuration_name
                    or child_key in detached_keys
                    or child.root_path in detached_roots
                    or child in {recovery.previous, recovery.staged}
                    or child_key in pointers
                ):
                    raise RecoveryBlocked(
                        "generation recovery содержит неверный detached pointer"
                    )
                detached_keys.add(child_key)
                detached_roots.add(child.root_path)
        self._generation_store.remove_pointer_root(recovery.previous)
        for child in recovery.detached:
            self._generation_store.remove_pointer_root(child)
        self._generation_store.remove_staging(recovery.staging_path)
        self._generation_store.clear_recovery()
        with self._lock:
            self._generation_pointers[key] = recovery.staged
            self._generation_manifests[key] = manifest
        return [
            f"generation {recovery.staged.generation_id}: публикация завершена"
        ]

    def generation_view(self, configuration: str) -> GenerationView:
        identity = ExportIdentity.configuration(configuration)
        with self._lock:
            manifest = self._generation_manifests.get(identity.grouping_key)
            if manifest is not None:
                pointer = self._generation_pointers.get(identity.grouping_key)
                return native_generation_view(
                    manifest,
                    pointer.activation if pointer is not None else None,
                    legacy_barrier=(
                        pointer is not None
                        and pointer.activation is None
                    ),
                )
            loaded = self.configurations.get(configuration)
            if loaded is None:
                raise UnknownConfiguration(f"Конфигурация не загружена: {configuration}.")
            modules = self.modules.get(source_ids.modules_id(configuration))
            code_source = (
                modules.source
                if modules is not None and modules.готов
                else None
            )
            return legacy_generation_view(
                identity,
                base_sha256=loaded.source.sha256,
                base_items_total=loaded.source.items_total,
                code_sha256=code_source.sha256 if code_source else "",
                code_items_total=code_source.items_total if code_source else 0,
            )

    def _ensure_not_blocked(self, *, allow_restore: bool = False) -> None:
        """Отказать в записи, пока журнал прерванной операции не разобран.

        При неоднозначном WAL память заведомо неполна: startup вышел до
        restore() либо выкинул строки кода. Любая запись registry.json,
        нового WAL или удаление файлов источников затёрли бы единственное
        свидетельство сбоя и прежнее готовое состояние. ``allow_restore``
        пропускает только собственные add_* вызовы restore(): они поднимают
        уже записанные строки в память и сами ничего не сохраняют.
        """
        restoring = getattr(self._restore_scope, "active", False)
        if allow_restore and restoring:
            return
        if self._restore_hold and not restoring:
            raise RegistryRecoveryBlocked(self._restore_hold)
        if self._registry_version_blocked:
            raise RegistryRecoveryBlocked(self._registry_version_blocked)
        if self._generation_recovery_blocked:
            raise RegistryRecoveryBlocked(
                "Реестр открыт только для чтения: журнал публикации поколения "
                f"({self._relative(self.generation_recovery_path)}) не удалось "
                "восстановить. Проверьте каталог данных, исправьте или уберите "
                "журнал и выполните перезагрузку."
            )
        if self._module_recovery_blocked:
            raise RegistryRecoveryBlocked(
                "Реестр открыт только для чтения: журнал рокировки кода "
                f"({self._relative(self._module_swap_path)}) не удалось "
                "восстановить. Проверьте каталог данных, исправьте или уберите "
                "журнал и выполните перезагрузку."
            )

    def save(self) -> None:
        # Без reload: двухфазные вызовы (add_*() затем save()) при reload
        # здесь получили бы старую строку файла поверх новой памяти. Внешняя
        # смена доходит до вызывающего как RegistryChangedExternally;
        # перечитывание — только в начале операций, до мутации памяти.
        self._save_unrefreshed()

    def _save_unrefreshed(self) -> None:
        """Запись без reload: для путей, которые уже держат замки startup."""
        with self._lock:
            self._write_registry_payload(self._registry_payload())

    def mutate_dictionary(
        self,
        mutation: Callable[[Dictionary], _DictionaryMutationResult],
    ) -> _DictionaryMutationResult:
        """Сериализовать изменение, запись и публикацию словаря как одно целое.

        Правится копия: при отказе записи или исключении в самой правке в
        памяти не остаётся неподтверждённого изменения, которое молча унесла
        бы на диск следующая удачная правка.
        """
        with self._dictionary_mutation_lock:
            if self._dictionary_error:
                raise DictionaryError(
                    f"{self._dictionary_error} Правка не записана, чтобы не "
                    "затереть файл."
                )
            # База правки — файл на диске, а не память: правку оператора на
            # томе или командой `dict-*` до reload иначе молча затёрла бы
            # следующая правка из дашборда.
            try:
                candidate = Dictionary.load(self.dictionary_path)
            except DictionaryError as error:
                self._dictionary_error = str(error)
                raise DictionaryError(
                    f"{error} Правка не записана, чтобы не затереть файл."
                ) from error
            before = candidate.copy()
            result = mutation(candidate)
            # Правка без изменений файл не трогает: снятие отсутствующей
            # группы не должно падать на томе только для чтения.
            if (candidate.synonym_groups, candidate.aliases) != (
                before.synonym_groups,
                before.aliases,
            ):
                candidate.save(self.dictionary_path)
            self._publish_dictionary(candidate)
            return result

    @property
    def dictionary_error(self) -> str:
        """Почему локальный словарь не прочитан; пусто — прочитан."""
        return self._dictionary_error

    def reload_dictionary(self) -> list[str]:
        """Перечитать словарь. Индексы при этом не перестраиваются.

        Ни синонимы, ни псевдонимы не участвуют в построении постингов —
        `SearchIndex.add()` их не читает, они нужны только в момент поиска.
        Пересборка стоила бы секунду на каждую правку словаря и обесценивала
        бы кэш; достаточно подменить ссылки.

        Нечитаемый файл не подменяет действующий словарь пустым: остаётся
        прежний, причина возвращается сообщением, запись закрыта.
        """
        with self._dictionary_mutation_lock:
            try:
                dictionary = Dictionary.load(self.dictionary_path)
            except DictionaryError as error:
                self._dictionary_error = str(error)
                return [self._dictionary_error]
            self._dictionary_error = ""
            self._publish_dictionary(dictionary)
            return []

    def _publish_dictionary(self, dictionary: Dictionary) -> None:
        synonyms = dictionary.synonyms()
        with self._lock:
            self.dictionary = dictionary
            self._synonyms = synonyms
            for name, loaded in self.configurations.items():
                loaded.index.synonyms = synonyms
                loaded.index.aliases = dictionary.aliases_for(name)
                loaded.field_index.synonyms = synonyms
            # Поиск процедур получает синонимы при подъёме из кэша; без этой
            # подмены правка словаря доходила до него только после рестарта.
            for modules in self.modules.values():
                if modules.поиск is not None:
                    modules.поиск.synonyms = synonyms

    def restore(self) -> list[str]:
        """Поднять источники, записанные в registry.json."""
        previous = getattr(self._restore_scope, "active", False)
        self._restore_scope.active = True
        try:
            return self._restore_rows()
        finally:
            self._restore_scope.active = previous

    def _restore_rows(self) -> list[str]:
        with self._lock:
            self._restored_for_serving = True
        # От чтения registry.json до применения native pointers и runtime
        # restore сериализован с publish/remove: иначе снимок, прочитанный до
        # долгой сборки runtime, затёр бы опубликованное за это время
        # поколение или вернул бы снятое. Legacy-строки ниже идут уже без
        # него: у них свой CAS по ревизии конфигурации, и remove во время
        # их подъёма остаётся возможен.
        with ExitStack() as native_phase:
            native_phase.enter_context(self._generation_mutation_lock)
            return self._restore_rows_locked(native_phase)

    def _restore_rows_locked(self, native_phase: ExitStack) -> list[str]:
        problems: list[str] = []
        with self._registry_file_lock():
            stamp = self._registry_file_stamp()
            # Байты, а не текст: недопустимый UTF-8 разбирается ниже тем же
            # fail-closed путём, что и битый JSON.
            text = (
                self.registry_path.read_bytes()
                if stamp is not None
                else None
            )
        self._registry_stamp = stamp
        self._registry_read_text = text
        if text is None:
            self._registry_version_blocked = ""
            return problems

        try:
            payload = json.loads(text)
        except ValueError as error:
            payload = error
        if not isinstance(payload, dict):
            # Пустой или обрезанный файл после сбоя питания раньше ронял
            # старт traceback-ом. Пустое состояние поверх него записывать
            # нельзя: строки ещё можно восстановить руками. Тот же
            # fail-closed путь, что и для чужой версии формата.
            причина = (
                str(payload)
                if isinstance(payload, ValueError)
                else "корень не является объектом JSON"
            )
            self._registry_version_blocked = (
                "Реестр открыт только для чтения: registry.json не читается "
                f"({причина}). Файл не перезаписан; исправьте его или "
                "перенесите и выполните перезагрузку."
            )
            return [self._registry_version_blocked]
        if payload.get("registry_version") != REGISTRY_VERSION:
            # Раньше старт продолжался и итоговый save() записывал пустое
            # состояние поверх чужого формата: откат пакета на старую версию
            # стирал реестр новой. Файл остаётся дословно, запись закрыта.
            self._registry_version_blocked = (
                "Реестр открыт только для чтения: registry.json версии "
                f"{payload.get('registry_version')}, а этот сервер понимает "
                f"версию {REGISTRY_VERSION}. Файл не перезаписан; запустите "
                "версию сервера, которая его создала, или перенесите файл и "
                "выполните перезагрузку."
            )
            return [self._registry_version_blocked]
        # Версия прочитанного файла подходит: собственные записи restore
        # (legacy cleanup) уже не должны упираться в прежний отказ. Другие
        # потоки до конца restore держит `_restore_hold`.
        self._registry_version_blocked = ""

        pointers = self._generation_pointers_from_payload(payload)
        raw_declarations = payload.get("platform_declarations", {})
        if not isinstance(raw_declarations, dict):
            return ["registry.json platform_declarations должен быть объектом"]
        declarations = {
            name: PlatformDeclaration(
                raw["version"], raw.get("source", "user"), raw.get("status", "declared")
            )
            for name, raw in raw_declarations.items()
            if isinstance(name, str) and isinstance(raw, dict)
        }
        manifests: dict[tuple[str, ...], GenerationManifest] = {}
        for key, pointer in pointers.items():
            manifest = self._generation_store.verify(pointer)
            manifests[key] = manifest

        native_runtime: dict[
            tuple[str, ...],
            tuple[
                _PreparedNativeRuntime,
                LoadedConfiguration | None,
                LoadedModules | None,
            ],
        ] = {}
        # Родители поднимаются первыми независимо от порядка JSON-строк.
        for key, manifest in manifests.items():
            if manifest.identity.source_kind is not SourceKind.CONFIGURATION:
                continue
            pointer = pointers[key]
            prepared = self._build_native_generation_runtime(
                self.data_dir / pointer.root_path,
                manifest,
            )
            loaded_configuration, loaded_modules = self._native_runtime_maps(
                prepared,
                self.data_dir / pointer.root_path,
            )
            assert loaded_configuration is not None
            native_runtime[key] = (
                prepared,
                loaded_configuration,
                loaded_modules,
            )
        with self._lock:
            self._generation_pointers = pointers
            self._generation_manifests = manifests
            self._platform_declarations = declarations
            for _key, (
                _prepared,
                loaded_configuration,
                loaded_modules,
            ) in native_runtime.items():
                name = loaded_configuration.config.name
                self.configurations[name] = self._словарь_конфигурации_locked(
                    loaded_configuration
                )
                self._advance_configuration_revision(name)
                modules_key = source_ids.modules_id(name)
                if loaded_modules is None:
                    self.modules.pop(modules_key, None)
                else:
                    self.modules[modules_key] = self._словарь_модулей_locked(
                        loaded_modules
                    )
                if _prepared.runtime.roles is None:
                    self.roles.pop(name, None)
                else:
                    self.roles[name] = _prepared.runtime.roles
                self._relation_cache.pop(name, None)

        native_configurations = {
            manifest.identity.configuration_name
            for manifest in manifests.values()
            if manifest.identity.source_kind is SourceKind.CONFIGURATION
        }
        сырые_строки = payload.get("sources") or []
        все_источники = [Source.from_dict(raw) for raw in сырые_строки]
        строка_по_id = {
            raw.get("id"): raw for raw in сырые_строки if isinstance(raw, dict)
        }
        # Перенесённые прошлым restore строки снова лежат в registry.json и
        # повторяются этим проходом. До конца прохода прежний набор остаётся
        # в силе: параллельный save() не должен потерять ещё не повторённую
        # строку. Итоговый набор присваивается в конце restore.
        новые_перенесённые: dict[str, dict] = {}

        def удержать(source_id: str) -> bool:
            raw = строка_по_id.get(source_id)
            if raw is None:
                return False
            новые_перенесённые[source_id] = raw
            with self._lock:
                self._carried_source_rows[source_id] = raw
            return True

        def перенести(source: Source, error: BaseException) -> str:
            """Сохранить строку при временном сбое; вернуть пояснение."""
            if not _временный_сбой(error) or not удержать(source.id):
                return ""
            return (
                " (временный сбой: запись сохранена в registry.json и будет "
                "повторена при следующем восстановлении)"
            )
        конфигурации_этого_restore: dict[str, Source] = {}
        legacy_родители_расширений = {
            manifest.identity.parent_configuration
            for manifest in manifests.values()
            if manifest.identity.source_kind is SourceKind.EXTENSION
            and manifest.identity.parent_configuration not in native_configurations
        }
        # Native-расширение может жить поверх ещё не мигрировавшей schema-v1
        # базы. Такой родитель тоже должен быть готов до второго generation-
        # прохода; порядок строк sources и manifests контрактом не является.
        for source in все_источники:
            if (
                source.kind != KIND_CONFIGURATION
                or source.id not in legacy_родители_расширений
            ):
                continue
            stored = self._absolute(source.stored_path)
            if not stored.exists():
                problems.append(f"{source.id}: файл источника пропал ({stored})")
                continue
            try:
                конфигурации_этого_restore[source.id] = self.add_configuration(
                    stored,
                    keep_source=False,
                    known_sha256=source.sha256,
                    expected_id=source.id,
                    known_origin=source.origin,
                    allow_truncated=source.incomplete,
                    replace_native=False,
                )
            except _NativeGenerationActive:
                continue
            except Exception as error:
                problems.append(f"{source.id}: {error}{перенести(source, error)}")

        for key, manifest in manifests.items():
            if manifest.identity.source_kind is not SourceKind.EXTENSION:
                continue
            pointer = pointers[key]
            prepared = self._build_native_generation_runtime(
                self.data_dir / pointer.root_path,
                manifest,
            )
            loaded_configuration, loaded_modules = self._native_runtime_maps(
                prepared,
                self.data_dir / pointer.root_path,
            )
            assert loaded_configuration is None
            native_runtime[key] = (prepared, None, loaded_modules)
            identity = manifest.identity
            extension_key = source_ids.extension_modules_id(
                identity.parent_configuration,
                index_cache.safe_name(identity.extension_name),
            )
            structure = prepared.runtime.extension_structure
            assert structure is not None
            with self._lock:
                if loaded_modules is None:
                    self.modules.pop(extension_key, None)
                else:
                    self.modules[extension_key] = self._словарь_модулей_locked(
                        loaded_modules
                    )
                self.extension_structures[extension_key] = structure
                self.extension_relations[extension_key] = (
                    prepared.extension_relations
                )
                if prepared.runtime.roles is None:
                    self.extension_roles.pop(extension_key, None)
                else:
                    self.extension_roles[extension_key] = prepared.runtime.roles

        if not self._guest:
            # Гость не пишет кэш поверх файлов, которые читает сервер.
            for prepared, _configuration, _modules in native_runtime.values():
                self._save_native_runtime_cache(prepared)
        # Ревизии снимаются под тем же замком, под которым прочитан
        # registry.json. Remove или публикация, ждавшие native-фазу, пройдут
        # сразу после close(), и legacy-строки ниже сверяются с этим снимком.
        with self._lock:
            ревизии_конфигураций = dict(self._configuration_revisions)
            ревизии_источников = dict(self._source_revisions)
        native_phase.close()

        # Код восстанавливается вторым проходом: порядок строк в
        # registry.json не является контрактом, а модули/расширение
        # без уже восстановленной конфигурации не имеют владельца.
        with self._lock:
            отложенный_код = [
                (source, self._modules_generation.get(source.id, 0))
                for source in все_источники
                if source.kind in (KIND_MODULES, KIND_EXTENSION)
                and not self._module_recovery_blocked
            ]
            отложенный_runtime = [
                source
                for source in все_источники
                if source.kind == KIND_EXTENSION_RUNTIME
            ]
        for source in все_источники:
            if source.kind in (
                KIND_MODULES,
                KIND_EXTENSION,
                KIND_EXTENSION_RUNTIME,
            ):
                continue
            if (
                source.kind == KIND_CONFIGURATION
                and (
                    source.id in native_configurations
                    or source.id in конфигурации_этого_restore
                )
            ):
                # Legacy-файл сохраняется до отдельного crash-safe cleanup,
                # но не может подменить уже проверенный native runtime.
                continue
            stored = self._absolute(source.stored_path)
            if not stored.exists():
                problems.append(f"{source.id}: файл источника пропал ({stored})")
                continue
            try:
                if source.kind == KIND_CONFIGURATION:
                    try:
                        восстановленный_source = self.add_configuration(
                            stored,
                            keep_source=False,
                            known_sha256=source.sha256,
                            expected_id=source.id,
                            known_origin=source.origin,
                            allow_truncated=source.incomplete,
                            replace_native=False,
                            expected_revision=ревизии_конфигураций.get(
                                source.id, 0
                            ),
                        )
                    except _NativeGenerationActive:
                        # Публикация, дождавшаяся native-фазы, уже сделала
                        # конфигурацию native: строка A не должна поверх неё
                        # опубликовать A_ONLY. Legacy-файл уберёт cleanup.
                        continue
                    конфигурации_этого_restore[
                        source.id
                    ] = восстановленный_source
                elif source.kind == KIND_SYNTAX:
                    # Слитый вид собирается один раз в конце: сборка на каждой
                    # справке дала бы квадрат работы, а видел бы её только
                    # последний прогон. Вид источника передаём как сохранённый
                    self.add_syntax(
                        stored,
                        platform=source.platform,
                        keep_source=False,
                        known_sha256=source.sha256,
                        rebuild=False,
                        expected_revision=ревизии_источников.get(source.id, 0),
                    )
                else:
                    problems.append(
                        f"{source.id}: вид источника `{source.kind}` больше "
                        "не поддерживается и снят с учёта."
                    )
            except Exception as error:  # источник не должен ронять запуск
                problems.append(f"{source.id}: {error}{перенести(source, error)}")

        for source in отложенный_runtime:
            владелец = source.id.removesuffix(source_ids.EXTENSION_RUNTIME_SUFFIX)
            if владелец in новые_перенесённые:
                # Снимок без временно не поднятой конфигурации не публикуется,
                # но и не теряется вместе с ней.
                удержать(source.id)
                problems.append(
                    f"{source.id}: снимок не поднят — нет его конфигурации. "
                    "Запись сохранена до следующего восстановления."
                )
                continue
            stored = self._absolute(source.stored_path)
            if not stored.exists():
                problems.append(f"{source.id}: файл источника пропал ({stored})")
                continue
            try:
                self.add_extension_runtime(
                    stored,
                    keep_source=False,
                    known_sha256=source.sha256,
                    expected_id=source.id,
                    known_origin=source.origin,
                    expected_revision=ревизии_источников.get(source.id, 0),
                )
            except Exception as error:
                problems.append(f"{source.id}: {error}{перенести(source, error)}")

        for source, expected_generation in отложенный_код:
            configuration = self._владелец_источника_кода(source)
            if (
                source.kind == KIND_MODULES
                and configuration in native_configurations
            ):
                continue
            stored = self._absolute(source.stored_path)
            if not stored.exists():
                problems.append(f"{source.id}: файл источника пропал ({stored})")
                continue
            expected = (
                конфигурации_этого_restore.get(configuration)
                if configuration is not None
                else None
            )
            if expected is None and configuration in native_configurations:
                with self._lock:
                    loaded_configuration = self.configurations.get(configuration)
                    expected = (
                        loaded_configuration.source
                        if loaded_configuration is not None
                        else None
                    )
            if configuration is None or expected is None:
                владелец_перенесён = configuration in новые_перенесённые
                if владелец_перенесён:
                    # Код без своей конфигурации не публикуется, но и не
                    # теряется вместе с временно не поднятым владельцем.
                    удержать(source.id)
                problems.append(
                    f"{source.id}: индекс кода не поднят — "
                    "в этом восстановлении нет его конфигурации."
                    + (
                        " Запись сохранена до следующего восстановления."
                        if владелец_перенесён
                        else ""
                    )
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

        if self.syntax_versions:
            # Сборка не должна ронять запуск: испорченная справка версии
            # называется в списке проблем, остальные продолжают работать.
            try:
                problems += self._apply_syntax(dict(self.syntax_versions))
            except Exception as error:
                problems.append(f"слитый вид не собран: {error}")
        for pointer in pointers.values():
            if (
                pointer.identity.source_kind is not SourceKind.CONFIGURATION
                or self._guest
            ):
                # Удаление legacy-файлов — уборка владельца data/.
                continue
            try:
                self._cleanup_legacy_sources(pointer)
            except Exception as error:
                problems.append(
                    f"{pointer.identity.configuration_name}: legacy cleanup "
                    f"отложен — {error}"
                )
        for prepared, _configuration, loaded_modules in native_runtime.values():
            self._обновить_native_диагностику_кода(
                prepared,
                loaded_modules,
            )
        with self._lock:
            self._carried_source_rows = новые_перенесённые
        self._registry_version_blocked = ""
        return problems

    def bootstrap(self) -> list[str]:
        """Проиндексировать всё новое из `data/bootstrap/`.

        Так сервер поднимается готовым: файлы кладутся в каталог при сборке
        образа или в volume, руками через дашборд ничего делать не нужно.
        """
        added: list[str] = []
        if not self.bootstrap_dir.exists():
            return added

        known = self._bootstrap_known_sha256()
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
                    # После native-публикации legacy-строка A снята, и её sha
                    # из `known` пропадает. Повтор A поверх B_FULL на каждом
                    # старте публиковал бы A_ONLY и терял код, формы и роли:
                    # bootstrap наполняет пустой реестр, а не заменяет
                    # поколение, которое человек опубликовал явно.
                    source = self.add_configuration(path, replace_native=False)
                else:
                    source = self.add_syntax(path, rebuild=False)
                    справок += 1
                added.append(source.id)
            except _NativeGenerationActive:
                # Молчаливый пропуск выглядел бы как «файл не заметили»:
                # человек положил A и должен узнать, почему её нет в выдаче.
                logger.info("bootstrap: %s пропущен — native-поколение активно", path.name)
                added.append(
                    f"{path.name}: пропущен — у конфигурации уже есть "
                    "опубликованное native-поколение; новую Source A "
                    "загружают через дашборд или reg-add."
                )
            except Exception as error:
                added.append(f"{path.name}: ОШИБКА — {error}")

        if справок:
            self._apply_syntax(dict(self.syntax_versions))
        return added

    def _bootstrap_known_sha256(self) -> set[str]:
        """Sha исходников, уже представленных строками или native-поколениями.

        Native Source не дублируется строкой ``sources``, поэтому его исходник
        узнаётся только по ``raw_sha256`` manifest, активации и слоёв.
        """
        with self._lock:
            known = {source.sha256 for source in self.sources.values()}
            known.update(
                str(raw.get("sha256") or "")
                for raw in self._carried_source_rows.values()
            )
            for manifest in self._generation_manifests.values():
                known.add(manifest.raw_sha256)
                for layer in manifest.layers:
                    if layer.provenance is not None:
                        known.add(layer.provenance.raw_sha256)
            for pointer in self._generation_pointers.values():
                if pointer.activation is not None:
                    known.add(pointer.activation.main.raw_sha256)
        known.discard("")
        return known

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
            if source.kind == KIND_SYNTAX and source.stored_path
        }
        # Справка, не поднятая временным сбоем, остаётся строкой реестра:
        # удалить её индекс значит потерять источник навсегда.
        allowed |= self._carried_stored_paths()
        # Слитый вид источником не заявлен — он производное от всего набора.
        # Действующий оставляем, устаревшие уходят вместе с прочим мусором.
        with self._lock:
            versions = list(self.syntax_versions.values())
        if versions:
            по_версии = sorted(versions, key=lambda s: parse_version(s.platform))
            allowed.add(self._merged_path(по_версии).resolve())
        removed: list[str] = []
        now = time.time()
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.resolve() in allowed:
                continue
            # `add_syntax` пишет индекс до того, как заявит Source, а `.hbk`
            # не хранится: удалить свежий незаявленный файл — потерять
            # справку. Его может писать и гость (CLI) в другом процессе.
            if index_cache.being_written(path, now) or index_cache.recently_modified(
                path, now
            ):
                continue
            try:
                path.unlink()
            except OSError:
                # Том только на чтение. Лишний файл полежит, старт важнее.
                continue
            removed.append(path.name)
        return removed

    @_refresh_on_external_change()
    def forget_source(self, stored_path: str) -> None:
        """Удалить только текущий orphan, не доверяя прежнему снимку SPA."""
        # Без восстановленных строк каждый сохранённый исходник выглядит
        # orphan: удаление по такому списку стёрло бы живые источники.
        self._ensure_not_blocked()
        self._ensure_owner("Удаление файла источника")
        relative = Path(stored_path)
        if (
            relative.is_absolute()
            or len(relative.parts) < 2
            or relative.parts[0] != "sources"
            or ".." in relative.parts
            or relative.as_posix() != stored_path
        ):
            raise RegistryError("Удалять можно только файл внутри sources.")

        target = self.data_dir / relative
        keys = self._source_file_keys(target)
        opened: list[int] = []
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            opened.append(os.open(self.data_dir, os.O_RDONLY | os.O_DIRECTORY))
            for part in relative.parts[:-1]:
                opened.append(os.open(part, flags, dir_fd=opened[-1]))
            name = relative.name
            # Публикация владельца, резерв и unlink используют один замок.
            # Полное сканирование sources и разбор файлов здесь не выполняются.
            with self._lock:
                used = set().union(*(
                    self._source_file_keys(self._absolute(source.stored_path))
                    for source in self.sources.values() if source.stored_path
                ), *(
                    self._source_file_keys(self._absolute(stored))
                    for _, _, stored in self._carried_rows_locked() if stored
                ))
                if keys & used or any(self._source_reservations[key] for key in keys):
                    raise SourceInUseError(
                        "Файл уже используется или готовится к публикации. "
                        "Обновите список неиспользуемых файлов."
                    )
                info = os.stat(name, dir_fd=opened[-1], follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise RegistryError("Удалять можно только обычный файл, не ссылку.")
                os.unlink(name, dir_fd=opened[-1])
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)

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
            reserved_paths = tuple(self._source_reservations)
        used = {self._absolute(path).resolve() for path in stored_paths}
        used.update(reserved_paths)
        used |= self._carried_stored_paths()
        orphans: list[tuple[Path, int]] = []
        if not self.sources_dir.is_dir():
            return orphans
        for path in sorted(self.sources_dir.rglob("*")):
            try:
                if not path.is_file() or path.resolve() in used:
                    continue
                size = path.stat().st_size
            except OSError:
                # Атомарная публикация удаляет временный файл между rglob,
                # is_file и stat. Это штатная гонка с upload, а не причина
                # ронять всю страницу источников.
                continue
            orphans.append((path, size))
        return orphans

    def startup(self) -> list[str]:
        """Восстановить реестр один раз, не мешая параллельным `resolve()`."""
        previous = getattr(self._startup_scope, "active", False)
        self._startup_scope.active = True
        try:
            with self._startup_lock:
                # Каждый startup/reload снова пробует стать владельцем:
                # гость, стартовавший при чужом замке (долгий CLI, старый
                # контейнер при обновлении), не должен остаться им навсегда.
                self._try_take_ownership()
                self._startup_generation += 1
                поколение = self._startup_generation
                результат = (
                    self._startup_guest() if self._guest else self._startup_once()
                )
                self._last_successful_startup_generation = поколение
                return результат
        finally:
            self._startup_scope.active = previous

    def _try_take_ownership(self) -> bool:
        if self._owns_data_dir:
            return True
        if _acquire_ownership(self.data_dir):
            self._owns_data_dir = True
            self._guest = False
            weakref.finalize(self, _release_ownership, self.data_dir.resolve())
            return True
        self._guest = True
        return False

    def acquire_ownership(
        self,
        timeout: float = 0.0,
        *,
        on_wait: Callable[[], None] | None = None,
    ) -> bool:
        """Дождаться владения data/ не дольше ``timeout`` секунд.

        Сервер не должен работать урезанным гостем: при обновлении контейнера
        старый процесс ещё может держать замок, а долгий CLI — дописывать
        строку. ``on_wait`` вызывается один раз, когда ждать всё же пришлось.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        waited = False
        while True:
            with self._startup_lock:
                if self._try_take_ownership():
                    return True
            if time.monotonic() >= deadline:
                return False
            if not waited and on_wait is not None:
                on_wait()
            waited = True
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    @property
    def role(self) -> str:
        """``owner`` — этот процесс обслуживает data/, ``guest`` — другой."""
        if self._owns_data_dir:
            return "owner"
        return "guest" if self._guest else "unknown"

    def reload_if_changed(self) -> bool:
        """Перечитать registry.json, если его сменил другой процесс."""
        if not self._may_refresh():
            return False
        before = self._registry_stamp
        self._refresh_if_changed_externally()
        return self._registry_stamp != before

    def _may_refresh(self) -> bool:
        return (
            self._owns_data_dir
            and not self._guest
            and not getattr(self._refresh_scope, "depth", 0)
            and not getattr(self._startup_scope, "active", False)
            and not getattr(self._restore_scope, "active", False)
        )

    def _refresh_if_changed_externally(self) -> None:
        """Выполнить reload, если registry.json сменил другой процесс."""
        if self._registry_stamp is _REGISTRY_STAMP_UNSEEN:
            return
        # Под тем же замком, под которым writer обновляет штамп после replace:
        # иначе собственная запись соседнего потока выглядела бы чужой.
        with self._registry_file_lock():
            unchanged = self._registry_file_stamp() == self._registry_stamp
        if unchanged:
            return
        logger.info("registry.json изменён другим процессом: перечитываем")
        for message in self.startup():
            logger.info("reload: %s", message)

    def _startup_guest(self) -> list[str]:
        """Поднять память, не трогая журналы и файлы живого владельца data/.

        Recovery и уборка здесь опасны: WAL идущей публикации сервера выглядит
        как аварийный, а его временные каталоги — как мусор.
        """
        messages = self.reload_dictionary()
        # Подсказку про /admin/reload печатает только пишущая команда CLI:
        # чтению она ни к чему (см. `role`).
        return messages + self.restore()

    def _ensure_owner(self, operation: str) -> None:
        """Отказать в операции, которой нужен единственный писатель data/."""
        if self._guest:
            raise RegistryOwnedElsewhere(
                f"{operation} недоступна, пока каталог данных обслуживает "
                "работающий сервер: выполните её через дашборд или "
                "остановите сервер."
            )

    def _подмести_временные_базы_schema_v1(self) -> None:
        """Удалить `.schema-v1-base-*`, пережившие аварийную остановку.

        Вызывается под ``_generation_mutation_lock``: живая замена базы держит
        его на всё время жизни своего каталога.
        """
        try:
            кандидаты = tuple(self.data_dir.glob(".schema-v1-base-*"))
        except OSError:
            return
        for path in кандидаты:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)

    def _подмести_временные_файлы_записи(self) -> None:
        """Удалить tmp-файлы `_write_json_durably`, пережившие SIGKILL.

        Под замком записи registry.json ни один writer не держит свой tmp;
        журнал рокировки пишется под `_modules_cache_lock`, его tmp
        подметает `_подмести_временный_кэш_модулей`.
        """
        with self._registry_file_lock():
            for pattern in (".registry.json.*.tmp", "registry.tmp"):
                try:
                    кандидаты = tuple(self.data_dir.glob(pattern))
                except OSError:
                    return
                for path in кандидаты:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        continue

    def _подмести_временный_кэш_модулей(self) -> None:
        """Удалить staging-каталоги, пережившие аварийную остановку."""
        try:
            for path in self.data_dir.glob(".modules-swap.json.*.tmp"):
                path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            кандидаты = tuple(self.cache_dir.glob(".modules-cache.tmp-*"))
        except OSError:
            return
        for path in кандидаты:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    def _подмести_осиротевшие_каталоги_кода(self) -> None:
        """Дочистить `.<имя>.retired-*` и `.<имя>.tmp-*` в modules/ и extensions/.

        `.retired-*` уже отвязан от реестра операцией `remove` и остаётся,
        только если процесс погиб до `rmtree`; `.tmp-*` — от рокировки
        прежнего приёма кода. Раньше их подбирал следующий `add_modules`,
        которого больше нет. `.<имя>.old-*` не трогаем: это отставленная
        копия после отказа отката, и удалить её — решение человека.
        Вызывается только владельцем под `_modules_cache_lock`: гость
        не убирает за живым сервером.
        """
        корни = [self.modules_dir, self.extensions_dir]
        try:
            корни += [
                каталог
                for каталог in self.extensions_dir.iterdir()
                if каталог.is_dir() and not каталог.is_symlink()
            ]
        except OSError:
            pass
        for корень in корни:
            try:
                кандидаты = tuple(корень.glob(".*.retired-*")) + tuple(
                    корень.glob(".*.tmp-*")
                )
            except OSError:
                continue
            for path in кандидаты:
                if path.is_dir() and not path.is_symlink():
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
        dictionary_messages = self.reload_dictionary()
        if (
            self._generation_recovery_blocked
            or self._module_recovery_blocked
            or self._registry_version_blocked
        ):
            self._restore_hold = (
                "Реестр открыт только для чтения: идёт восстановление после "
                "сбоя. Повторите операцию после завершения перезагрузки."
            )
        try:
            return dictionary_messages + self._startup_held()
        finally:
            self._restore_hold = ""

    def _startup_held(self) -> list[str]:
        self._подмести_временные_файлы_записи()
        try:
            # Живая публикация держит этот замок от записи WAL до его
            # очистки: reload дожидается её, а не откатывает чужой staging.
            with self._generation_mutation_lock:
                messages = self.recover_generation_publish()
                self._подмести_временные_базы_schema_v1()
        except RecoveryBlocked as error:
            self._generation_recovery_blocked = True
            return [str(error)]
        # Флаг снимается только доказанным восстановлением: пока он стоит,
        # все записи в data/ отклоняются (`_ensure_not_blocked`).
        self._generation_recovery_blocked = False
        # Журнал рокировки мог остаться только от процесса прежней версии,
        # убитого посреди приёма кода (окно совместимости до выпуска 4.3.0).
        # Lock кэша занят лишь живым writer кэша; startup его не ждёт.
        module_messages: list[str] = []
        if self._modules_cache_lock.acquire(blocking=False):
            try:
                module_messages = self._восстановить_рокировку_кода()
                self._подмести_временный_кэш_модулей()
                if not module_messages:
                    # Только после доказанного восстановления: `.tmp-*`
                    # неразобранной рокировки ещё нужен человеку.
                    self._подмести_осиротевшие_каталоги_кода()
            finally:
                self._modules_cache_lock.release()
            # Без попытки восстановления прежний вердикт не меняется.
            self._module_recovery_blocked = bool(module_messages)
        messages += module_messages
        if self._module_recovery_blocked:
            self._заблокировать_источники_кода_после_ошибки_рокировки()
        messages += self.restore()
        # Память собрана заново: дальше действует только новый вердикт.
        self._restore_hold = ""
        if self._module_recovery_blocked or self._registry_version_blocked:
            # Сохраняем исходный registry.json и code-cache дословно: после
            # ручного исправления/удаления WAL следующий startup должен
            # суметь поднять прежнее готовое поколение, а не обнаружить, что
            # fail-closed проверка сама его забыла и подмела.
            return messages
        messages += [
            # Id источника не содержит «: », а пропуск и ошибка содержат.
            f"bootstrap: {name}" if ": " in name else f"добавлено из bootstrap: {name}"
            for name in self.bootstrap()
        ]
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
        try:
            self.save()
        except RegistryChangedExternally:
            # Гость (CLI reg-add) записал registry.json, пока шёл долгий
            # старт. Его строку нужно поднять, а не затереть и не уронить
            # старт traceback-ом: перечитываем и сохраняем один раз.
            messages += self.restore()
            try:
                self.save()
            except RegistryChangedExternally:
                messages.append(
                    "registry.json снова изменён другим процессом во время "
                    "старта; запись отложена до следующей операции."
                )
        return messages
