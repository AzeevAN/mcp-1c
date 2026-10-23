"""Кэш построенных поисковых индексов на диске.

Построение индексов — 72% старта: справка 1,2 с, реквизиты трёх конфигураций
0,76 с. Данные для этого уже лежат на диске разобранными, платим мы за
токенизацию и постройку постингов, а результат от запуска к запуску один и
тот же.

**Формат — `marshal`, и это осознанное расхождение с `store.py`.** Там gzip +
JSON, потому что разобранная справка — канонический артефакт: единственный
носитель данных, если `.hbk` под рукой нет. Здесь всё наоборот: постинги
производные, восстанавливаются за секунду из уже загруженного, и глазами их
не читают. Замерено на индексе справки:

    json.gz  1037 мс   json 958 мс   marshal 117 мс   построить заново 1266 мс

JSON проигрывает не из-за разбора, а из-за структуры: `doc_id` повторяется в
постингах миллион раз, и JSON пишет каждое вхождение отдельной строкой — 106
МБ текста против 3,4 МБ. `marshal` версии 4 хранит повторы ссылками и на
подъёме переиспользует один объект.

От `pickle`, отвергнутого в `store.py`, отличается тем, что не исполняет код:
собирает только числа, строки, списки и словари.

**Кэш — расходник.** Не сошёлся штамп, побился файл, обновился Python — молча
строим как раньше. Неверный ответ получить нельзя: либо годный кэш, либо
пересборка.

**Подпись — HMAC-SHA256 заголовка и сжатого блоба.** Ключ — 32 случайных
байта в `.cache-key` того же каталога кэша, права 0600, создаётся при первой
записи и переживает рестарт: per-process ключ обесценивал бы кэш на каждом
старте, а тёплый старт — его единственный смысл. `marshal.loads` вызывается
только после сверки подписи. Честная граница: подпись защищает от порчи и от
подмены файла тем, кто не может прочитать ключ (кэш другой установки,
скопированный файл, запись без доступа к `.cache-key`). Тот, кто читает и
пишет весь `data/`, прочитает и ключ — от него подпись не защищает, как и
штамп.
"""

from __future__ import annotations

import errno
import gzip
import hashlib
import hmac
import json
import logging
import marshal
import os
import secrets
import stat
import sys
import tempfile
import threading
import time
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

from .search import SearchIndex
from .package_digest import package_digest
from .fsio import RecordReadError, read_record

logger = logging.getLogger(__name__)
# Каталоги, о чужом ключе в которых уже предупредили: отказ повторяется на
# каждом чтении кэша, а в журнале нужен один раз.
_WARNED_FOREIGN_KEYS: set[Path] = set()

CACHE_VERSION = 1

# Уровень 1, а не 6: файл вчетверо меньше несжатого (4,0 МБ против 21,3), а
# запись втрое дешевле, чем на шестом (115 мс против 431). Подъём при этом
# отличается на 19 мс — полтора процента старта.
COMPRESS_LEVEL = 1

_MARSHAL_VERSION = 4

# Один файл индекса на измеренном корпусе заметно меньше этих границ. Пределы
# защищают расходный reader от gzip bomb и от файла, который подменили уже
# после проверки штампа: cache miss дешевле попытки выделить всю память.
MAX_CACHE_FILE_BYTES = 256 * 1024 * 1024
MAX_CACHE_PAYLOAD_BYTES = 512 * 1024 * 1024
_MAX_HEADER_BYTES = 16 * 1024

KEY_FILE_NAME = ".cache-key"
# Ошибки os.link на файловых системах без жёстких ссылок (часть сетевых и
# FUSE-томов, bind-монтирования Docker Desktop): тогда ключ создаётся O_EXCL на месте.
_NO_HARDLINK = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in ("EPERM", "ENOTSUP", "EOPNOTSUPP", "EXDEV", "ENOSYS")
    )
    if code is not None
)
# Временные файлы, которые этот процесс пишет прямо сейчас: (каталог после
# realpath, префикс имени) → число живых записей. `sweep` идёт на старте
# владельца сразу после `restore()`, а тот уже запустил фоновую сборку
# индексов кода; по одному имени `.<файл>.*.tmp` живую запись не отличить от
# обрывка SIGKILL. Учёт процессный и точный, но кэш пишет и гость (CLI
# `reg-add` при живом сервере: Source A, справка, ролевой индекс при
# restore) — его записи закрывает порог свежести `TEMPORARY_GRACE_SECONDS`.
_LIVE_WRITES: dict[tuple[str, str], int] = {}
_LIVE_WRITES_LOCK = threading.Lock()


