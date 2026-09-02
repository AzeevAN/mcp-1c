"""MCP-сервер: тонкая обёртка над `tools.py`.

Единственный модуль проекта, зависящий от внешней библиотеки. Вся логика
живёт в `tools.py` и работает без неё — протокольный слой меняется чаще,
чем предметная область.

Протокол реализуется официальным SDK, а не руками. Разобранный аналог
(`1c-syntax-helper-mcp`) написал JSON-RPC поверх FastAPI самостоятельно, и
подключение к VS Code у него делается обёрткой `command: curl` вокруг
HTTP-эндпоинта — это не транспорт, а обходной путь.

Запуск::

    python3 -m mcp1c.server                      # HTTP на 127.0.0.1:8000/mcp
    python3 -m mcp1c.server --transport stdio    # для локальных клиентов
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import uuid
from functools import wraps
from pathlib import Path
from typing import Annotated, Callable, ParamSpec, TypeVar
from urllib.parse import urlencode

from mcp.server import MCPServer
from mcp.server.subscriptions import InMemorySubscriptionBus, ToolsListChanged
from mcp.types import CallToolResult, TextContent
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

from . import __version__, tools
from .auth import same_token
from .dashboard_backend import MAX_UPLOAD, can_read
from .dashboard_runtime import routes as dashboard_routes
from .process_restart import RestartController
from .reference_provider import (
    MAX_PAGE_CHARS,
    MAX_REFERENCE_ARTIFACT_BYTES,
    MIN_PAGE_CHARS,
    ReferenceQueryError,
    ReferenceService,
)
from .registry import Registry, RegistryError
from .role_access_service import (
    DEFAULT_PAGE_CHARS as DEFAULT_ROLE_PAGE_CHARS,
    MAX_ACCESS_LIMIT as MAX_ROLE_ACCESS_LIMIT,
    MAX_FIND_LIMIT as MAX_ROLE_FIND_LIMIT,
    MAX_PAGE_CHARS as MAX_ROLE_PAGE_CHARS,
    MIN_PAGE_CHARS as MIN_ROLE_PAGE_CHARS,
    RoleAccessQueryError,
    find_roles_payload,
    get_role_access_payload,
)
from .runtime_config import (
    ACCESS_HTTPS_PROXY,
    AccessModeError,
    TokenConfigurationError,
    access_mode,
    require_tokens,
)

# Лимит файла и лимит HTTP-тела различаются: multipart добавляет служебные
# заголовки и разделители. Остальные значения соответствуют цене операций:
# форма входа крошечная, пакет запросов вмещает 32 максимально длинные фразы,
# MCP и прочие маршруты получают безопасный общий запас.
HTTP_BODY_LIMIT_LOGIN = 16 * 1024
HTTP_BODY_LIMIT_QUERIES = 1024 * 1024
HTTP_BODY_LIMIT_UPLOAD = MAX_UPLOAD + 1024 * 1024
HTTP_BODY_LIMIT_REFERENCE = MAX_REFERENCE_ARTIFACT_BYTES + 1024 * 1024
HTTP_BODY_LIMIT_DEFAULT = 2 * 1024 * 1024

WRITABLE_DATA_DIRECTORIES = (
    "bootstrap",
    "incoming",
    "sources",
    "index",
    "modules",
    "extensions",
    "logs",
    "reference",
)


class DataDirectoryError(RuntimeError):
    """Каталог данных не подходит для изменяемого серверного запуска."""


def _data_directory_error(path: Path, error: OSError) -> DataDirectoryError:
    reason = error.strerror or str(error)
    return DataDirectoryError(
        f"Каталог данных `{path}` недоступен для записи: {reason}. "
        f"Процесс запущен с uid={os.geteuid()} gid={os.getegid()}. "
        "Для обычного Docker на Linux остановите контейнер и назначьте "
        "владельца каталога, подключённого как `/data`, на хосте: "
        "`sudo chown -R 10001:10001 <MCP1C_DATA_DIR>`. "
        "Не исправляйте это через `chmod 777` и не запускайте сервер от root."
    )


def _probe_directory_write(path: Path) -> None:
    """Доказать создание и удаление файла фактическим системным вызовом."""
    name = f".mcp1c-write-test-{uuid.uuid4().hex}"
    fd: int | None = None
    created = False
    try:
        directory_fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
            created = True
            os.close(fd)
            fd = None
            os.unlink(name, dir_fd=directory_fd)
            created = False
        finally:
            if fd is not None:
                os.close(fd)
            if created:
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)
    except OSError as error:
        raise _data_directory_error(path, error) from error


def require_writable_data(data_dir: str | Path) -> None:
    """Подготовить и проверить каталоги, которые изменяет живой сервер.

    Проверка включается отдельным флагом у Docker-образа. Обычный запуск CLI
    сохраняет возможность читать готовый Registry с read-only носителя; у
    контейнера с загрузкой, словарём и входящими архивами запись обязательна.
    """
    root = Path(data_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _data_directory_error(root, error) from error
    if not root.is_dir():
        raise _data_directory_error(root, NotADirectoryError(str(root)))

    _probe_directory_write(root)
    for name in WRITABLE_DATA_DIRECTORIES:
        directory = root / name
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise _data_directory_error(directory, error) from error
        if not directory.is_dir():
            raise _data_directory_error(directory, NotADirectoryError(str(directory)))
        _probe_directory_write(directory)


def _http_body_limit(scope) -> int:
    if scope.get("method") == "POST":
        path = scope.get("path", "")
        if path == "/login":
            return HTTP_BODY_LIMIT_LOGIN
        if path == "/queries":
            return HTTP_BODY_LIMIT_QUERIES
        if path in (
            "/sources",
            "/api/v1/sources/upload",
            "/api/v1/sources/intake/upload",
        ):
            return HTTP_BODY_LIMIT_UPLOAD
        if path == "/api/v1/reference/upload":
            return HTTP_BODY_LIMIT_REFERENCE
    return HTTP_BODY_LIMIT_DEFAULT


def http_body_guard(app):
    """Остановить тело до parsing по объявленным и фактически принятым байтам."""

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        limit = _http_body_limit(scope)
        raw_lengths = [
            value
            for name, value in scope.get("headers", ())
            if name.lower() == b"content-length"
        ]
        if len(raw_lengths) > 1:
            response = PlainTextResponse("Повторяющийся Content-Length.", 400)
            await response(scope, receive, send)
            return
        if raw_lengths:
            try:
                declared = int(raw_lengths[0])
            except ValueError:
                declared = -1
            if declared < 0:
                response = PlainTextResponse("Некорректный Content-Length.", 400)
                await response(scope, receive, send)
                return
            if declared > limit:
                response = PlainTextResponse(
                    f"Тело запроса превышает предел {limit} байт.", 413
                )
                await response(scope, receive, send)
                return

        received = 0
        response_started = False
        body_rejected = False

        async def limited_receive():
            nonlocal received, response_started, body_rejected
            if body_rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Ответ отправляется в момент пересечения границы, пока
                    # form/json parser ещё не получил тело целиком. Затем ему
                    # показывается disconnect; возможный внутренний 500
                    # от Starlette гасится в `tracked_send` ниже.
                    if response_started:
                        raise RuntimeError(
                            "HTTP-тело превысило предел после начала ответа."
                        )
                    body_rejected = True
                    response_started = True
                    response = PlainTextResponse(
                        f"Тело запроса превышает предел {limit} байт.", 413
                    )
                    await response(scope, receive, send)
                    return {"type": "http.disconnect"}
            return message

        async def tracked_send(message):
            nonlocal response_started
            if body_rejected:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await app(scope, limited_receive, tracked_send)
        except Exception:
            if not body_rejected:
                raise

    return wrapped

# Описания параметров попадают в JSON-схему инструментов, которую клиент
# получает при `tools/list`. Без них агент видит только тип и вынужден
# догадываться — например, что `config` принимает имя из `list_configurations`,
# а не путь к файлу.
CONFIG_PARAM = Annotated[
    str | None,
    Field(
        description=(
            "Имя конфигурации 1С, как его вернул `list_configurations` "
            "(например «ОтраслеваяКонфигурация»). Обязателен, если загружено "
            "больше одной конфигурации: по умолчанию ничего не подставляется, "
            "иначе ответ может относиться к чужой конфигурации."
        )
    ),
]

DETAIL_PARAM = Annotated[
    str,
    Field(
        description=(
            "Уровень детализации: `brief` — пара строк со счётчиками, "
            "`fields` — состав для написания кода, `full` — со свойствами и "
            "связями. Полное описание крупного документа занимает много "
            "контекста, поэтому `full` только когда связи действительно нужны."
        )
    ),
]

LIMIT_PARAM = Annotated[
    int,
    Field(
        description=(
            "Сколько результатов вернуть. По умолчанию 10, максимум 50 "
            "(большее молча урезается). Поднимать выше 10 стоит только когда "
            "нужного не оказалось в первой десятке: правильный ответ почти "
            "всегда в первой пятёрке, а длинная выдача тратит контекст."
        )
    ),
]

PROCEDURE_LIMIT_PARAM = Annotated[
    int,
    Field(
        ge=1,
        le=50,
        description=(
            "Сколько результатов вернуть отдельно на каждом уровне поиска: "
            "по точному имени и по словам. Целое число от 1 до 50; значение "
            "вне диапазона отклоняется, чтобы не скрывать ошибку вызова."
        )
    ),
]

CALLERS_LIMIT_PARAM = Annotated[
    int,
    Field(
        ge=1,
        le=50,
        strict=True,
        description=(
            "Сколько мест вызова показать: целое число от 1 до 50. "
            "Оставшееся число подтверждённых мест и модулей указывается "
            "отдельно; одноимённые места без разрешённой цели тоже не "
            "выводятся без границы."
        ),
    ),
]

PROCEDURE_START_LINE_PARAM = Annotated[
    int,
    Field(
        ge=0,
        strict=True,
        description=(
            "Номер первой строки окна тела, начиная с 0. Значение берётся "
            "из готового вызова продолжения в предыдущем ответе."
        ),
    ),
]

PROCEDURE_LINES_PARAM = Annotated[
    int,
    Field(
        ge=1,
        le=200,
        strict=True,
        description=(
            "Размер окна тела: целое число от 1 до 200. Если тело длиннее, "
            "ответ содержит готовый вызов следующего окна без пропусков."
        ),
    ),
]

ROLE_OPERATIONS_PARAM = Annotated[
    list[str],
    Field(
        min_length=1,
        max_length=6,
        description=(
            "Одна или несколько базовых операций: read, update, insert, "
            "delete, posting, use. Ответ всегда показывает точное право "
            "платформы, с которым сопоставлена каждая операция."
        ),
    ),
]

ROLE_FIND_LIMIT_PARAM = Annotated[
    int,
    Field(
        ge=1,
        le=MAX_ROLE_FIND_LIMIT,
        strict=True,
        description=(
            "Число ролей-кандидатов на странице, от 1 до 20. Следующая "
            "страница запрашивается по непрозрачному next_cursor."
        ),
    ),
]

ROLE_ACCESS_LIMIT_PARAM = Annotated[
    int,
    Field(
        ge=1,
        le=MAX_ROLE_ACCESS_LIMIT,
        strict=True,
        description=(
            "Число объявленных прав на странице, от 1 до 100. Большая роль "
            "никогда не возвращается одним ответом."
        ),
    ),
]

ROLE_MAX_CHARS_PARAM = Annotated[
    int,
    Field(
        ge=MIN_ROLE_PAGE_CHARS,
        le=MAX_ROLE_PAGE_CHARS,
        strict=True,
        description=(
            "Размер одного явно запрошенного окна RLS или шаблона: от 256 "
            "до 8000 символов. Продолжение приходит в next_cursor."
        ),
    ),
]

_P = ParamSpec("_P")
_T = TypeVar("_T")


def _safe_registry_error_message(error: RegistryError) -> str:
    """Скрыть имена локальных конфигураций в ошибке выбора для MCP."""
    message = str(error)
    if message.startswith(
        ("Загружено несколько конфигураций", "Конфигурация не загружена:")
    ):
        return (
            "Конфигурация не выбрана или не найдена. Сначала вызовите "
            "`list_configurations` и передайте точное имя в параметре `config`."
        )
    return message


def _expected_registry_errors(
    function: Callable[_P, _T],
) -> Callable[_P, _T | CallToolResult]:
    """Вернуть ожидаемую предметную ошибку как результат ``tools/call``."""

    @wraps(function)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _T | CallToolResult:
        try:
            return function(*args, **kwargs)
        except RegistryError as error:
            message = _safe_registry_error_message(error)
            return CallToolResult(
                content=[TextContent(type="text", text=message)],
                structured_content={"result": message},
                is_error=True,
            )

    return guarded


def _expected_role_errors(
    function: Callable[_P, _T],
) -> Callable[_P, _T | CallToolResult]:
    """Ожидаемые ошибки role-запроса остаются bounded tools/call result."""

    @wraps(function)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _T | CallToolResult:
        try:
            return function(*args, **kwargs)
        except RegistryError as error:
            message = _safe_registry_error_message(error)
        except RoleAccessQueryError as error:
            message = str(error)
        return CallToolResult(
            content=[TextContent(type="text", text=message)],
            structured_content={"result": message},
            is_error=True,
        )

    return guarded


class MCP1CServer(MCPServer):
    """MCPServer с атомарно меняющейся парой условных role-tools."""

    def __init__(self, *args, **kwargs):
        self._role_subscriptions = InMemorySubscriptionBus()
        kwargs["subscriptions"] = self._role_subscriptions
        self._role_catalog_lock = threading.RLock()
        self._role_registry: Registry | None = None
        self._role_tool_specs: tuple[tuple[Callable, str, str], ...] = ()
        self._role_tools_registered = False
        super().__init__(*args, **kwargs)

    async def list_tools(self):
        # В SDK добавление идёт по одному имени. Общий lock не даёт
        # tools/list увидеть половину пары между add_tool/remove_tool.
        with self._role_catalog_lock:
            return await super().list_tools()

    def configure_role_tools(
        self,
        registry: Registry,
        specs: tuple[tuple[Callable, str, str], ...],
    ) -> None:
        self._role_registry = registry
        self._role_tool_specs = specs
        self._sync_role_tools()

    def _sync_role_tools(self) -> bool:
        registry = self._role_registry
        if registry is None:
            return False
        required = registry.has_ready_roles()
        with self._role_catalog_lock:
            if required == self._role_tools_registered:
                return False
            if required:
                added: list[str] = []
                try:
                    for function, name, description in self._role_tool_specs:
                        super().add_tool(
                            function,
                            name=name,
                            description=description,
                        )
                        added.append(name)
                except Exception:
                    for name in reversed(added):
                        super().remove_tool(name)
                    raise
            else:
                for _function, name, _description in self._role_tool_specs:
                    super().remove_tool(name)
            self._role_tools_registered = required
            return True

    async def refresh_role_tools(self) -> bool:
        """Обновить каталог и уведомить подписанные современные сессии."""
        changed = self._sync_role_tools()
        if changed:
            await self._role_subscriptions.publish(ToolsListChanged())
        return changed


INSTRUCTIONS = f"""
Справочник по структуре конфигураций 1С и синтаксису платформы.

