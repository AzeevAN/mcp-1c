"""Общий backend современного дашборда без HTML-представления.

Здесь живут единые правила авторизации, загрузки, фоновых заданий, поиска
и подготовки данных. HTTP API и SPA-маршруты определены в
``dashboard_runtime.py``.

Имена без подчёркивания — API для ``dashboard_runtime`` и ``server``
(авторизация, сессии, загрузка, журнал заданий, подготовка страниц);
имена с подчёркиванием — внутреннее устройство модуля.
"""

from __future__ import annotations
import asyncio
import errno
import hashlib
import hmac
import os
import re
import secrets
import shutil
import threading
import time
import traceback
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import BinaryIO, Callable
from urllib.parse import quote, urlsplit
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from . import tools
from .auth import same_token
from .heavy_operations import _settled
from .intake_v2 import CandidateTransport
from .loader import ExportError
from .runtime_config import (
    LOOPBACK_HOSTS,
    HostConfigurationError,
    allowed_hosts,
    split_host,
    token_is_strong,
)
from .registry import (
    Registry,
    RegistryChangedExternally,
    RegistryError,
)
from .search import FIELD_KIND_TITLES
from .syntax_model import KIND_TITLES
from .v8container import V8ContainerError

COOKIE = "mcp1c_session"

MAX_UPLOAD = 500 * 1024 * 1024

CHUNK = 1024 * 1024

MAX_QUERY_PHRASES = 32

MAX_UPLOAD_FIELDS = 1

MAX_UPLOAD_FIELD_SIZE = 4 * 1024

LEVEL_READ = "read"

LEVEL_ADMIN = "admin"

# Сессия браузера — бессрочный пропуск, пока жив процесс, если не ограничить
# её срок: украденная cookie работала бы до рестарта. 12 часов покрывают
# рабочий день без повторного входа. Предел числа нужен потому, что вход по
# скрипту иначе растит словарь без конца; вытесняется старейшая сессия того же
# уровня. Предел свой у каждого уровня: держатель токена чтения иначе 256
# входами выселял бы сессии администратора.
SESSION_TTL_SECONDS = 12 * 60 * 60

MAX_SESSIONS = 256

# Вне Docker токен может быть коротким, и онлайн-перебор ничем не сдерживался.
# Ограничение общее для формы входа и для заголовков (`X-Api-Token`,
# `Authorization: Bearer`, `X-Admin-Token`) на `/mcp`, API и `/admin/reload`:
# иначе перебор просто переходил с формы на заголовок.
#
# Считаются различные неверные токены, а не запросы: перебору нужны новые
# догадки, а клиент со старым токеном в цикле повторов шлёт одну и ту же.
# За NAT Docker Desktop все клиенты приходят с адреса шлюза, и такой клиент
# иначе отрезал бы от сервера всех. Десяти разных неудач за пять минут хватает
# на опечатки человека. Успешный вход счётчик не сбрасывает: окно истекает
# само, иначе держатель токена чтения перебирал бы токен администратора,
# чередуя догадки с успешными входами.
LOGIN_FAILURE_LIMIT = 10

LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60

MAX_TRACKED_LOGIN_CLIENTS = 4096

# Адрес клиента за proxy с доверием `*` подделывается левым элементом
# X-Forwarded-For, и лимит по адресу тогда обходится сменой адреса. Общий
# потолок различных неудач со всех адресов это останавливает. Чтобы волна
# перебора не отрезала уже работающих клиентов, под общим потолком
# продолжают проверяться адреса, успешно входившие за последние 12 часов.
GLOBAL_LOGIN_FAILURE_LIMIT = 100

MAX_KNOWN_GOOD_CLIENTS = 1024

# Порядок вставки словаря — порядок создания: первая запись старейшая.
_SESSIONS: dict[str, tuple[str, float]] = {}

# адрес → {отпечаток неверного токена → момент}; отпечаток — HMAC с ключом
# процесса, чтобы в памяти не лежали сами догадки.
_LOGIN_FAILURES: dict[str, dict[bytes, float]] = {}

_GLOBAL_FAILURES: dict[bytes, float] = {}

_KNOWN_GOOD_CLIENTS: dict[str, float] = {}

_FAILURE_KEY = secrets.token_bytes(32)

# Сессии читаются и из event loop, и из потоков run_in_threadpool.
_AUTH_LOCK = threading.Lock()