@contextmanager
def live_write(directory: str | Path, prefix: str) -> Iterator[None]:
    """Защитить от `sweep` временные файлы `<directory>/<prefix>*` на время записи.

    Регистрироваться нужно до создания временного файла: тогда `sweep`,
    увидевший файл в листинге, увидит и запись — либо файла уже нет.
    """
    key = (os.path.realpath(directory), prefix)
    with _LIVE_WRITES_LOCK:
        _LIVE_WRITES[key] = _LIVE_WRITES.get(key, 0) + 1
    try:
        yield
    finally:
        with _LIVE_WRITES_LOCK:
            left = _LIVE_WRITES[key] - 1
            if left:
                _LIVE_WRITES[key] = left
            else:
                del _LIVE_WRITES[key]


def _live_prefixes(directory: Path) -> tuple[str, ...]:
    real = os.path.realpath(directory)
    with _LIVE_WRITES_LOCK:
        return tuple(prefix for folder, prefix in _LIVE_WRITES if folder == real)


def is_temporary_name(name: str) -> bool:
    """Имя временного файла записи: `.<цель>.<случайное>.tmp`."""
    return name.startswith(".") and name.endswith(".tmp")


def recently_modified(path: Path, now: float | None = None) -> bool:
    """Файл менялся позже порога свежести — возможно, его ещё пишут.

    Будущий mtime (сдвиг часов) тоже считается свежим: удалить живое хуже,
    чем оставить обрывок до следующей уборки.
    """
    try:
        mtime = path.lstat().st_mtime
    except OSError:
        return False
    return (time.time() if now is None else now) - mtime < TEMPORARY_GRACE_SECONDS


def being_written(path: Path, now: float | None = None) -> bool:
    """Отмечен живой записью этого процесса или свежий временный другого."""
    if any(path.name.startswith(prefix) for prefix in _live_prefixes(path.parent)):
        return True
    return is_temporary_name(path.name) and recently_modified(path, now)


# Временный файл моложе порога уборка не трогает: его может писать другой
# процесс (гость — CLI `reg-add` при живом сервере), а процессная отметка
# `live_write` ему не видна. Самое долгое окно записи на крупнейшей реальной
# выгрузке (2026-10-03) — 4,2 с у ролевого SQLite, 0,18 с у `save_blob`,
# 1,5 с у разобранной справки. 10 минут — запас больше ×100 на медленный
# том (bind-mount Docker Desktop, сетевой диск), где окно не замерено;
# цена запаса — обрывок SIGKILL ждёт следующей уборки.
TEMPORARY_GRACE_SECONDS = 10 * 60
_KEY_BYTES = 32
# Без жёстких ссылок ключ создаётся O_EXCL прямо на месте, и между open и
# write файл виден пустым. Свежий пустой файл — не «битый», а ключ соперника
# в записи: его дожидаются, а не заменяют, иначе победитель подписывал бы
# кэш ключом, которого на диске уже нет. 32 байта уходят одним write, так
# что непустой короткий файл — порча, а не запись. Пустой старше порога —
# создатель умер между open и write, файл действительно битый.
_KEY_WAIT_SECONDS = 1.0
_KEY_FRESH_SECONDS = 30.0
_MAC_HEX_BYTES = 64
# Ключ читается с диска один раз на каталог и процесс: тёплый старт поднимает
# два десятка файлов, и каждый раз открывать ключ незачем.
_KEYS: dict[Path, bytes] = {}
_KEYS_LOCK = threading.Lock()
# Чтение, создание и ожидание недописанного ключа соперника (до
# `_KEY_WAIT_SECONDS`) идут под замком своего каталога, а не под общим:
# иначе секунда ожидания в одном каталоге останавливала бы запись и
# чтение кэша во всех остальных, ключи которых уже известны.
_KEY_DIRECTORY_LOCKS: dict[Path, threading.Lock] = {}


