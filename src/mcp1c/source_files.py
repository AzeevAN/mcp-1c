"""Хранение исходников и операции с неиспользуемыми файлами.

Функции получают Registry только на время вызова. Учёт источников,
резервирования и замки принадлежат реестру.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from . import fsio
from .registry_types import RegistryError, SourceInUseError

if TYPE_CHECKING:
    from .registry import Registry


def store_source(
    registry: Registry,
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
    target_dir = registry.sources_dir / subdir
    # Резерв живёт до завершения всей add-операции, а не только копирования.
    # Временный файл также не должен исчезнуть между open и rename.
    registry._reserve_source_file(target_dir / target)
    registry._reserve_source_file(target_dir / temporary)
    opened: list[int] = []

    def open_child(parent_fd: int, name: str) -> int:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.open(name, flags, dir_fd=parent_fd)

    try:
        registry.data_dir.mkdir(parents=True, exist_ok=True)
        data_fd = os.open(
            registry.data_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        opened.append(data_fd)
        sources_fd = open_child(data_fd, "sources")
        opened.append(sources_fd)
        target_fd = open_child(sources_fd, subdir)
        opened.append(target_fd)

        copied = hashlib.sha256()
        with fsio.atomic_writer(
            target_fd, target, mode=0o600, temporary_name=temporary,
        ) as target_stream, path.open("rb") as source_stream:
            for block in iter(lambda: source_stream.read(1 << 20), b""):
                copied.update(block)
                target_stream.write(block)
            if copied.hexdigest() != digest:
                raise RegistryError(
                    f"{path.name}: исходник изменился во время сохранения."
                )
    except OSError as error:
        raise RegistryError(
            f"{path.name}: не удалось безопасно сохранить исходник: {error}"
        ) from error
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)
    return target_dir / target


def forget_source(registry: Registry, stored_path: str) -> None:
    """Удалить только текущий orphan, не доверяя прежнему снимку SPA."""
    # Без восстановленных строк каждый сохранённый исходник выглядит
    # orphan: удаление по такому списку стёрло бы живые источники.
    registry._ensure_not_blocked()
    registry._ensure_owner("Удаление файла источника")
    relative = Path(stored_path)
    if (
        relative.is_absolute()
        or len(relative.parts) < 2
        or relative.parts[0] != "sources"
        or ".." in relative.parts
        or relative.as_posix() != stored_path
    ):
        raise RegistryError("Удалять можно только файл внутри sources.")

    target = registry.data_dir / relative
    keys = registry._source_file_keys(target)
    opened: list[int] = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        opened.append(os.open(registry.data_dir, os.O_RDONLY | os.O_DIRECTORY))
        for part in relative.parts[:-1]:
            opened.append(os.open(part, flags, dir_fd=opened[-1]))
        name = relative.name
        # Публикация владельца, резерв и unlink используют один замок.
        # Полное сканирование sources и разбор файлов здесь не выполняются.
        with registry._lock:
            used = set().union(*(
                registry._source_file_keys(registry._absolute(source.stored_path))
                for source in registry.sources.values() if source.stored_path
            ), *(
                registry._source_file_keys(registry._absolute(stored))
                for _, _, stored in registry._carried_rows_locked() if stored
            ))
            if keys & used or any(registry._source_reservations[key] for key in keys):
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


def orphan_sources(registry: Registry) -> list[tuple[Path, int]]:
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
    with registry._lock:
        stored_paths = tuple(
            source.stored_path
            for source in registry.sources.values()
            if source.stored_path
        )
        reserved_paths = tuple(registry._source_reservations)
    used = {registry._absolute(path).resolve() for path in stored_paths}
    used.update(reserved_paths)
    used |= registry._carried_stored_paths()
    orphans: list[tuple[Path, int]] = []
    if not registry.sources_dir.is_dir():
        return orphans
    for path in sorted(registry.sources_dir.rglob("*")):
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
