"""Межпроцессные замки каталога данных и короткой записи registry.json.

Владение каталогом разделяется всеми Registry процесса; короткий замок
получает отдельный дескриптор на каждую операцию записи или чтения реестра.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:  # pragma: no cover - на Windows межпроцессного flock нет
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from .registry_types import RegistryError

logger = logging.getLogger("mcp1c.registry")

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


class RegistryFileLock:
    """Короткий flock с отдельным для каждого Registry предупреждением."""

    def __init__(self):
        self._file_lock_warned = False

    @contextmanager
    def hold(self, data_dir: Path):
        """Короткий межпроцессный замок «прочитать или заменить registry.json».

        Ждёт не дольше ``_REGISTRY_FILE_LOCK_TIMEOUT``: зависший второй
        процесс не должен бесконечно держать запросы сервера. Том без flock
        работает без замка с предупреждением, а не падает на каждом save().
        """
        if fcntl is None:
            yield
            return
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / ".registry.lock"
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