class _Miss(Exception):
    """Кэш не годится. Наружу не выходит — превращается в None."""


def _code_digest(_cache: list[str] = []) -> str:
    """Отпечаток кода, от которого зависит содержимое индексов.

    Берётся весь пакет, а не список «важных» модулей: такой список сгниёт —
    кто-нибудь тронет логику в четвёртом файле и забудет его дописать, а кэш
    молча разойдётся с кодом. Цена грубости — лишняя пересборка после любой
    правки исходников, один раз, полторы секунды. В контейнере код не меняется
    вовсе.
    """
    if _cache:
        return _cache[0]
    _cache.append(package_digest(Path(__file__).parent))
    return _cache[0]


# Предел на нашу часть имени: 255 байт — предел файловой системы, остальное
# оставлено под приписки вызывающих (вид кэша, префиксы временных каталогов).
_ПРЕДЕЛ_ИМЕНИ = 120


def safe_name(value: str) -> str:
    """Имя из идентификатора источника или конфигурации, годное для пути.

    Имя приходит из манифеста выгрузки, там встречается и косая черта, и
    двоеточие; в имя файла или каталога они не годятся.

    Правило одно на весь проект: тем же именем называется каталог кода
    `data/modules/<Имя>/` (`Registry._modules_root`). Второй способ чистки
    означал бы, что кэш и каталог кода расходятся на первом же необычном имени.

    Точка правилом сохраняется — она нужна в именах вида `Розница 2.3`, — и
    поэтому «..» проходит через чистку неизменным. Проверять, что собранный
    путь лежит там, где задумано, обязан вызывающий: чистка от выхода наружу
    не защищает.

    Длинное имя укорачивается: предел имени файла — 255 байт, а к нашему
    имени вызывающие приписывают своё (`<имя>.<вид>` у кэша, `.<имя>.tmp-…`
    и `.<имя>.old-…` у рокировки каталогов кода). Кириллица весит два байта
    на букву, поэтому предел достигается уже на сотне символов. Хвост из
    хеша исходного имени оставляет укороченные имена различимыми: без него
    две конфигурации, совпадающие в начале, делили бы один каталог кода —
    ровно та беда, от которой каталог расширений сделан двухуровневым.
    """
    чистое = "".join(ch if ch.isalnum() or ch in "-_. " else "_" for ch in value)
    if len(чистое.encode("utf-8")) <= _ПРЕДЕЛ_ИМЕНИ:
        return чистое
    хвост = "_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    место = _ПРЕДЕЛ_ИМЕНИ - len(хвост)
    обрезок = чистое.encode("utf-8")[:место].decode("utf-8", "ignore")
    return обрезок + хвост


def path_for(cache_dir: str | Path, source_id: str, kind: str) -> Path:
    """Путь к файлу кэша — детерминированный, без дат и хешей в имени.

    Пересборка обязана перезаписывать тот же файл, а не класть рядом новый:
    иначе каталог растёт молча, и заметят это нескоро — кэш никто не открывает.
    """
    return Path(cache_dir) / f"{safe_name(source_id)}.{kind}"


class _KeyUnavailable(Exception):
    """Ключ нельзя ни прочитать, ни безопасно создать — кэш не используется."""


