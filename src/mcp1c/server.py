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
import os
import sys
from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

from . import tools
from .dashboard import MAX_UPLOAD, can_read
from .dashboard import routes as dashboard_routes
from .registry import Registry

# Лимит файла и лимит HTTP-тела различаются: multipart добавляет служебные
# заголовки и разделители. Остальные значения соответствуют цене операций:
# форма входа крошечная, пакет запросов вмещает 32 максимально длинные фразы,
# MCP и прочие маршруты получают безопасный общий запас.
HTTP_BODY_LIMIT_LOGIN = 16 * 1024
HTTP_BODY_LIMIT_QUERIES = 1024 * 1024
HTTP_BODY_LIMIT_UPLOAD = MAX_UPLOAD + 1024 * 1024
HTTP_BODY_LIMIT_DEFAULT = 2 * 1024 * 1024


def _http_body_limit(scope) -> int:
    if scope.get("method") == "POST":
        path = scope.get("path", "")
        if path == "/login":
            return HTTP_BODY_LIMIT_LOGIN
        if path == "/queries":
            return HTTP_BODY_LIMIT_QUERIES
        if path == "/sources":
            return HTTP_BODY_LIMIT_UPLOAD
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

QUERY_VERSION_CONTRACT = (
    "В самом `shquery_ru.hbk` версии появления не записаны; известные "
    "границы заданы курируемой таблицей и при заданном `config` "
    "фильтруются по версии платформы конфигурации."
)


INSTRUCTIONS = f"""
Справочник по структуре конфигураций 1С, синтаксису платформы и языку
запросов.

Порядок работы:
1. `list_configurations` — узнать, какие конфигурации загружены и что по ним
   доступно. Если загружено больше одной, параметр `config` обязателен во
   всех остальных вызовах.
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
8. `search_syntax` / `get_syntax` — методы и свойства платформы, а также язык
   запросов: `ВЫБРАТЬ`, `ЛЕВОЕ СОЕДИНЕНИЕ`, `ИТОГИ ПО`, `ВЫРАЗИТЬ`, функции
   вроде `КОНЕЦПЕРИОДА`. Выдача отфильтрована по версии конфигурации: то, чего
   в ней нет, не покажется. {QUERY_VERSION_CONTRACT} Обращайте внимание на поле
   «Доступность» — вызов серверного метода из клиентского контекста не
   скомпилируется.

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

Сервер не читает данные информационной базы. Код доступен только после загрузки
выгрузки конфигурации в файлы; `search_procedures` ищет по её процедурам, но не
по значениям документов, справочников и регистров. `get_procedure` читает
тело только по точному адресу и ограничивает его окном до 200 строк.
""".strip()


def build_server(registry: Registry, name: str = "mcp1c") -> MCPServer:
    server = MCPServer(
        name=name,
        title="Структура конфигураций 1С",
        instructions=INSTRUCTIONS,
        version="0.7.0",
    )

    @server.tool(
        description=(
            "Какие конфигурации 1С загружены, на какой платформе и что по ним "
            "доступно. Вызывать первым: если конфигураций больше одной, "
            "параметр `config` обязателен во всех остальных инструментах, а "
            "имя для него берётся отсюда."
        )
    )
    def list_configurations() -> str:
        return tools.list_configurations(registry)

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
            "Найти метод, свойство или объект платформы 1С, а также элемент "
            "языка запросов (функцию, ключевое слово, статью). "
            f"{QUERY_VERSION_CONTRACT} "
            "Даёт только строку списка и первые 400 символов описания. "
            "Сигнатура, параметры, доступность по контекстам, версия появления "
            "и рецепт замены для старой платформы — в `get_syntax` по "
            "найденному имени."
        )
    )
    def search_syntax(
        query: Annotated[str, Field(
            description="Что ищем: «разделить строку», «ЗаписьJSON», «StrFind» "
                        "по платформе; «левое соединение», «итоги по иерархии» "
                        "по языку запросов. Русские и английские имена "
                        "равнозначны.")],
        config: CONFIG_PARAM = None,
        kind: Annotated[str | None, Field(
            description="Ограничить вид: method, property, event, object, "
                        "query_table, query_field, query_function, "
                        "query_keyword, query_article.")] = None,
        limit: LIMIT_PARAM = 10,
    ) -> str:
        return tools.search_syntax(registry, query, config, kind, limit)

    @server.tool(
        description=(
            "Полное описание элемента платформы 1С (сигнатура, параметры, тип "
            "возврата, доступность, версия появления, пример) или элемента "
            "языка запросов (функции, ключевого слова, статьи). "
            "Вызывать перед использованием функции на старой конфигурации: "
            "недоступное в её версии помечается, и там же лежит рецепт "
            "замены, если он записан (`СтрРазделить` появилась в 8.3.6 — на "
            "8.3.5 нужен обход). Поле «Доступность» — тоже ошибка компиляции: "
            "серверный метод из клиентского контекста не соберётся."
        )
    )
    def get_syntax(
        name: Annotated[str, Field(
            description="Имя элемента платформы: `СтрНайти`, "
                        "`ЗаписьJSON.ЗаписатьНачалоОбъекта`, `ValueTable.Columns`. "
                        "Для членов объектов надёжнее указывать `Объект.Член`.")],
        config: CONFIG_PARAM = None,
        detail: DETAIL_PARAM = "fields",
    ) -> str:
        return tools.get_syntax(registry, name, config, detail)

    _add_http_routes(server, registry)
    return server