KIND_COLORS = {
    "Справочник": "#4c8ed9",
    "Документ": "#e0803c",
    "РегистрСведений": "#5aa469",
    "РегистрНакопления": "#3f8f6b",
    "РегистрБухгалтерии": "#2f7d5e",
    "РегистрРасчета": "#7fae55",
    "ПланСчетов": "#9b6bbf",
    "ПланВидовХарактеристик": "#8a72c4",
    "ПланВидовРасчета": "#a37fd0",
    "ПланОбмена": "#b07f9e",
    "Перечисление": "#c9a227",
    "Константа": "#9a9a9a",
    "ОбщийМодуль": "#7d7d7d",
    "Обработка": "#a8a8a8",
    "Отчет": "#a8a8a8",
    "БизнесПроцесс": "#c07b7b",
    "Задача": "#c07b7b",
}

KIND_FALLBACK = "#8f8f8f"

def admin_token() -> str:
    """Читается на каждый запрос, а не при импорте: так его подменяют тесты."""
    return os.environ.get("ADMIN_TOKEN", "")

def api_token() -> str:
    """Токен чтения. Пуст — читать может кто угодно, как было всегда."""
    return os.environ.get("API_TOKEN", "")

def _token_from_headers(request: Request) -> str:
    """Токен из заголовка: `X-Api-Token`, `Authorization: Bearer` или
    `X-Admin-Token`.

    `X-Admin-Token` — заголовок, которым документирован `/admin/reload`; без
    него внешний слой при заданном `API_TOKEN` отвечал бы 401 раньше, чем
    маршрут сверит токен администратора. Админский токен даёт и чтение, так что
    этот заголовок годится для чтения так же, как два других.

    Заголовки HTTP кодируются latin-1, поэтому кириллический токен сюда
    физически не доходит — через форму входа он работает, через заголовок нет.
    """
    direct = request.headers.get("x-api-token", "")
    if direct:
        return direct
    auth = request.headers.get("authorization", "")
    prefix = "bearer "
    if auth.lower().startswith(prefix):
        return auth[len(prefix):]
    return request.headers.get("x-admin-token", "")

def storage_error_text(action: str, error: OSError) -> str:
    """Сообщение об отказе диска без путей: они уходят только в журнал.

    Текст `OSError` содержит абсолютный путь внутри контейнера или хоста.
    Оператору он нужен в журнале процесса, а в JSON браузеру достаточно
    кода ошибки — по нему видно, кончилось ли место или нет прав.
    """
    traceback.print_exception(error)
    code = errno.errorcode.get(error.errno or 0, "")
    suffix = f" ({code})" if code else ""
    return f"{action}{suffix}; подробности в журнале сервера."

def _drop_expired_sessions_locked(now: float) -> None:
    for session, (_, expires_at) in list(_SESSIONS.items()):
        if now >= expires_at:
            del _SESSIONS[session]

def create_session(level: str) -> str:
    """Выдать новую серверную сессию, вытеснив просроченные и старейшие."""
    session = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _AUTH_LOCK:
        _drop_expired_sessions_locked(now)
        same_level = [key for key, (owner, _) in _SESSIONS.items() if owner == level]
        for key in same_level[: max(0, len(same_level) - MAX_SESSIONS + 1)]:
            del _SESSIONS[key]
        _SESSIONS[session] = (level, now + SESSION_TTL_SECONDS)
    return session

def drop_session(session: str) -> None:
    with _AUTH_LOCK:
        _SESSIONS.pop(session, None)

def session_level(request: Request) -> str | None:
    """Уровень серверной браузерной сессии без раскрытия самой cookie."""
    session = request.cookies.get(COOKIE, "")
    if not session:
        return None
    with _AUTH_LOCK:
        entry = _SESSIONS.get(session)
        if entry is None:
            return None
        level, expires_at = entry
        if time.monotonic() >= expires_at:
            del _SESSIONS[session]
            return None
        return level

def _login_client(request: Request) -> str:
    return request.client.host if request.client is not None else ""

def _fresh(entries: dict, now: float, window: float) -> dict:
    for key, moment in list(entries.items()):
        if now - moment >= window:
            del entries[key]
    return entries

def _retry_after_locked(client: str, now: float) -> int | None:
    own = _fresh(_LOGIN_FAILURES.get(client, {}), now, LOGIN_FAILURE_WINDOW_SECONDS)
    if not own:
        _LOGIN_FAILURES.pop(client, None)
    elif len(own) >= LOGIN_FAILURE_LIMIT:
        return max(1, int(min(own.values()) + LOGIN_FAILURE_WINDOW_SECONDS - now) + 1)
    _fresh(_GLOBAL_FAILURES, now, LOGIN_FAILURE_WINDOW_SECONDS)
    _fresh(_KNOWN_GOOD_CLIENTS, now, SESSION_TTL_SECONDS)
    if (
        len(_GLOBAL_FAILURES) >= GLOBAL_LOGIN_FAILURE_LIMIT
        and client not in _KNOWN_GOOD_CLIENTS
    ):
        return max(
            1,
            int(min(_GLOBAL_FAILURES.values()) + LOGIN_FAILURE_WINDOW_SECONDS - now) + 1,
        )
    return None