Порядок работы:
1. `list_configurations` — узнать, какие конфигурации загружены и что по ним
   доступно. Если загружено больше одной, параметр `config` обязателен во
   всех остальных вызовах. Если вывод зависит от фактической активности
   расширений, сразу после этого вызовите `list_extensions`; без отдельного
   снимка он честно вернёт `unknown`.
2. `search_objects` — превратить человеческую формулировку («расходная
   накладная») в точное имя объекта.
3. `search_procedures` — найти уже существующий код по точному имени или
   словам. `scope` задаётся явно, когда модули конкретного объекта или один
   точный модуль должны быть первыми; из текста запроса область не угадывается.
4. `get_procedure` — прочитать оглавление найденного модуля либо сигнатуру,
   контекст компиляции и ограниченное окно тела точной процедуры.
5. `get_callers` — проверить места вызова точной процедуры в коде, привязки
   подписок и регламентных заданий, а также события формы.
6. `get_object` — состав объекта. Уровень `brief` для беглого взгляда,
   `fields` для написания кода, `full` когда нужны и связи.
7. `get_related` — что ещё задевает задача: движения документа, кто ссылается
   на справочник.
8. `search_syntax` / `get_syntax` — методы, свойства и объекты платформы.
   Выдача отфильтрована по версии конфигурации: то, чего в ней нет, не
   покажется. Обращайте внимание на поле «Доступность» — вызов серверного
   метода из клиентского контекста не скомпилируется.
