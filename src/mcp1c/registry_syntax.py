"""Версионные справки: разбор, слияние и публикация снимка.

Функции получают реестр только на время вызова. Карты и замки остаются у
Registry; зависимости чтения и разбора передаются его фасадом явно.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import index_cache
from .registry_types import (
    KIND_SYNTAX,
    STATUS_ERROR,
    STATUS_READY,
    LoadedSyntax,
    RegistryError,
    Source,
)
from .resource_limits import ResourceLimitError
from .search_keys import coverage as search_keys_coverage
from .syntax_model import SyntaxIndex, parse_version
from .v8container import V8ContainerError
from .platform_events import event_catalog
from .virtual_tables import build_table_index

if TYPE_CHECKING:
    from .registry import Registry


def add_syntax(
    registry: Registry,
    path: str | Path,
    *,
    platform: str = "",
    keep_source: bool = True,
    known_sha256: str = "",
    rebuild: bool = True,
    expected_revision: int | None = None,
    reuse_loaded: bool = False,
    sha256: Callable[[Path], str],
    now: Callable[[], str],
    platform_from_path: Callable[[Path], str],
    platform_fullmatch: Callable[[str], object | None],
    parse_hbk: Callable[..., SyntaxIndex],
    load_syntax: Callable[[Path], SyntaxIndex],
    save_syntax: Callable[[SyntaxIndex, Path], Path],
) -> Source:
    """Принять версионную справку платформы.

    ``reuse_loaded`` — для restore: справка той же версии с тем же SHA,
    чей разобранный файл не менялся с прошлого приёма, повторно не
    читается. Проверки ниже она уже прошла, учётная запись копируется.
    """
    registry._ensure_not_blocked(allow_restore=True)
    source_path = Path(path)
    digest = known_sha256 or sha256(source_path)
    platform = platform or platform_from_path(source_path)

    is_index = source_path.suffix == ".gz"
    # Ревизия снимается ДО чтения: заменят файл во время разбора — ключ
    # останется от прежнего, и следующий reload честно перечитает.
    file_key = (
        registry._syntax_file_key(source_path, digest, platform)
        if is_index
        else None
    )
    if reuse_loaded and file_key is not None and platform:
        source_id = f"syntax-{platform}"
        with registry._lock:
            прежний = registry.syntax_versions.get(source_id)
            годен = (
                прежний is not None
                and прежний.status == STATUS_READY
                and registry._syntax_version_keys.get(source_id) == file_key
                # Тот же файл под другим путём строки реестра (`mv` плюс
                # правка registry.json) перечитывается: запись хранит путь.
                and прежний.stored_path == registry._relative(source_path)
            )
        if годен:
            source = replace(
                прежний, loaded_at=now(), warnings=list(прежний.warnings)
            )
            snapshot = registry._register_syntax_source(
                source, expected_revision, file_key
            )
            if rebuild:
                registry._apply_syntax(snapshot)
            return source

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
    if platform_fullmatch(platform) is None:
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
        index_path = registry.index_dir / "syntax" / f"{имя}.json.gz"
        save_syntax(syntax, index_path)
        file_key = registry._syntax_file_key(index_path, digest, platform)

    source = Source(
        id=f"syntax-{platform or source_path.stem}",
        kind=KIND_SYNTAX,
        origin=source_path.name,
        sha256=digest,
        loaded_at=now(),
        platform=platform,
        status=STATUS_READY,
        items_total=len(syntax),
        stored_path=registry._relative(index_path),
    )

    source.warnings.extend(syntax.warnings)

    предупреждение = search_keys_coverage(syntax.items.keys()).as_warning()
    if предупреждение:
        source.warnings.append(предупреждение)

    snapshot = registry._register_syntax_source(source, expected_revision, file_key)

    if rebuild:
        # Разобранная справка уже в руках — заново с диска её не читаем.
        registry._apply_syntax(snapshot, {source.id: syntax})
    return source


def register_syntax_source(
    registry: Registry,
    source: Source,
    expected_revision: int | None,
    file_key: tuple | None,
) -> dict[str, Source]:
    """Поставить учётную запись справки версии; вернуть снимок набора."""
    with registry._registry_memory_change():
        registry._check_source_revision(source.id, expected_revision)
        registry._advance_source_revision(source.id)
        # Справки разных версий стоят рядом: та же версия — это исправление
        # и заменяет прежнюю запись, другая версия дополняет набор.
        registry.syntax_versions[source.id] = source
        registry.sources[source.id] = source
        if file_key is None:
            registry._syntax_version_keys.pop(source.id, None)
        else:
            registry._syntax_version_keys[source.id] = file_key
        registry._advance_source_lifecycle_revision()
        registry._relation_cache.clear()
        return dict(registry.syntax_versions)


def syntax_file_key(path: Path, digest: str, platform: str) -> tuple | None:
    """Ревизия разобранной справки: SHA исходника, версия и метаданные файла.

    Повторный разбор того же HBK сохраняет SHA, но заменяет файл
    атомарно — inode и mtime меняются, и переиспользование отпадает.
    """
    try:
        stat_result = path.stat()
    except OSError:
        return None
    return (
        digest,
        platform,
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def fingerprint(versions: dict[str, Source]) -> tuple:
    """Отпечаток набора справок, решающий, нужна ли пересборка."""
    return tuple(
        sorted((sid, source.sha256) for sid, source in versions.items())
    )


def prepare_syntax(
    registry: Registry,
    versions: dict[str, Source],
    preloaded: dict[str, SyntaxIndex] | None = None,
    *,
    load_syntax: Callable[[Path], SyntaxIndex],
    merge_syntax: Callable[[list[SyntaxIndex]], SyntaxIndex],
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
        registry._cached_merged(по_версии)
        if len(по_версии) > 1 and not preloaded else None
    )
    живые = по_версии

    if merged is None:

        def поднять(source: Source) -> SyntaxIndex | None:
            index = preloaded.get(source.id)
            if index is not None:
                return index
            try:
                return load_syntax(registry._absolute(source.stored_path))
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
            registry._save_merged(merged, живые)

    if merged is None:
        return None, problems

    newest = живые[-1]
    отпечаток = registry._syntax_revision(живые)
    stamp = replace(newest, sha256=отпечаток)
    loaded = LoadedSyntax(
        source=newest,
        syntax=merged,
        index=registry._syntax_index(merged, stamp),
        by_name=registry._syntax_lookup(merged, stamp),
        tables=build_table_index(merged),
        events=event_catalog(merged),
    )
    return loaded, problems


def syntax_revision(registry: Registry, sources: list[Source]) -> str:
    """Отпечаток разобранных индексов, а не только исходных справок.

    Повторный разбор того же HBK сохраняет исходный SHA, но заменяет
    индекс атомарно. Метаданные файла обесценивают все производные кэши
    без повторного чтения десятков мегабайт на каждом старте.
    """
    digest = hashlib.sha256()
    for source in sorted(sources, key=lambda item: item.id):
        try:
            stat = registry._absolute(source.stored_path).stat()
            revision = (
                f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}"
            )
        except OSError:
            revision = "missing"
        digest.update(f"{source.id}:{source.sha256}:{revision}|".encode())
    return digest.hexdigest()


def merged_path(registry: Registry, sources: list[Source]) -> Path:
    """Имя файла слитого вида — по разобранным индексам и коду.

    Отпечаток кода обязателен: слияние правится часто, и кэш, переживший
    правку, тихо отдавал бы результат прежней логики. Тем же штампом
    пользуются остальные производные (`index_cache`), и по той же причине.
    """
    отпечаток = f"{registry._syntax_revision(sources)}:{index_cache._code_digest()}"
    имя = hashlib.sha256(отпечаток.encode()).hexdigest()[:16]
    return registry._syntax_index_dir() / f"merged-{имя}.json.gz"


def cached_merged(
    registry: Registry,
    sources: list[Source],
    *,
    load_syntax: Callable[[Path], SyntaxIndex],
) -> SyntaxIndex | None:
    """Готовый слитый вид с диска. `None` — кэша нет или он не читается."""
    path = registry._merged_path(sources)
    if not path.exists():
        return None
    try:
        return load_syntax(path)
    except Exception:
        # Кэш расходный: не прочитался — соберём заново.
        path.unlink(missing_ok=True)
        return None


def save_merged(
    registry: Registry,
    merged: SyntaxIndex,
    sources: list[Source],
    *,
    save_syntax: Callable[[SyntaxIndex, Path], Path],
) -> None:
    path = registry._merged_path(sources)
    try:
        save_syntax(merged, path)
    except OSError:
        # Том только на чтение — работать это не мешает.
        return
    # Прежние слитые виды больше не нужны: набор справок или код изменились,
    # и вернуться к ним нельзя. Копить их — растить каталог молча. Гость
    # не удаляет: «прежний» для него может быть живым видом сервера.
    if registry._guest:
        return
    for прежний in registry._syntax_index_dir().glob("merged-*.json.gz"):
        if прежний != path:
            прежний.unlink(missing_ok=True)


def apply_syntax(
    registry: Registry,
    versions: dict[str, Source],
    preloaded: dict[str, SyntaxIndex] | None = None,
) -> list[str]:
    """Собрать слитый вид вне замка и подменить ссылку под ним.

    Если за время сборки набор справок изменился — собираем заново по
    новому набору: наружу не должен попасть вид, не соответствующий
    списку источников.
    """
    while True:
        # Ключ снимается до сборки по той же причине, что в add_syntax:
        # файл, заменённый во время сборки, обесценит ключ, а не вид.
        key = (
            registry._fingerprint(versions),
            registry._syntax_revision(list(versions.values())),
        )
        with registry._lock:
            built = registry._syntax_built
            reuse = (
                built[1]
                if not preloaded
                and built is not None
                and built[0] == key
                and registry.syntax is built[1]
                else None
            )
        if reuse is not None:
            # Reload без изменений справок: слитый вид, его поисковый
            # индекс и таблицы те же — тот же объект, и кэши, привязанные
            # к нему по identity, остаются в силе.
            prepared, problems = reuse, []
        else:
            prepared, problems = registry._prepare_syntax(versions, preloaded)
        with registry._registry_memory_change():
            if registry._fingerprint(registry.syntax_versions) == registry._fingerprint(versions):
                if registry.syntax is not prepared:
                    registry._advance_source_lifecycle_revision()
                registry.syntax = prepared
                registry._syntax_built = (
                    (key, prepared)
                    if prepared is not None and not problems
                    else None
                )
                registry._relation_cache.clear()
                for problem in problems:
                    source_id = problem.split(":", 1)[0]
                    source = registry.sources.get(source_id)
                    if source is not None:
                        source.status = STATUS_ERROR
                        source.error = problem
                return problems
            versions = dict(registry.syntax_versions)
            preloaded = None