def _record_failure_locked(client: str, given: str, now: float) -> None:
    fingerprint = hmac.new(_FAILURE_KEY, given.encode("utf-8"), hashlib.sha256).digest()[:16]
    own = _LOGIN_FAILURES.pop(client, {})
    own[fingerprint] = now
    # Повторная вставка переносит адрес в конец: вытесняется самый давний.
    _LOGIN_FAILURES[client] = own
    while len(_LOGIN_FAILURES) > MAX_TRACKED_LOGIN_CLIENTS:
        del _LOGIN_FAILURES[next(iter(_LOGIN_FAILURES))]
    _GLOBAL_FAILURES[client.encode("utf-8", "replace") + b"\0" + fingerprint] = now
    while len(_GLOBAL_FAILURES) > GLOBAL_LOGIN_FAILURE_LIMIT:
        del _GLOBAL_FAILURES[next(iter(_GLOBAL_FAILURES))]

def check_token(request: Request, given: str) -> tuple[str | None, int | None]:
    """Сверить предъявленный токен с учётом ограничения перебора.

    Возвращает (уровень, None) при совпадении, (None, None) при неверном или
    пустом токене и (None, секунды) при исчерпанных попытках. Под блокировкой
    сверяются только стойкие токены (`token_is_strong`): у короткого ответ
    выдал бы, что очередная догадка совпала.
    Проверка лимита, сверка и учёт неудачи идут под одним замком без `await`:
    параллельные придержанные запросы не получают лишних проб.
    """
    if not given:
        return None, None
    admin, api = admin_token(), api_token()
    if not admin and not api:
        return None, None
    client = _login_client(request)
    now = time.monotonic()
    with _AUTH_LOCK:
        retry_after = _retry_after_locked(client, now)
        if retry_after is not None:
            # Стойкий токен сверяется и под блокировкой: за NAT Docker все
            # клиенты приходят с одного адреса, и иначе десять чужих догадок
            # (в том числе с чужой страницы в браузере) отрезали бы всех.
            # Короткий токен под блокировкой не сверяется: ответ «совпало»
            # продолжал бы перебор, ради сдерживания которого блокировка
            # существует. Неверная догадка здесь не учитывается — окно
            # истекает само, а не продлевается атакующим.
            # «Стойкий» — не только длина: предсказуемую длинную строку
            # `token_is_strong` стойкой не считает, и она под блокировкой не
            # сверяется, как короткая.
            for token, level in ((admin, LEVEL_ADMIN), (api, LEVEL_READ)):
                if token_is_strong(token) and same_token(given, token):
                    return level, None
            return None, retry_after
        if same_token(given, admin):
            level = LEVEL_ADMIN
        elif same_token(given, api):
            level = LEVEL_READ
        else:
            _record_failure_locked(client, given, now)
            return None, None
        _KNOWN_GOOD_CLIENTS.pop(client, None)
        _KNOWN_GOOD_CLIENTS[client] = now
        while len(_KNOWN_GOOD_CLIENTS) > MAX_KNOWN_GOOD_CLIENTS:
            del _KNOWN_GOOD_CLIENTS[next(iter(_KNOWN_GOOD_CLIENTS))]
        return level, None

_HEADER_AUTH_KEY = "mcp1c.header_auth"

def header_token_check(request: Request) -> tuple[str | None, int | None]:
    """Результат сверки токена из заголовка, один раз на запрос.

    Права проверяются несколько раз за запрос (внешний слой, маршрут, флаг
    admin в ответе); без кэша одна неверная догадка считалась бы дважды.
    Кэш живёт в ASGI scope, общем для всех `Request` одного запроса.
    """
    cached = request.scope.get(_HEADER_AUTH_KEY)
    if cached is None:
        cached = check_token(request, _token_from_headers(request))
        request.scope[_HEADER_AUTH_KEY] = cached
    return cached

def admin_authorized(request: Request) -> bool:
    """Право записи: загрузка источников, удаление, правка словаря."""
    if session_level(request) == LEVEL_ADMIN:
        return True
    return header_token_check(request)[0] == LEVEL_ADMIN