def _add_http_routes(server: MCPServer, registry: Registry) -> None:
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
        return JSONResponse(tools.health(registry, detailed=can_read(request)))

    @server.custom_route("/admin/reload", methods=["POST"])
    async def reload(request: Request) -> JSONResponse:
        token = os.environ.get("ADMIN_TOKEN", "")
        if not token:
            return JSONResponse(
                {"error": "Перезагрузка выключена: не задан ADMIN_TOKEN."},
                status_code=404,
            )
        if request.headers.get("x-admin-token") != token:
            return JSONResponse({"error": "Неверный токен."}, status_code=403)

        # Восстановление обычных источников и подъём валидного кэша остаются
        # синхронными операциями. Уводим их с event loop: иначе ручной reload
        # на это время останавливает и `/health`, и MCP-запросы. Холодная
        # сборка индексов модулей внутри `startup()` уже запускается фоном.
        messages = await run_in_threadpool(registry.startup)
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
    for route in dashboard_routes(registry):
        server.custom_route(
            route.path,
            methods=sorted(route.methods or {"GET"}),
            name=route.endpoint.__name__,
        )(route.endpoint)


def mcp_guard(app):
    """Закрывает `/mcp` токеном чтения, пока тот задан.

    Инструменты MCP отдают структуру конфигураций целиком, поэтому охраняются
    так же, как страницы дашборда, — и тем же токеном: клиенту иначе пришлось
    бы держать два. Слой ставится вокруг всего приложения, а не в маршрутах,
    потому что маршрут `/mcp` заводит SDK и до его обработчика не дотянуться.

    Мимо охраны пропускаются два пути. `/health` — по нему ходит healthcheck
    контейнера, а сведения он и без того отдаёт по праву чтения. `/login` —
    иначе войти из браузера неоткуда: форма входа оказалась бы за той самой
    авторизацией, которую она выдаёт. Это поймала живая проверка, а не тесты:
    они вешали маршруты дашборда напрямую, без внешнего слоя.
    """
    OPEN = ("/health", "/login")

    async def wrapped(scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith(OPEN):
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
            response = RedirectResponse("/login", status_code=303)
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
    args = parser.parse_args(argv)

    registry = Registry(args.data)
    for message in registry.startup():
        print(f"  {message}", file=sys.stderr)

    if not registry.configurations:
        print(
            "Внимание: не загружено ни одной конфигурации. "
            "Положите выгрузку в data/bootstrap/ или добавьте через reg-add.",
            file=sys.stderr,
        )

    server = build_server(registry)
    if args.transport == "stdio":
        # По stdio сервер разговаривает с одним клиентом, который его и
        # запустил: токен там не с кем проверять и не от кого защищаться.
        server.run(transport="stdio")
    else:
        _run_streamable_http(
            server,
            host=args.host,
            port=args.port,
            trust_proxy_headers=args.trust_proxy_headers,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