def _read_key_file(key_path: Path) -> bytes | None:
    """Прочитать ключ. ``None`` — файла нет или он испорчен (размер не тот)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(key_path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        # ELOOP — на месте ключа символическая ссылка: чужой ключ не берём.
        raise _KeyUnavailable(str(error)) from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise _KeyUnavailable("ключ кэша не является обычным файлом")
        os.set_blocking(descriptor, True)
        # Чужой или доступный другим ключ не берём и не подменяем: его мог
        # подложить кто угодно с записью в каталог, а решать — оператору.
        # На платформе без geteuid (Windows) владельца сверить нечем.
        geteuid = getattr(os, "geteuid", None)
        if geteuid is not None and info.st_uid != geteuid():
            _warn_foreign_key(key_path, "принадлежит другому пользователю")
            raise _KeyUnavailable("ключ кэша принадлежит другому пользователю")
        if info.st_mode & 0o077:
            _warn_foreign_key(key_path, "доступен группе или всем")
            raise _KeyUnavailable("ключ кэша доступен группе или всем")
        if info.st_size != _KEY_BYTES:
            return None
        key = os.read(descriptor, _KEY_BYTES + 1)
    except OSError as error:
        raise _KeyUnavailable(str(error)) from error
    finally:
        os.close(descriptor)
    return key if len(key) == _KEY_BYTES else None


def _warn_foreign_key(key_path: Path, reason: str) -> None:
    """Сказать оператору один раз: кэш молча перестраивался бы на каждом старте."""
    directory = key_path.parent
    if directory in _WARNED_FOREIGN_KEYS:
        return
    _WARNED_FOREIGN_KEYS.add(directory)
    logger.warning(
        "Ключ кэша индексов %s %s: кэш в этом каталоге не используется. "
        "Удалите ключ или исправьте владельца и права (0600).",
        key_path,
        reason,
    )


def _create_key_in_place(key_path: Path, key: bytes) -> bytes:
    """Создать ключ эксклюзивно прямо на месте, если жёстких ссылок нет.

    Проверка «нет файла» и rename не атомарны: два процесса могли бы
    переименовать каждый свой ключ, и первый подписывал бы кэш ключом,
    которого на диске уже нет. O_EXCL решает «создать, если нет» одной
    операцией; проигравший читает ключ победителя.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(key_path, flags, 0o600)
    except FileExistsError:
        return _existing_key(key_path)
    try:
        os.write(descriptor, key)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return key


def _create_key(directory: Path, *, replace_broken: bool) -> bytes:
    with live_write(directory, f"{KEY_FILE_NAME}."):
        return _create_key_unguarded(directory, replace_broken=replace_broken)


def _create_key_unguarded(directory: Path, *, replace_broken: bool) -> bytes:
    key_path = directory / KEY_FILE_NAME
    temporary = directory / f"{KEY_FILE_NAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    key = secrets.token_bytes(_KEY_BYTES)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        try:
            os.write(descriptor, key)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if replace_broken:
            os.replace(temporary, key_path)
        else:
            try:
                # link, а не replace: два процесса, стартующих одновременно,
                # не должны перезаписать ключ друг друга — второй берёт первый.
                os.link(temporary, key_path)
            except FileExistsError:
                key = _existing_key(key_path)
            except OSError as error:
                if error.errno not in _NO_HARDLINK:
                    raise
                # Без жёстких ссылок (FUSE, bind-mount Docker Desktop) ключ
                # создаётся эксклюзивно прямо на месте.
                key = _create_key_in_place(key_path, key)
    except OSError as error:
        raise _KeyUnavailable(str(error)) from error
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return key


def _key_being_written(key_path: Path) -> bool:
    """Пустой обычный файл, созданный только что, — запись соперника."""
    try:
        info = key_path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_size == 0
        and time.time() - info.st_mtime < _KEY_FRESH_SECONDS
    )


def _wait_for_key(key_path: Path) -> bytes | None:
    deadline = time.monotonic() + _KEY_WAIT_SECONDS
    while True:
        key = _read_key_file(key_path)
        if key is not None or time.monotonic() >= deadline:
            return key
        time.sleep(0.01)


def _existing_key(key_path: Path) -> bytes:
    existing = _wait_for_key(key_path)
    if existing is None:
        raise _KeyUnavailable("ключ кэша создаётся другим процессом")
    return existing