def _origin_key(value: str, *, allow_path: bool) -> tuple[str, str, int] | None:
    """Канонический origin без зависимости от написания стандартного порта."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        scheme not in ("http", "https")
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or (not allow_path and (parsed.path not in ("", "/") or parsed.query))
        or parsed.fragment
    ):
        return None
    return scheme, hostname, port or (443 if scheme == "https" else 80)

def login_from_foreign_site(request: Request) -> bool:
    """Пришла ли форма входа со страницы другого сайта.

    Проверка не зависит от `host_check_required`: в контейнере проверка Host
    выключена, а `fetch(..., {mode: "no-cors"})` со страницы атакующего иначе
    тратил бы попытки общего для NAT-адреса лимита и блокировал вход всем.
    Без `Sec-Fetch-Site` и `Origin` запрос пропускается: так приходят
    не-браузерные клиенты, и источник у них не определён.
    """
    if request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return True
    origin = request.headers.get("origin")
    if origin is None:
        return False
    actual = _origin_key(origin.strip(), allow_path=False)
    host = split_host(request.headers.get("host", ""))
    if actual is None:
        return True
    scheme, hostname, port = actual
    if host is not None:
        host_name, host_port = host
        # Стандартный порт в Host не пишут; схему Host не несёт, поэтому порт
        # по умолчанию берётся из схемы самого Origin.
        host_port = host_port or (443 if scheme == "https" else 80)
        if (hostname, port) == (host_name.strip("[]"), host_port):
            return False
    # Proxy с настройками по умолчанию (nginx: `Host: $proxy_host`) передаёт
    # внутренний адрес, и Origin публичного имени с Host не совпадёт. Имена,
    # объявленные оператором как свои, — тоже свой сайт.
    return not _origin_declared(scheme, hostname, port)


def _origin_declared(scheme: str, hostname: str, port: int) -> bool:
    try:
        declared = allowed_hosts()
    except HostConfigurationError:
        return False
    for entry in declared:
        # Loopback-имена объявлены по умолчанию для проверки Host, но для
        # формы входа они не «свои»: страница на другом локальном порту —
        # чужой сайт.
        if entry in LOOPBACK_HOSTS:
            continue
        split = split_host(entry)
        if split is None:
            continue
        name, entry_port = split
        # Имя без порта — только стандартный порт схемы самого Origin:
        # страница на другом порту того же имени для формы входа чужая.
        expected = entry_port or (443 if scheme == "https" else 80)
        if name.strip("[]").lower().rstrip(".") == hostname and expected == port:
            return True
    return False


def _cookie_mutation_is_same_origin(request: Request) -> bool:
    """Не дать браузерной cookie авторизовать запись с соседнего host.

    Явный админский заголовок не является фоновым полномочием браузера и потому
    не требует CSRF-проверки, даже если клиент заодно прислал cookie.
    """
    if session_level(request) is None:
        return True
    if header_token_check(request)[0] == LEVEL_ADMIN:
        return True

    origin = request.headers.get("origin", "")
    source = origin or request.headers.get("referer", "")
    if not source:
        return False
    expected = _origin_key(str(request.base_url), allow_path=True)
    actual = _origin_key(source, allow_path=not bool(origin))
    return expected is not None and actual == expected

def csrf_denied(request: Request) -> PlainTextResponse | None:
    if _cookie_mutation_is_same_origin(request):
        return None
    return PlainTextResponse(
        "Cookie-сессия не подтверждена заголовком Origin или Referer этого сервера.",
        status_code=403,
    )

class UploadTooLarge(MultiPartException):
    """File-part пересёк границу до следующей записи в spool."""

class UploadStorageFull(RuntimeError):
    """Во временном хранилище multipart кончилось место или квота."""

class _ProtectedUploadFile(UploadFile):
    """Не закрывать spool, пока его read/write ещё выполняется в потоке."""

    async def write(self, data: bytes) -> None:
        await _settled(asyncio.create_task(super().write(data)))

    async def read(self, size: int = -1) -> bytes:
        return await _settled(asyncio.create_task(super().read(size)))

    async def seek(self, offset: int) -> None:
        await _settled(asyncio.create_task(super().seek(offset)))

    async def close(self) -> None:
        await _settled(asyncio.create_task(super().close()))


class _LimitedUploadParser(MultiPartParser):
    """Multipart с одним файлом и ограниченным временным spool-файлом."""

    def __init__(
        self,
        request: Request,
        file_limit: int,
        *,
        file_factory: Callable[[], BinaryIO] | None = None,
    ) -> None:
        super().__init__(
            request.headers,
            request.stream(),
            max_files=1,
            max_fields=MAX_UPLOAD_FIELDS,
            max_part_size=MAX_UPLOAD_FIELD_SIZE,
        )
        self.file_limit = file_limit
        self._file_factory = file_factory
        self._current_file_size = 0

    def on_headers_finished(self) -> None:
        super().on_headers_finished()
        file = self._current_part.file
        if file is not None:
            source = file.file
            if self._file_factory is not None:
                source = self._file_factory()
                # Starlette закрывает отдельный список файлов при ошибке:
                # он должен владеть новым sink, а не пустым старым spool.
                self._files_to_close_on_error[-1] = source
                file.file.close()
            self._current_part.file = _ProtectedUploadFile(
                source, size=file.size, filename=file.filename, headers=file.headers,
            )

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_size = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._current_file_size += end - start
            if self._current_file_size > self.file_limit:
                raise UploadTooLarge(
                    f"File-part превысил предел {self.file_limit} байт."
                )
        super().on_part_data(data, start, end)

async def limited_upload_form(
    request: Request,
    file_limit: int | None = None,
    *,
    allowed_fields: frozenset[str] = frozenset(),
    file_factory: Callable[[], BinaryIO] | None = None,
) -> FormData:
    """Разобрать загрузку, не принимая лишние файлы и поля."""
    if not request.headers.get("content-type", "").lower().startswith(
        "multipart/form-data"
    ):
        raise MultiPartException("Ожидалась multipart/form-data.")
    parser = _LimitedUploadParser(
        request,
        MAX_UPLOAD if file_limit is None else file_limit,
        file_factory=file_factory,
    )
    try:
        form = await parser.parse()
    except BaseException as error:
        # Starlette закрывает spool при ошибке multipart, но отмена запроса
        # тоже должна освободить уже принятые части до возврата допуска.
        for file in parser._files_to_close_on_error:
            try:
                file.close()
            except OSError:
                pass
        if isinstance(error, OSError) and error.errno in (
            errno.ENOSPC,
            getattr(errno, "EDQUOT", -1),
        ):
            raise UploadStorageFull("недостаточно места для multipart") from error
        raise
    items = form.multi_items()
    files = [(name, value) for name, value in items if isinstance(value, UploadFile)]
    fields = [(name, value) for name, value in items if not isinstance(value, UploadFile)]
    if len(files) != 1 or files[0][0] != "file":
        await form.close()
        raise MultiPartException("Ожидался ровно один file-part с именем file.")
    if any(name not in allowed_fields for name, _ in fields):
        await form.close()
        raise MultiPartException("Получено неизвестное поле multipart.")
    return form

def can_read(request: Request) -> bool:
    """Право чтения: страницы дашборда и инструменты MCP.

    Админский токен годится и здесь — иначе владельцу пришлось бы держать в
    клиенте два заголовка вместо одного.
    """
    expected = api_token()
    if not expected:
        return True
    if session_level(request) is not None:
        return True
    return header_token_check(request)[0] is not None

JOB_READING = "принимается"

JOB_PARSING = "разбирается"

JOB_DONE = "готово"

JOB_FAILED = "ошибка"

JOBS: list[dict] = []

ФОНОВЫЕ: set[asyncio.Task] = set()

JOBS_KEPT = 10

def start_job(name: str, size: int) -> dict:
    job = {"name": name, "size": size, "state": JOB_READING, "error": "", "result": ""}
    JOBS.append(job)
    завершённые = [j for j in JOBS if j["state"] in (JOB_DONE, JOB_FAILED)]
    for лишнее in завершённые[:-JOBS_KEPT]:
        JOBS.remove(лишнее)
    return job

def _index_source(registry: Registry, path: Path, suffix: str) -> None:
    """Выполняется в отдельном потоке: разбор .hbk занимает около 4 секунд."""
    for попытка in range(2):
        if suffix == ".hbk":
            registry.add_syntax(path)
        else:
            registry.add_configuration(
                path,
                source_transport=CandidateTransport.BROWSER,
                same_edition_only=True,
            )
        try:
            registry.save()
            return
        except RegistryChangedExternally:
            # Второй процесс (CLI) записал registry.json между публикацией в
            # памяти и сохранением. Reload вернул бы его строку поверх новой,
            # поэтому после него загрузка повторяется целиком: add + save.
            if попытка:
                raise
            registry.reload_if_changed()

def run_job(
    registry: Registry,
    job: dict,
    directory: str,
    path: Path,
    suffix: str,
) -> None:
    """Разбор в фоне. Ошибку кладём в задание: ответа, куда её вернуть, уже нет."""
    job["state"] = JOB_PARSING
    try:
        _index_source(registry, path, suffix)
    except (ExportError, RegistryError, V8ContainerError, ValueError) as error:
        job["state"] = JOB_FAILED
        job["error"] = str(error)
    except Exception as error:  # фоновая задача не должна ронять процесс молча
        job["state"] = JOB_FAILED
        # Текст неожиданного исключения может содержать пути и фрагменты
        # данных; в задание идёт только класс, полный стек — в журнал.
        job["error"] = f"{type(error).__name__}; подробности в журнале сервера."
        # На странице остаётся одна строка — больше туда не влезет. Но без
        # стека неожиданную ошибку разбирать не по чему: имя класса и текст
        # не говорят, где именно упало. Один раз это уже стоило отдельного
        # расследования, поэтому стек уходит в лог контейнера.
        traceback.print_exc()
    else:
        job["state"] = JOB_DONE
    finally:
        # `directory` — временный каталог browser-upload; исходный incoming
        # в двухфазном intake принадлежит человеку и здесь не удаляется.
        shutil.rmtree(directory, ignore_errors=True)

_СКАНЕРЫ: dict[str, "IncomingScanner"] = {}
_СКАНЕРЫ_LOCK = threading.Lock()

def _scanner(registry: Registry) -> "IncomingScanner":
    """Сканер один на каталог данных: его словарь состояния (хеши и отказы
    файлов `incoming/`) живёт в памяти под одним замком и пишется в
    `incoming-state.json` целиком. Второй экземпляр на тот же каталог держал
    бы свою копию и затирал бы записи первого."""
    from .incoming import IncomingScanner

    ключ = str(registry.data_dir)
    with _СКАНЕРЫ_LOCK:
        if ключ not in _СКАНЕРЫ:
            _СКАНЕРЫ[ключ] = IncomingScanner(registry)
        return _СКАНЕРЫ[ключ]

SCOPES = {
    "objects": "объектам",
    "fields": "реквизитам",
    "syntax": "справке платформы",
}

def run_queries(
    registry: Registry, config: str | None, scope: str, phrases: list[str]
) -> list[tuple[str, list, list]]:
    """Прогон фраз по выбранному индексу. Пять попаданий на фразу.

    Возвращает ещё и то, что отсеял фильтр версии. Молча отбрасывать нельзя:
    человек прочтёт «ничего не найдено» там, где верный вывод — «метод есть,
    но не в этой версии платформы».
    """
    context = registry.resolve(config, require_configuration=(scope != "syntax"))

    if scope == "syntax":
        if context.syntax is None:
            raise RegistryError("Справка платформы не подключена.")
        index = context.syntax.index
        # Фильтр по версии обязателен: без него дашборд покажет методы,
        # которых в этой конфигурации нет. Смысл фильтрации в том, что
        # предупреждение человек пропустит, а отсутствие в выдаче — нет.
        keep = context.syntax_filter()
    else:
        index = (
            context.configuration.index
            if scope == "objects"
            else context.configuration.field_index
        )
        keep = None

    result: list[tuple[str, list, list]] = []
    for phrase in phrases:
        hits = index.search(
            phrase, limit=5,
            predicate=(
                lambda doc: tools._поисковый_элемент_справки(context, doc.payload)
                and keep(doc.payload)
            ) if keep is not None else None,
        )
        hidden: list = []
        if keep is not None:
            # Выборка скрытого не отнимает места у доступных результатов.
            raw = index.search(
                phrase, limit=20,
                predicate=lambda doc: tools._поисковый_элемент_справки(context, doc.payload),
            )
            hidden = [hit for hit in raw if not keep(hit.doc.payload)]
        result.append((phrase, hits, hidden[:5]))
    return result

_RE_CODE = re.compile(r"`([^`]+)`")

_RE_BOLD = re.compile(r"\*\*([^*]+)\*\*")

def _inline(text: str) -> str:
    """Внутристрочная разметка. Экранирование — до неё, а не после.

    Порядок важен: сначала гасим угловые скобки, потом ставим свои теги.
    Наоборот — и `<code>` из нашей же разметки уехал бы в `&lt;code&gt;`.
    """
    готово = escape(text)
    готово = _RE_CODE.sub(r"<code>\1</code>", готово)
    return _RE_BOLD.sub(r"<b>\1</b>", готово)

def _блок_кода(строки: list[str], начало: int) -> tuple[str, int]:
    """Ограждённый блок в `<pre><code>` и номер первой строки после него.

    Язык после тройки отбрасывается: подсветки на странице нет. Незакрытый
    блок доходит до конца текста — остаток страницы при этом виден, просто
    внутри `<pre>`, а не абзацами.
    """
    конец = начало + 1
    while конец < len(строки) and not строки[конец].strip().startswith("```"):
        конец += 1
    тело = "\n".join(строки[начало + 1 : конец])
    return f"<pre><code>{escape(тело)}</code></pre>", конец + 1

def _ячейки_строки(строка: str) -> list[str] | None:
    """Ячейки markdown-строки таблицы. `None`, если это не строка таблицы."""
    голая = строка.strip()
    if not голая.startswith("|") or not голая.endswith("|"):
        return None
    # Экранированная черта — значение, а не граница ячейки.
    return [
        ячейка.strip().replace("\x00", "|")
        for ячейка in голая[1:-1].replace("\\|", "\x00").split("|")
    ]

def _таблица_markdown(строки: list[str], начало: int) -> tuple[str, int] | None:
    """Таблица, начинающаяся на этой строке. `None`, если её здесь нет.

    Возвращает готовый HTML и номер первой строки после таблицы.
    """
    шапка = _ячейки_строки(строки[начало])
    if шапка is None or начало + 1 >= len(строки):
        return None
    разделитель = _ячейки_строки(строки[начало + 1])
    if разделитель is None or not разделитель:
        return None
    if not all(set(ячейка) <= set("-: ") and "-" in ячейка for ячейка in разделитель):
        return None

    части = ["<tr>" + "".join(f"<th>{_inline(я)}</th>" for я in шапка) + "</tr>"]
    номер = начало + 2
    while номер < len(строки):
        ячейки = _ячейки_строки(строки[номер])
        if ячейки is None:
            break
        части.append("<tr>" + "".join(f"<td>{_inline(я)}</td>" for я in ячейки) + "</tr>")
        номер += 1
    return "<table>" + "".join(части) + "</table>", номер

def render_markdown(text: str) -> str:
    """Markdown из `render.py` в HTML — ровно то, что он порождает.

    Вложенных списков в его выводе нет, поэтому и здесь их нет: разбирать то,
    чего не бывает, значит держать код, который никто не проверяет. Замер
    вывода: 308 пунктов списка, 296 фрагментов кода, 34 заголовка, 6 жирных,
    2 цитаты.

    Ограждённые блоки (```` ```bsl ````) печатались абзацами с самого начала:
    сигнатуры и примеры теряли моноширинный вид и отступы, а карточка справки
    из них и состоит. Внутри блока разметка не разбирается — решётка и дефис
    там часть кода. Незакрытый блок доводится до конца текста, но остаток
    страницы не теряется: он остаётся видимым внутри `<pre>`.

    Таблицы появились вместе с разделением показа и поиска в справке языка
    запросов: страница отдаёт их отдельным полем, и карточка печатает их
    markdown-таблицей. Признаём таблицей только пару «строка с чертами плюс
    строка-разделитель»: одиночная черта в строке — обычный знак, в лесенке
    грамматики она стоит в каждой второй строке.
    """
    строки = text.splitlines()
    куски: list[str] = []
    список: list[str] = []
    цитата: list[str] = []
    номер = 0

    def закрыть_список() -> None:
        if список:
            куски.append("<ul>" + "".join(f"<li>{i}</li>" for i in список) + "</ul>")
            список.clear()

    def закрыть_цитату() -> None:
        if цитата:
            куски.append("<blockquote>" + "<br>".join(цитата) + "</blockquote>")
            цитата.clear()

    while номер < len(строки):
        строка = строки[номер]
        номер += 1
        голая = строка.rstrip()

        if голая.startswith("```"):
            закрыть_список()
            закрыть_цитату()
            разметка, номер = _блок_кода(строки, номер - 1)
            куски.append(разметка)
            continue

        таблица = _таблица_markdown(строки, номер - 1)
        if таблица is not None:
            разметка, следующая = таблица
            закрыть_список()
            куски.append(разметка)
            номер = следующая
            continue

        if голая.startswith("> ") or голая == ">":
            # Пустая строка внутри цитаты пишется одним знаком `>` без пробела.
            # Без этой ветки цитата разваливалась на куски с абзацами `&gt;`
            # между ними — видно на оговорке про неограниченные строки.
            закрыть_список()
            цитата.append(_inline(голая[2:].lstrip("- ")))
            continue
        закрыть_цитату()

        if голая.strip() == "---":
            закрыть_список()
            куски.append("<hr>")
        elif голая.startswith("#"):
            закрыть_список()
            уровень = len(голая) - len(голая.lstrip("#"))
            куски.append(f"<h{уровень}>{_inline(голая[уровень:].strip())}</h{уровень}>")
        elif голая.startswith("- ") or голая.startswith("* "):
            список.append(_inline(голая[2:]))
        elif голая.strip():
            закрыть_список()
            куски.append(f"<p>{_inline(голая)}</p>")
        else:
            закрыть_список()

    закрыть_список()
    закрыть_цитату()
    return "".join(куски)

def card_link(scope: str, config: str, hit) -> str:
    """Куда ведёт имя в таблице результатов.

    У реквизита своей карточки нет — ведём на объект-владелец. У элемента
    справки идентификатор в индексе служебный (`objects/catalog63/…`), а
    `get_syntax` принимает имя, поэтому берём `full_ru`.
    """
    if scope == "syntax":
        # Точный адрес сохраняет владельца и не смешивает одноимённые элементы.
        name = getattr(hit.doc.payload, "address", "") or hit.doc.id
        page = "/syntax"
    elif scope == "fields":
        name = getattr(hit.doc.payload, "object_full_name", "") or hit.doc.id
        page = "/object"
    else:
        name = hit.doc.id
        page = "/object"
    return f"{page}?config={quote(config)}&name={quote(name)}"

def card_text(
    registry: Registry,
    kind: str,
    config: str,
    name: str,
    detail: str,
    links_offset: int = 0,
) -> str:
    """Буквальный текст карточки для MCP и JSON API SPA."""
    if kind == "syntax":
        return tools.get_syntax(registry, name, config or None, detail, links_offset)
    return tools.get_object(registry, name, config or None, detail)

def kind_title(scope: str, kind: str) -> str:
    """Подпись вида найденного элемента — той же таблицей, что видит агент.

    Для `fields` берётся та же подпись, что печатает CLI и MCP
    (`FIELD_KIND_TITLES`); для `objects` вид уже человеческое слово
    («Справочник», «Документ») и не нуждается в замене.
    """
    if scope == "syntax":
        return KIND_TITLES.get(kind, kind)
    if scope == "fields":
        return FIELD_KIND_TITLES.get(kind, kind)
    return kind

def hidden_reason(item) -> str:
    """Человекочитаемое объяснение фильтра версии для SPA."""
    if item.until:
        return f"описан по версию {item.until} включительно, дальше его нет"
    if item.since:
        return f"появился в {item.since}"
    return "недоступен в этой версии"

@dataclass(frozen=True, slots=True)
class _OrphanRow:
    relative: str
    size: int

@dataclass(frozen=True, slots=True)
class _IncomingRow:
    name: str
    size: int
    state: str
    detail: str
    settling: bool

@dataclass(frozen=True, slots=True)
class _JobRow:
    name: str
    size: int
    state: str
    error: str

@dataclass(frozen=True, slots=True)
class _SourcesPageData:
    sources: tools.SourcesSnapshot
    sources_error: str
    orphans: tuple[_OrphanRow, ...]
    incoming: tuple[_IncomingRow, ...]
    incoming_exists: bool
    incoming_dir: str
    jobs: tuple[_JobRow, ...]
    authorized: bool

def prepare_sources_page(
    registry: Registry,
    *,
    authorized: bool,
) -> _SourcesPageData:
    """Снять данные, пройти диск и только затем подтвердить поколение."""

    def collect(
        sources: tools.SourcesSnapshot, sources_error: str = ""
    ) -> _SourcesPageData:
        orphans = tuple(
            _OrphanRow(path.relative_to(registry.data_dir).as_posix(), size)
            for path, size in registry.orphan_sources()
        )
        if authorized:
            # Сканер общий на data_dir, но runtime реестра принадлежит
            # текущему запросу, а не первому создателю сканера.
            active_hashes = frozenset(
                generation.manifest.raw_sha256
                for generation in registry.snapshot().generations.values()
            )
            incoming = tuple(
                _IncomingRow(
                    name=str(row["name"]),
                    size=int(row["size"]),
                    state=str(row["state"]),
                    detail=str(row["detail"]),
                    settling=bool(row.get("settling")),
                )
                for row in _scanner(registry).scan(active_hashes=active_hashes)
            )
            incoming_exists = registry.incoming_dir.is_dir()
        else:
            incoming = ()
            incoming_exists = False
        jobs = tuple(
            _JobRow(
                name=str(job["name"]),
                size=int(job["size"]),
                state=str(job["state"]),
                error=str(job["error"]),
            )
            for job in JOBS
        )
        return _SourcesPageData(
            sources=sources,
            sources_error=sources_error,
            orphans=orphans,
            incoming=incoming,
            incoming_exists=incoming_exists,
            incoming_dir=str(registry.incoming_dir),
            jobs=jobs,
            authorized=authorized,
        )

    last: _SourcesPageData | None = None
    for _ in range(2):
        prepared = tools._capture_sources_snapshot(registry)
        if prepared is None:
            continue
        sources, capture = prepared
        last = collect(sources)
        if tools._sources_snapshot_is_current(registry, capture):
            return last

    error = (
        "Источники изменились дважды; повторите запрос после завершения загрузки."
    )
    empty = tools.SourcesSnapshot((), (), ())
    if last is None:
        return collect(empty, error)
    return _SourcesPageData(
        sources=empty,
        sources_error=error,
        orphans=last.orphans,
        incoming=last.incoming,
        incoming_exists=last.incoming_exists,
        incoming_dir=last.incoming_dir,
        jobs=last.jobs,
        authorized=last.authorized,
    )

def apply_dictionary_change(registry: Registry, mutation):
    """Записать словарь и подхватить его без перезапуска.

    Пересборки индексов не требуется: ни синонимы, ни псевдонимы не участвуют
    в построении постингов, они читаются в момент поиска.
    """
    return registry.mutate_dictionary(mutation)