9. `search_reference` / `get_reference`, если эти инструменты подключены, —
   общая справка, не зависящая от выбранной конфигурации: конструкции BSL,
   язык запросов, СКД, Конфигуратор и инструменты разработки. Когда предмет
   вопроса известен, обязательно задайте `domain`; поиск сразу по всем разделам
   нужен только для намеренно неоднозначного вопроса. Получите короткие
   результаты через `search_reference`, затем прочитайте точный `id` через
   `get_reference` и продолжайте по `next_cursor`, пока он не станет `null`.
   Известная версия появления, удаления или присутствия приходит даже без
   `platform`; целевая версия только добавляет проверку применимости. Не
   называйте `unknown` отсутствием данных, если рядом возвращён версионный факт.
   Если широкая естественная формулировка не дала нужного среди коротких
   результатов, повторите поиск с более короткой задачей и известным термином
   языка; не угадывайте `id`.

Не пропускайте шаг 6. Поиск объектов отдаёт только имена и счётчики; всё, от чего
зависит код, живёт в `get_object`:

* **вид и периодичность регистра** — `СрезПоследних` есть только у
  периодического регистра сведений, а непериодических большинство;
* **готовые имена полей виртуальных таблиц** — в запросе ресурс `Количество`
  называется `КоличествоОстаток`, `КоличествоОборот`, `КоличествоПриход`, и в
  конфигураторе таких имён не видно;