def _key_for(path: Path, *, create: bool) -> bytes:
    directory = path.parent
    with _KEYS_LOCK:
        cached = _KEYS.get(directory)
        if cached is not None:
            return cached
        directory_lock = _KEY_DIRECTORY_LOCKS.setdefault(directory, threading.Lock())
    # Замок каталога сериализует потоки одного процесса: замена битого ключа
    # (`replace_broken`) двумя потоками сразу подписала бы кэш ключом,
    # которого на диске уже нет.
    with directory_lock:
        with _KEYS_LOCK:
            cached = _KEYS.get(directory)
        if cached is not None:
            return cached
        key_path = directory / KEY_FILE_NAME
        key = _read_key_file(key_path)
        if key is None:
            if not create:
                raise _KeyUnavailable("ключа кэша нет")
            if _key_being_written(key_path):
                key = _existing_key(key_path)
            else:
                broken = key_path.is_symlink() or key_path.exists()
                key = _create_key(directory, replace_broken=broken)
        with _KEYS_LOCK:
            _KEYS[directory] = key
        return key


def _mac(key: bytes, header: bytes, blob: bytes | memoryview) -> bytes:
    digest = hmac.new(key, header, hashlib.sha256)
    digest.update(b"\n")
    digest.update(blob)
    return digest.hexdigest().encode("ascii")


def _stamp(source_sha256: str, kind: str) -> dict:
    return {
        "cache_version": CACHE_VERSION,
        # Файлы без подписи прежнего формата промахиваются уже на штампе.
        "mac": "hmac-sha256",
        "kind": kind,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "marshal": _MARSHAL_VERSION,
        "code": _code_digest(),
        "source": source_sha256,
    }


def save_blob(payload, path: str | Path, *, source_sha256: str, kind: str) -> Path | None:
    """Записать что угодно из примитивов. Атомарно: временный файл, потом замена.

    Без атомарности прерванная запись оставляет обрезанный файл на месте
    годного, и следующий старт поднимет полуиндекс.

    `None` — записать не удалось. Это не ошибка: кэш расходный.
    """
    target = Path(path)
    header = json.dumps(_stamp(source_sha256, kind), ensure_ascii=False).encode("utf-8")
    encoded = marshal.dumps(payload, _MARSHAL_VERSION)
    if len(encoded) > MAX_CACHE_PAYLOAD_BYTES:
        return None
    blob = gzip.compress(encoded, COMPRESS_LEVEL)
    if len(header) + 1 + len(blob) > MAX_CACHE_FILE_BYTES:
        return None

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        mac = _mac(_key_for(target, create=True), header, blob)
        # Уникальное имя через mkstemp (O_EXCL, 0600), а не предсказуемый
        # `<файл>.tmp`: write_bytes по такому имени шёл бы по подложенному
        # симлинку и перезаписывал его цель.
        with live_write(target.parent, f".{target.name}."):
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    # Крупный blob уже готов: склейка дублировала его в памяти.
                    stream.write(header + b"\n")
                    stream.write(mac + b"\n")
                    stream.write(blob)
                os.replace(temporary, target)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
    except _KeyUnavailable:
        return None
    except OSError:
        # Том смонтирован только на чтение, кончилось место, каталог занят —
        # ронять из-за кэша нечего: следующий старт просто построит заново.
        return None
    return target


def load_blob(
    path: str | Path,
    *,
    source_sha256: str,
    kind: str,
    max_file_bytes: int | None = None,
    max_payload_bytes: int | None = None,
):
    """Поднять записанное. `None` — кэш не годится, надо строить заново."""
    try:
        return _read_verified(
            Path(path), source_sha256, kind,
            max_file_bytes=max_file_bytes, max_payload_bytes=max_payload_bytes,
        )
    except _Miss:
        return None


def save(index: SearchIndex, path: str | Path, *, source_sha256: str, kind: str) -> Path | None:
    return save_blob(index.export_state(), path, source_sha256=source_sha256, kind=kind)


