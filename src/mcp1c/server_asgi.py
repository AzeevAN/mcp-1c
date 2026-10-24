"""Защитные ASGI-слои и запуск HTTP-транспорта MCP."""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlencode

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse

from .dashboard_backend import MAX_UPLOAD, can_read, header_token_check
from .intake_v2_transport import MAX_BROWSER_UPLOAD_BYTES
from .module_download import DOWNLOAD_PATH
from .runtime_config import split_host


# Лимит файла и лимит HTTP-тела различаются: multipart добавляет служебные
# заголовки и разделители. Остальные значения соответствуют цене операций:
# форма входа крошечная, пакет запросов вмещает 32 максимально длинные фразы,
# MCP и прочие маршруты получают безопасный общий запас.
HTTP_BODY_LIMIT_LOGIN = 16 * 1024
HTTP_BODY_LIMIT_QUERIES = 1024 * 1024
HTTP_BODY_LIMIT_UPLOAD = MAX_UPLOAD + 1024 * 1024
HTTP_BODY_LIMIT_INTAKE_UPLOAD = MAX_BROWSER_UPLOAD_BYTES + 1024 * 1024
HTTP_BODY_LIMIT_DEFAULT = 2 * 1024 * 1024


def _http_body_limit(scope) -> int:
    if scope.get("method") == "POST":
        path = scope.get("path", "")
        if path == "/login":
            return HTTP_BODY_LIMIT_LOGIN
        if path == "/api/v1/queries":
            return HTTP_BODY_LIMIT_QUERIES
        if path == "/api/v1/sources/intake/upload":
            return HTTP_BODY_LIMIT_INTAKE_UPLOAD
        if path == "/api/v1/sources/upload":
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
            or (path == DOWNLOAD_PATH and scope.get("method") == "GET")
            or path.startswith(OPEN_PREFIX)
        ):
            await app(scope, receive, send)
            return
        request = Request(scope)
        if can_read(request):
            await app(scope, receive, send)
            return
        retry_after = header_token_check(request)[1]
        if retry_after is not None:
            response = JSONResponse(
                {"error": "Слишком много неудачных попыток. Повторите позже."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
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


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Защитные заголовки для всех ответов: SPA и API отдаёт один процесс, и
# встроить дашборд в чужой фрейм или угадать тип JSON-ответа как HTML
# не должно быть возможно ни на одном маршруте. `same-origin` сохраняет
# Referer для собственной CSRF-проверки и не отдаёт адреса страниц наружу.
_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"same-origin"),
)


def security_headers(app):
    """Добавить защитные заголовки, не перекрывая уже выставленные маршрутом."""

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                present = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value)
                    for name, value in _SECURITY_HEADERS
                    if name not in present
                )
                message = {**message, "headers": headers}
            await send(message)

        await app(scope, receive, send_with_headers)

    return wrapped


def _host_allowed(value: str, hosts: tuple[str, ...]) -> bool:
    split = split_host(value)
    if split is None:
        return False
    hostname, port = split
    for entry in hosts:
        allowed_name, allowed_port = split_host(entry) or ("", None)
        if allowed_name == hostname and (allowed_port is None or allowed_port == port):
            return True
    return False


def _origin_allowed(origin: str, hosts: tuple[str, ...]) -> bool:
    scheme, separator, rest = origin.partition("://")
    if not separator or scheme.lower() not in ("http", "https") or "/" in rest:
        return False
    return _host_allowed(rest, hosts)


def trusted_host_guard(app, hosts: tuple[str, ...] | None):
    """Отклонить запрос к чужому имени до любого маршрута приложения.

    SDK проверяет Host и Origin только у `/mcp`, а API дашборда, `/health` и
    служебные маршруты добавлены рядом через `custom_route`. Без внешнего слоя
    страница с чужого сайта после DNS rebinding читает их как свой origin, если
    чтение не закрыто токеном. Origin сверяется у изменяющих методов и у
    websocket: у браузерного GET он бывает и у законных переходов, а записи с
    чужого сайта нужны только атакующему. Websocket браузер открывает с любой
    страницы без CORS, поэтому чужой Origin там равен записи. `None` —
    проверка выключена осознанно (контейнер с обязательными токенами, см.
    `host_check_required`).
    """
    if hosts is None:
        return app

    async def wrapped(scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await app(scope, receive, send)
            return
        headers = {}
        for name, value in scope.get("headers", ()):
            headers.setdefault(name.lower(), value.decode("latin-1"))
        websocket = scope["type"] == "websocket"

        async def reject(text: str, status: int) -> None:
            if websocket:
                # Отказ до accept: сервер отвечает на рукопожатие 403. Ответ
                # с телом (`websocket.http.response`) — расширение ASGI,
                # которое поддерживает не каждый сервер.
                await receive()
                await send({"type": "websocket.close", "code": 1008})
                return
            await PlainTextResponse(text, status_code=status)(scope, receive, send)

        if not _host_allowed(headers.get(b"host", ""), hosts):
            await reject(
                "Недопустимый заголовок Host. Добавьте имя, по которому "
                "клиенты обращаются к серверу, в MCP1C_ALLOWED_HOSTS.",
                421,
            )
            return
        origin = headers.get(b"origin")
        if (
            origin is not None
            and (websocket or scope.get("method", "GET") not in _SAFE_METHODS)
            and not _origin_allowed(origin, hosts)
        ):
            await reject(
                "Запрос с чужого origin отклонён. Если это origin самого "
                "сервера, добавьте его имя в MCP1C_ALLOWED_HOSTS.",
                403,
            )
            return
        await app(scope, receive, send)

    return wrapped


def _transport_security(hosts: tuple[str, ...] | None) -> TransportSecuritySettings:
    """Те же правила для SDK, чтобы `/mcp` не жил по своему списку.

    Без явных настроек SDK сам включает защиту только при loopback bind и
    только для loopback-имён, а при `0.0.0.0` выключает её. Явный объект
    убирает это неявное расхождение с внешним слоем.
    """
    if hosts is None:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    host_patterns: list[str] = []
    origin_patterns: list[str] = []
    for entry in hosts:
        hostname, port = split_host(entry) or (entry, None)
        suffixes = [""] if port is not None else ["", ":*"]
        base = hostname if port is None else f"{hostname}:{port}"
        host_patterns.extend(base + suffix for suffix in suffixes)
        origin_patterns.extend(
            f"{scheme}://{base}{suffix}"
            for scheme in ("http", "https")
            for suffix in suffixes
        )
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=host_patterns,
        allowed_origins=origin_patterns,
    )


def run_streamable_http(
    server: MCPServer,
    *,
    host: str,
    port: int,
    forwarded_allow_ips: str | None,
    allowed_hosts: tuple[str, ...] | None,
    mcp_guard: Callable,
) -> None:
    """То же, что `server.run('streamable-http')`, но с охраной вокруг.

    SDK умеет запускать себя сам, но тогда между сетью и приложением ничего не
    вставить. Здесь приложение берётся готовым и оборачивается.
    """
    import uvicorn

    app = server.streamable_http_app(
        host=host, transport_security=_transport_security(allowed_hosts)
    )
    uvicorn.run(
        security_headers(trusted_host_guard(mcp_guard(app), allowed_hosts)),
        host=host,
        port=port,
        log_level="info",
        # X-Forwarded-Proto влияет на флаг Secure сессионной cookie, а
        # X-Forwarded-For — на адрес клиента в журнале и в ограничении
        # попыток входа. По умолчанию заголовкам клиента не верим.
        proxy_headers=forwarded_allow_ips is not None,
        forwarded_allow_ips=forwarded_allow_ips,
    )