* **предел субконто, корреспонденция, ресурсы графика** — без них поля вида
  `СубконтоДт1` назвать нечем.

Запрос, написанный сразу после `search_objects`, выглядит правильным и падает
на «поле не найдено».

Перед вызовом функции платформы на старой конфигурации проверьте её через
`get_syntax`: недоступное помечается, и там же лежит рецепт замены, если он
записан (`СтрРазделить` появилась в 8.3.6 — на 8.3.5 нужен обход).

Не подменяйте общей справкой справку платформы: методы, свойства и объекты
платформы ищутся через `search_syntax` / `get_syntax`. Параметр `platform` у
общей справки не улучшает текстовый поиск, а только проверяет совместимость и
отделяет заведомо недоступные результаты. Если вопрос относится к загруженной
конфигурации, версию можно взять из `list_configurations`; иначе параметр
следует опустить.

Сервер не читает данные информационной базы. Код доступен только после загрузки
выгрузки конфигурации в файлы; `search_procedures` ищет по её процедурам, но не
по значениям документов, справочников и регистров. `get_procedure` читает
тело только по точному адресу и ограничивает его окном до 200 строк.
""".strip()


def build_server(
    registry: Registry,
    name: str = "mcp1c",
    *,
    reference: ReferenceService | None = None,
    restart: RestartController | None = None,
) -> MCP1CServer:
    if reference is None:
        reference = ReferenceService.discover(registry.data_dir)
    if restart is None:
        restart = RestartController(enabled=False)
    server = MCP1CServer(
        name=name,
        title="Структура конфигураций 1С",
        instructions=INSTRUCTIONS,
        version=__version__,
    )

    @server.tool(
        description=(
            "Какие конфигурации 1С загружены, на какой платформе и что по ним "
            "доступно. Вызывать первым: если конфигураций больше одной, "
            "параметр `config` обязателен во всех остальных инструментах, а "
            "имя для него берётся отсюда."
        )
    )
    @_expected_registry_errors
    def list_configurations() -> str:
        return tools.list_configurations(registry)

    @server.tool(
        description=(
            "Показать расширения, фактически действовавшие в снятом сеансе "
            "1С, не применённые расширения и порядок элементов в ответе API "
            "платформы. Вызывать после `list_configurations`, когда задача "
            "зависит от активности расширений. Без отдельного runtime-снимка "
            "возвращает `unknown`; позиция API не выдаётся за доказанный "
            "порядок исполнения модулей."
        )
    )
    @_expected_registry_errors
    def list_extensions(config: CONFIG_PARAM = None) -> str:
        return tools.list_extensions(registry, config)

    @server.tool(
        description=(
            "Найти объект конфигурации по описанию или части имени. "
            "Запрос можно писать по-человечески: «расходная накладная», "
            "«цены номенклатуры». "
            "Отдаёт только имена и счётчики — состава полей здесь нет. "
            "Прежде чем писать код или запрос по найденному объекту, вызовите "
            "`get_object`: вид и периодичность регистра, готовые имена полей "
            "виртуальных таблиц (`КоличествоОстаток`, `СубконтоДт1`) приходят "
            "только оттуда. Запрос, написанный сразу после поиска, выглядит "
            "правильным и падает на «поле не найдено»."
        )
    )
    @_expected_registry_errors
    def search_objects(
        query: Annotated[str, Field(
            description="Формулировка по-человечески («расходная накладная») "
                        "или часть имени объекта.")],
        config: CONFIG_PARAM = None,
        kind: Annotated[str | None, Field(
            description="Ограничить вид: Документ, Справочник, РегистрСведений, "
                        "РегистрНакопления, Перечисление, ОбщийМодуль и т. п.")] = None,
        limit: LIMIT_PARAM = 10,
    ) -> str:
        return tools.search_objects(registry, query, config, kind, limit)

    @server.tool(
        description=(
            "Найти процедуру или функцию в загруженном коде конфигурации либо "
            "одного выбранного расширения. Точное имя находит и неэкспортные "
            "процедуры; поиск по словам — только экспортные. Расширенная "
            "фраза о 12 типовых событиях разрешается в точное имя, но без "
            "`scope` не выбирает случайную реализацию. Для обычного поиска "
            "`scope` задаёт приоритет модулей, для распознанного события — "
            "ограничивает его реализации. Из query область не угадывается. "
            "Сигнатуры "
            "читаются из выгрузки в файлы только для показанных результатов."
        )
    )
    @_expected_registry_errors
    def search_procedures(
        query: Annotated[str, Field(
            description=(
                "Точное имя процедуры (`ПриЗаписи`) или слова из имени и "
                "комментария-шапки (`проверить остатки`), в том числе фраза "
                "о поддержанном типовом событии (`что выполняется при записи "
                "объекта`). Объект поиска из этого текста не угадывается — "
                "для него есть `scope`."
            )
        )],
        config: CONFIG_PARAM = None,
        extension: Annotated[str | None, Field(
            description=(
                "Имя одного загруженного расширения. Не задано — поиск идёт "
                "только по коду основной конфигурации, без примеси расширений."
            )
        )] = None,
        scope: Annotated[str | None, Field(
            description=(
                "Необязательный явный scope: объект (`Документ.ЧекККМ`) или "
                "точный адрес (`ОбщийМодуль.ОбщегоНазначения`). В обычном "
                "поиске его модули поднимаются, а распознанное типовое "
                "событие разрешается только внутри scope. Из query область "
                "не выводится."
            )
        )] = None,
        limit: PROCEDURE_LIMIT_PARAM = 10,
    ) -> str:
        return tools.search_procedures(
            registry, query, config, extension, scope, limit
        )

    @server.tool(
        description=(
            "Оглавление модуля либо карточка одной процедуры из загруженной "
            "выгрузки кода. Адрес модуля без `::` возвращает только "
            "оглавление без тела; `Модуль::Имя` возвращает сигнатуру, "
            "контекст компиляции и окно тела до 200 строк с готовым вызовом "
            "продолжения. Выбранное расширение читается отдельно от основной "
            "конфигурации; смысл его аннотации показывается явно."
        )
    )
    @_expected_registry_errors
    def get_procedure(
        address: Annotated[str, Field(
            description=(
                "Точный адрес модуля (`ОбщийМодуль.ОбщегоНазначения`) или "
                "процедуры (`ОбщийМодуль.ОбщегоНазначения::Проверить`)."
            )
        )],
        config: CONFIG_PARAM = None,
        extension: Annotated[str | None, Field(
            description=(
                "Имя одного загруженного расширения. Не задано — читается "
                "код основной конфигурации; чужие расширения в тело не "
                "подмешиваются."
            )
        )] = None,
        start_line: PROCEDURE_START_LINE_PARAM = 0,
        lines: PROCEDURE_LINES_PARAM = 200,
    ) -> str:
        return tools.get_procedure(
            registry, address, config, extension, start_line, lines
        )

    @server.tool(
        description=(
            "Кто вызывает точную процедуру: подтверждённые места в коде с "
            "процедурой-владельцем, привязки подписок и регламентных заданий "
            "из метаданных, элементы и события формы. Одноимённые вызовы без "
            "разрешённого модуля показываются отдельно и не приписываются "
            "запрошенному адресу. Тела модулей не читаются."
        )
    )
    @_expected_registry_errors
    def get_callers(
        address: Annotated[str, Field(
            description=(
                "Точный адрес процедуры `Модуль::Имя`, полученный из "
                "`search_procedures` или оглавления `get_procedure`."
            )
        )],
        config: CONFIG_PARAM = None,
        extension: Annotated[str | None, Field(
            description=(
                "Имя одного загруженного расширения. Не задано — места в "
                "коде ищутся только в основной конфигурации; чужой код не "
                "подмешивается."
            )
        )] = None,
        limit: CALLERS_LIMIT_PARAM = 20,
    ) -> str:
        return tools.get_callers(registry, address, config, extension, limit)

    @server.tool(
        description=(
            "Структура объекта конфигурации: реквизиты с типами, табличные "
            "части, движения, предопределённые. detail: brief | fields | full. "
            "Обязательный шаг перед написанием кода или запроса. Только здесь "
            "видно то, от чего код зависит и чего нет в поиске: вид и "
            "периодичность регистра (`СрезПоследних` есть лишь у "
            "периодического регистра сведений, а непериодических "
            "большинство), корреспонденция, предел субконто, и — для "
            "регистров — раздел «Таблицы запроса» с уже подставленными "
            "именами полей: ресурс `Количество` в запросе называется "
            "`КоличествоОстаток` или `КоличествоОборот`, и в конфигураторе "
            "таких имён не видно."
        )
    )
    @_expected_registry_errors
    def get_object(
        full_name: Annotated[str, Field(
            description="Полное имя объекта: `Документ.ЧекККМ`, "
                        "`Справочник.Номенклатура`. Получается из `search_objects`.")],
        config: CONFIG_PARAM = None,
        detail: DETAIL_PARAM = "fields",
    ) -> str:
        return tools.get_object(registry, full_name, config, detail)

    @server.tool(
        description=(
            "Прямые связи объекта: на что ссылается, кто ссылается на него, "
            "какие регистры двигает — с подписью, через какой реквизит или "
            "движение идёт каждая связь. Отвечает на «что ещё сломается, если "
            "тронуть». Только один шаг: дальше связь идёт через объекты, "
            "которые соединены почти со всем (дополнительные реквизиты, "
            "значения доступа), и список имён на втором шаге правдоподобен, но "
            "бессмыслен. Нужна цепочка через несколько объектов — стройте её "
            "повторными вызовами по конкретному имени из выдачи."
        )
    )
    @_expected_registry_errors
    def get_related(
        full_name: Annotated[str, Field(
            description="Полное имя объекта, например `Документ.ЧекККМ`.")],
        config: CONFIG_PARAM = None,
    ) -> str:
        return tools.get_related(registry, full_name, config)

    @server.tool(
        description=(
            "Сравнить один и тот же объект в двух конфигурациях: чем "
            "различается состав реквизитов."
        )
    )
    @_expected_registry_errors
    def compare_configurations(
        full_name: Annotated[str, Field(
            description="Полное имя объекта, которое ищется в обеих конфигурациях.")],
        configs: Annotated[list[str] | None, Field(
            description="Имена конфигураций из `list_configurations`. "
                        "Не заданы — берутся все загруженные.")] = None,
    ) -> str:
        return tools.compare_configurations(registry, full_name, configs)

    @server.tool(
        description=(
            "Найти метод, свойство или объект платформы 1С. "
            "Даёт только строку списка. "
            "Сигнатура, параметры, доступность по контекстам, версия появления "
            "и рецепт замены для старой платформы — в `get_syntax` по "
            "найденному имени."
        )
    )
    @_expected_registry_errors
    def search_syntax(
        query: Annotated[str, Field(
            description="Что ищем: «разделить строку», «ЗаписьJSON», «StrFind» "
                        "по платформе. Русские и английские имена равнозначны.")],
        config: CONFIG_PARAM = None,
        kind: Annotated[str | None, Field(
            description="Ограничить вид: method, property, event, object, "
                        "query_table, query_field.")] = None,
        limit: LIMIT_PARAM = 10,
    ) -> str:
        return tools.search_syntax(registry, query, config, kind, limit)

    @server.tool(
        description=(
            "Полное описание элемента платформы 1С (сигнатура, параметры, тип "
            "возврата, доступность, версия появления, пример). "
            "Вызывать перед использованием функции на старой конфигурации: "
            "недоступное в её версии помечается, и там же лежит рецепт "
            "замены, если он записан (`СтрРазделить` появилась в 8.3.6 — на "
            "8.3.5 нужен обход). Поле «Доступность» — тоже ошибка компиляции: "
            "серверный метод из клиентского контекста не соберётся."
        )
    )
    @_expected_registry_errors
    def get_syntax(
        name: Annotated[str, Field(
            description="Имя элемента платформы: `СтрНайти`, "
                        "`ЗаписьJSON.ЗаписатьНачалоОбъекта`, `ValueTable.Columns`. "
                        "Для членов объектов надёжнее указывать `Объект.Член`.")],
        config: CONFIG_PARAM = None,
        detail: DETAIL_PARAM = "fields",
    ) -> str:
        return tools.get_syntax(registry, name, config, detail)

    @_expected_role_errors
    def find_roles_for_access(
        full_name: Annotated[str, Field(
            description=(
                "Точное имя корневого объекта из search_objects, например "
                "Справочник.Заказы."
            )
        )],
        operations: ROLE_OPERATIONS_PARAM,
        config: CONFIG_PARAM = None,
        child_path: Annotated[str, Field(
            description=(
                "Необязательный точный source-B путь дочернего элемента "
                "парами Вид.Имя, например Attribute.Code."
            )
        )] = "",
        include_conditional: Annotated[bool, Field(
            description=(
                "Явно учитывать условные true с RLS как кандидатов. По "
                "умолчанию они исключены из доказанного покрытия."
            )
        )] = False,
        cursor: Annotated[str | None, Field(
            max_length=2048,
            description=(
                "Непрозрачный next_cursor предыдущей страницы кандидатов; "
                "не переносится между запросами или generation."
            )
        )] = None,
        limit: ROLE_FIND_LIMIT_PARAM = 10,
    ) -> str:
        payload = find_roles_payload(
            registry,
            full_name,
            operations,
            config=config,
            child_path=child_path,
            include_conditional=include_conditional,
            cursor=cursor,
            limit=limit,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @_expected_role_errors
    def get_role_access(
        role: Annotated[str, Field(
            max_length=512,
            description=(
                "Точное техническое имя роли из find_roles_for_access или "
                "страницы /roles."
            )
        )],
        config: CONFIG_PARAM = None,
        full_name: Annotated[str, Field(
            description=(
                "Необязательное точное имя корневого объекта: ограничивает "
                "страницу прав одной целью."
            )
        )] = "",
        cursor: Annotated[str | None, Field(
            max_length=2048,
            description=(
                "Непрозрачный next_cursor страницы прав или шаблонов; не "
                "переносится между ролями и generation."
            )
        )] = None,
        limit: ROLE_ACCESS_LIMIT_PARAM = 50,
        restriction_ref: Annotated[str, Field(
            max_length=2048,
            description=(
                "Явная ссылка restriction/template из обычной страницы. "
                "Только с ней возвращается текст RLS; роль целиком повторно "
                "не загружается."
            )
        )] = "",
        restriction_cursor: Annotated[str | None, Field(
            max_length=2048,
            description=(
                "Непрозрачный next_cursor следующего окна одного явно "
                "открытого RLS или шаблона."
            )
        )] = None,
        max_chars: ROLE_MAX_CHARS_PARAM = DEFAULT_ROLE_PAGE_CHARS,
    ) -> str:
        payload = get_role_access_payload(
            registry,
            role,
            config=config,
            full_name=full_name,
            cursor=cursor,
            limit=limit,
            restriction_ref=restriction_ref,
            restriction_cursor=restriction_cursor,
            max_chars=max_chars,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    server.configure_role_tools(
        registry,
        (
            (
                find_roles_for_access,
                "find_roles_for_access",
                "Подобрать роли по объекту и базовым операциям только из "
                "объявленных прав. Ответ не является эффективным доступом "
                "пользователя, показывает точное сопоставление операция → "
                "право платформы, explicit false, безусловные и RLS-права. "
                "Минимальный набор возвращается лишь при полном доказанном "
                "покрытии; кандидаты идут страницами.",
            ),
            (
                get_role_access,
                "get_role_access",
                "Прочитать объявленные права одной роли ограниченными "
                "страницами. Обычный ответ различает explicit false, "
                "безусловный true и условный true, но не содержит полный "
                "RLS. Текст одного RLS или шаблона открывается только явно "
                "по restriction_ref и дочитывается по next_cursor. Это не "
                "эффективный доступ пользователя.",
            ),
        ),
    )

    if reference.provider is not None:
        provider = reference.provider

        @server.tool(
            description=(
                "Найти конструкцию BSL, оператор языка запросов, функцию СКД "
                "или материал по Конфигуратору в общей справке, не привязанной "
                "к Registry и конкретной конфигурации. Это не поиск методов и "
                "свойств платформы: для них используйте `search_syntax`. "
                "Возвращает короткие карточки, причину совпадения и известные "
                "версионные факты даже без целевой версии; полный текст "
                "читается через `get_reference` по точному `id`."
            )
        )
        def search_reference(
            query: Annotated[str, Field(
                description=(
                    "Короткая предметная формулировка или точное имя: например, "
                    "название конструкции, оператора либо задачи. Укажите также "
                    "domain, если раздел вопроса известен. Если нужного нет в "
                    "короткой выдаче, повторите с известным термином языка, а "
                    "не угадывайте `id`."
                )
            )],
            domain: Annotated[str | None, Field(
                description=(
                    "Раздел поиска. Рекомендуется задавать всегда, когда предмет "
                    "известен: `bsl` — язык BSL; `query` — язык запросов; `dcs` — "
                    "выражения СКД; `dcs_ui` — интерфейс СКД; `configurator` — "
                    "Конфигуратор; `development_tools` — инструменты разработки; "
                    "`legacy` — архивные инструменты. Без domain поиск смешивает "
                    "разделы и нужен только для намеренно общего вопроса."
                )
            )] = None,
            kind: Annotated[str | None, Field(
                description=(
                    "Точный технический вид внутри domain. Обычно первый поиск "
                    "делайте без него. Основные значения: `bsl_construct`, "
                    "`bsl_guidance`, `bsl_template`, `query_function`, "
                    "`query_keyword`, `dcs_function`, `dcs_article`, "
                    "`dcs_ui_article`, `config_operator`, `devtool_operator`."
                )
            )] = None,
            platform: Annotated[str | None, Field(
                description=(
                    "Необязательная целевая версия, например `8.3.20`. Она не "
                    "влияет на текстовое совпадение, а переносит заведомо "
                    "недоступное в `unavailable_matches`. Для вопроса по "
                    "конфигурации возьмите версию из `list_configurations`; "
                    "для общего вопроса опустите — известная версия появления "
                    "всё равно вернётся в `availability`."
                )
            )] = None,
            include_explicit: Annotated[bool, Field(
                description=(
                    "Добавить служебные и словарные карточки внутри обычного "
                    "раздела. Полностью специальные разделы `configurator`, "
                    "`dcs_ui` и `development_tools` открываются уже самим "
                    "явным выбором domain."
                )
            )] = False,
            include_hidden: Annotated[bool, Field(
                description=(
                    "Добавить архивные и скрытые материалы. Включать только по "
                    "прямой просьбе об устаревшем поведении; одного domain "
                    "`legacy` недостаточно."
                )
            )] = False,
            limit: Annotated[int, Field(
                ge=1,
                le=50,
                description="Число коротких результатов от 1 до 50; обычно достаточно 5–10.",
            )] = 10,
        ) -> str:
            try:
                result = provider.search(
                    query, domain=domain, kind=kind, platform=platform,
                    include_explicit=include_explicit,
                    include_hidden=include_hidden, limit=limit,
                )
                return json.dumps(result, ensure_ascii=False, indent=2)
            except ReferenceQueryError as error:
                message = str(error)
                return CallToolResult(
                    content=[TextContent(type="text", text=message)],
                    structured_content={"result": message}, is_error=True,
                )

        @server.tool(
            description=(
                "Прочитать точную карточку или её раздел из подключённой "
                "общей справки после `search_reference`. Длинный текст выдаётся "
                "страницами; повторяйте вызов с `next_cursor`, пока он не станет "
                "`null`. Известные версионные факты возвращаются и без "
                "целевой платформы."
            )
        )
        def get_reference(
            item_id: Annotated[str, Field(
                description="Точный `id`, полученный из `search_reference`."
            )],
            section_id: Annotated[str | None, Field(
                description="Необязательный точный идентификатор найденного раздела."
            )] = None,
            cursor: Annotated[str | None, Field(
                description="Непрозрачный `next_cursor` предыдущей страницы."
            )] = None,
            max_chars: Annotated[int, Field(ge=MIN_PAGE_CHARS, le=MAX_PAGE_CHARS)] = 8000,
            platform: Annotated[str | None, Field(
                description=(
                    "Та же целевая версия, что в `search_reference`; нужна для "
                    "проверки совместимости карточки, а не для выбора текста. "
                    "Без неё версия появления всё равно возвращается, если известна."
                )
            )] = None,
        ) -> str:
            try:
                result = provider.get(
                    item_id, section_id=section_id, cursor=cursor,
                    max_chars=max_chars, platform=platform,
                )
                return json.dumps(result, ensure_ascii=False, indent=2)
            except ReferenceQueryError as error:
                message = str(error)
                return CallToolResult(
                    content=[TextContent(type="text", text=message)],
                    structured_content={"result": message}, is_error=True,
                )

    _add_http_routes(server, registry, reference, restart)
    return server


def _add_http_routes(
    server: MCPServer,
    registry: Registry,
    reference: ReferenceService,
    restart: RestartController,
) -> None:
    """Служебные HTTP-маршруты рядом с MCP: проверка живости и перезагрузка.

    Перезагрузка нужна, потому что источники добавляются отдельным процессом
    (`mcp1c.cli reg-add` внутри контейнера), а работающий сервер держит реестр
    в памяти. Без неё пришлось бы перезапускать контейнер после каждой новой
    выгрузки.

    Маршрут доступен только если задан `ADMIN_TOKEN`: `custom_route` идёт в
    обход авторизации протокола, и открытая ручка, меняющая данные, на которых
    агент пишет код, — плохая идея по умолчанию.
    """

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        # Открыт всегда: по нему ходит healthcheck контейнера. Но имена
        # конфигураций — уже сведения о клиенте, кто у него внедрён и как
        # называются его доработки. Без токена отдаём только живость и
        # счётчики; с токеном — прежний ответ целиком.
        return JSONResponse(
            {
                **tools.health(registry, detailed=can_read(request)),
                "runtime_id": restart.runtime_id,
            }
        )

    @server.custom_route("/admin/reload", methods=["POST"])
    async def reload(request: Request) -> JSONResponse:
        token = os.environ.get("ADMIN_TOKEN", "")
        if not token:
            return JSONResponse(
                {"error": "Перезагрузка выключена: не задан ADMIN_TOKEN."},
                status_code=404,
            )
        if not same_token(request.headers.get("x-admin-token", ""), token):
            return JSONResponse({"error": "Неверный токен."}, status_code=403)

        # Восстановление обычных источников и подъём валидного кэша остаются
        # синхронными операциями. Уводим их с event loop: иначе ручной reload
        # на это время останавливает и `/health`, и MCP-запросы. Холодная
        # сборка индексов модулей внутри `startup()` уже запускается фоном.
        messages = await run_in_threadpool(registry.startup)
        await server.refresh_role_tools()
        # Состав источников называется тем же способом, что в `/health`:
        # иначе два ответа про одно и то же расходятся, и первым же
        # расхождением стала пустая строка вместо «справки платформы нет».
        return JSONResponse(
            {
                **tools.health(registry, detailed=True),
                "messages": messages,
                "dictionary": registry.dictionary.stats(),
            }
        )

    # Дашборд монтируется тем же способом: `custom_route` — декоратор с
    # сигнатурой (path, methods, name), регистрирует по одному маршруту.
    # Имя передаётся явно, потому что два маршрута `/queries` (GET и POST) —
    # разные функции, а без имени Starlette выведет его из пути и получит
    # конфликт.
    for route in dashboard_routes(
        registry,
        reference=reference,
        restart=restart,
        role_tools_refresh=server.refresh_role_tools,
    ):
        server.custom_route(
            route.path,
            methods=sorted(route.methods or {"GET"}),
            name=route.name or route.endpoint.__name__,
        )(route.endpoint)


def mcp_guard(app):
    """Закрывает `/mcp` токеном чтения, пока тот задан.

    Инструменты MCP отдают структуру конфигураций целиком, поэтому охраняются
    так же, как страницы дашборда, — и тем же токеном: клиенту иначе пришлось
    бы держать два. Слой ставится вокруг всего приложения, а не в маршрутах,
    потому что маршрут `/mcp` заводит SDK и до его обработчика не дотянуться.

    Мимо охраны пропускаются `/health`, `/login` и статика SPA. Health нужен
    контейнеру, а форма входа вместе с CSS/JS должна загрузиться до появления
    сессии. В статике нет предметных данных: они доступны только через API,
    который остаётся закрытым. Это проверяется живым входом, потому что тесты
    маршрутов дашборда не видят внешний слой.
    """
    OPEN_EXACT = ("/health", "/login")
    OPEN_PREFIX = ("/assets/",)

    async def wrapped(scope, receive, send):
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or path in OPEN_EXACT
            or path.startswith(OPEN_PREFIX)
        ):
            await app(scope, receive, send)
            return
        request = Request(scope)
        if can_read(request):
            await app(scope, receive, send)
            return

        # Отказ выглядит по-разному для клиента и для человека. Браузеру JSON
        # в адресной строке — тупик: войти из него неоткуда, а форма входа
        # рядом. Различаем по `Accept`, как это делает сам протокол MCP.
        if "text/html" in request.headers.get("accept", ""):
            target = request.url.path
            if request.url.query:
                target += "?" + request.url.query
            response = RedirectResponse(
                "/login?" + urlencode({"next": target}), status_code=303
            )
        else:
            response = JSONResponse(
                {
                    "error": "Нужен токен. Передайте его заголовком X-Api-Token "
                    "или Authorization: Bearer. Для браузера — /login."
                },
                status_code=401,
            )
        await response(scope, receive, send)

    return http_body_guard(wrapped)


def _run_streamable_http(
    server: MCPServer,
    *,
    host: str,
    port: int,
    trust_proxy_headers: bool,
) -> None:
    """То же, что `server.run('streamable-http')`, но с охраной вокруг.

    SDK умеет запускать себя сам, но тогда между сетью и приложением ничего не
    вставить. Здесь приложение берётся готовым и оборачивается.
    """
    import uvicorn

    app = server.streamable_http_app(host=host)
    uvicorn.run(
        mcp_guard(app),
        host=host,
        port=port,
        log_level="info",
        # X-Forwarded-Proto влияет на флаг Secure сессионной cookie. По
        # умолчанию не доверяем заголовку от клиента. Значение `*` допустимо
        # только за изолированным proxy, явно включённым оператором.
        proxy_headers=trust_proxy_headers,
        forwarded_allow_ips="*" if trust_proxy_headers else None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp1c-server", description=__doc__)
    parser.add_argument("--data", default="data", help="каталог данных сервера")
    parser.add_argument(
        "--transport",
        default="streamable-http",
        choices=("streamable-http", "stdio"),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--trust-proxy-headers",
        action="store_true",
        help=(
            "доверять X-Forwarded-*; только за изолированным reverse proxy, "
            "который перезаписывает эти заголовки"
        ),
    )
    parser.add_argument(
        "--require-writable-data",
        action="store_true",
        help=(
            "перед стартом создать рабочие подкаталоги и доказать запись в них; "
            "используется Docker-образом"
        ),
    )
    parser.add_argument(
        "--require-tokens",
        action="store_true",
        help=(
            "до старта потребовать два разных безопасных API_TOKEN и "
            "ADMIN_TOKEN; используется официальным Docker-образом"
        ),
    )
    args = parser.parse_args(argv)

    try:
        access = access_mode()
        if args.require_tokens:
            require_tokens()
    except (AccessModeError, TokenConfigurationError) as error:
        parser.error(str(error))

    if args.require_writable_data:
        try:
            require_writable_data(args.data)
        except DataDirectoryError as error:
            parser.error(str(error))

    registry = Registry(args.data)
    for message in registry.startup():
        print(f"  {message}", file=sys.stderr)

    if not registry.snapshot().configurations:
        print(
            "Внимание: не загружено ни одной конфигурации. "
            "Положите выгрузку в data/bootstrap/ или добавьте через reg-add.",
            file=sys.stderr,
        )

    server = build_server(
        registry,
        restart=RestartController.from_environment(),
    )
    if args.transport == "stdio":
        # По stdio сервер разговаривает с одним клиентом, который его и
        # запустил: токен там не с кем проверять и не от кого защищаться.
        server.run(transport="stdio")
    else:
        _run_streamable_http(
            server,
            host=args.host,
            port=args.port,
            trust_proxy_headers=(
                args.trust_proxy_headers or access == ACCESS_HTTPS_PROXY
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