def _read_verified(
    path: Path, source_sha256: str, kind: str, *,
    max_file_bytes: int | None = None, max_payload_bytes: int | None = None,
) -> dict:
    """Прочитать файл, сверив штамп до разбора содержимого.

    Порядок принципиален: `marshal` на испорченных данных способен уронить
    интерпретатор, и перехватить это уже нельзя. Поэтому чужой блоб до него
    не доходит — сначала заголовок обычным JSON, и только потом всё остальное.
    """
    file_limit = (
        max(0, min(MAX_CACHE_FILE_BYTES, max_file_bytes))
        if max_file_bytes is not None else MAX_CACHE_FILE_BYTES
    )
    payload_limit = (
        max(0, min(MAX_CACHE_PAYLOAD_BYTES, max_payload_bytes))
        if max_payload_bytes is not None else MAX_CACHE_PAYLOAD_BYTES
    )
    try:
        if max_file_bytes is not None:
            # Малый маркер не должен читать большой файл или ждать FIFO.
            raw = read_record(path, limit=file_limit)
        else:
            with path.open("rb") as stream:
                raw = stream.read(file_limit + 1)
    except (OSError, RecordReadError) as error:
        raise _Miss(str(error)) from error
    if len(raw) > file_limit:
        raise _Miss("файл превышает предел размера")

    separator = raw.find(b"\n", 0, _MAX_HEADER_BYTES + 1)
    if separator < 0:
        raise _Miss("нет заголовка")
    head = raw[:separator]
    mac_end = separator + 1 + _MAC_HEX_BYTES
    if raw[mac_end : mac_end + 1] != b"\n":
        raise _Miss("нет подписи")
    # Заголовок мал, а сжатый blob может занимать сотни мегабайт. Срез view
    # сохраняет исходный буфер для HMAC и zlib без второй копии blob.
    view = memoryview(raw)
    mac = view[separator + 1 : mac_end]
    blob = view[mac_end + 1 :]

    try:
        stamp = json.loads(head.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise _Miss(f"заголовок не читается: {error}") from error

    if stamp != _stamp(source_sha256, kind):
        raise _Miss("штамп не совпал")

    # Подпись сверяется до распаковки и до marshal: подменённый блоб с верным
    # штампом дальше этой строки не проходит.
    try:
        key = _key_for(path, create=False)
    except _KeyUnavailable as error:
        raise _Miss(str(error)) from error
    if not hmac.compare_digest(mac, _mac(key, head, blob)):
        raise _Miss("подпись не совпала")

    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = decoder.decompress(blob, payload_limit + 1)
        if len(decoded) > payload_limit or decoder.unconsumed_tail:
            raise _Miss("распакованный кэш превышает предел размера")
        decoded += decoder.flush(payload_limit + 1 - len(decoded))
        if (
            len(decoded) > payload_limit
            or not decoder.eof
            or decoder.unused_data
        ):
            raise _Miss("gzip-поток кэша некорректен")
        return marshal.loads(decoded)
    except _Miss:
        raise
    except Exception as error:  # обрезанный или подменённый блоб
        raise _Miss(f"содержимое не читается: {error}") from error


def load(
    path: str | Path,
    payloads: dict,
    *,
    source_sha256: str,
    kind: str,
    synonyms: dict | None = None,
    aliases: dict | None = None,
) -> SearchIndex | None:
    """Поднять индекс из кэша. `None` — кэш не годится, надо строить."""
    state = load_blob(path, source_sha256=source_sha256, kind=kind)
    if state is None:
        return None
    try:
        return SearchIndex.from_state(state, payloads, synonyms=synonyms, aliases=aliases)
    except (KeyError, TypeError, ValueError):
        # Файл прочитался, но в нём не то, что мы кладём. Тоже промах.
        return None


def sweep(cache_dir: str | Path, keep: Iterable[str]) -> list[str]:
    """Убрать чужое: файлы исчезнувших источников и обрывки записи.

    Без уборки каталог растёт при каждом переименовании или удалении
    конфигурации. Сносить безопасно: кэш восстанавливается сам.
    """
    directory = Path(cache_dir)
    if not directory.is_dir():
        return []

    allowed = set(keep)
    removed: list[str] = []
    now = time.time()
    for path in sorted(directory.iterdir()):
        # Ключ подписи уборке не подлежит: без него обесценился бы весь кэш.
        if not path.is_file() or path.name in allowed or path.name == KEY_FILE_NAME:
            continue
        # Временный файл живой записи — не обрывок. Проверка после листинга:
        # запись регистрируется до создания файла.
        if being_written(path, now):
            continue
        try:
            path.unlink()
        except OSError:
            # Каталог только на чтение. Лишний файл там полежит, старт важнее.
            continue
        removed.append(path.name)
    return removed
