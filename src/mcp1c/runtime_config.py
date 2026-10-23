"""Fail-closed runtime-настройки публичного HTTP-сервера."""

from __future__ import annotations

import ipaddress
import os
import string
from collections.abc import Mapping
from urllib.parse import urlsplit


DASHBOARD_ON = "on"
DASHBOARD_OFF = "off"
DASHBOARD_MODES = (DASHBOARD_ON, DASHBOARD_OFF)

ACCESS_LOCAL = "local"
ACCESS_HTTP = "http"
ACCESS_HTTPS_PROXY = "https-proxy"
ACCESS_MODES = (ACCESS_LOCAL, ACCESS_HTTP, ACCESS_HTTPS_PROXY)

MIN_TOKEN_CHARS = 32
_TOKEN_CHARS = frozenset(string.ascii_letters + string.digits + string.punctuation)
_PLACEHOLDER_TOKENS = frozenset(
    {
        "123",
        "api-token",
        "admin-token",
        "change-me",
        "changeme",
        "replace-me",
    }
)


class DashboardModeError(ValueError):
    """В окружении указан неподдерживаемый режим дашборда."""


class AccessModeError(ValueError):
    """В окружении указана неподдерживаемая топология HTTP-доступа."""


class TokenConfigurationError(ValueError):
    """Официальный HTTP-запуск получил небезопасный набор токенов."""


class HostConfigurationError(ValueError):
    """Список допустимых хостов или доверенных proxy записан неверно."""


# Имена, под которыми к процессу обращаются с той же машины. Порт не
# фиксируется: Compose публикует 8000 контейнера как 5001 хоста, а MCP
# Inspector и подобные инструменты приходят со своего loopback-порта.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def dashboard_mode(environ: Mapping[str, str] | None = None) -> str:
    """Выбрать современный UI либо полностью отключить dashboard routes."""
    source = os.environ if environ is None else environ
    mode = source.get("MCP1C_DASHBOARD", DASHBOARD_ON).strip().lower()
    if mode not in DASHBOARD_MODES:
        legacy = (
            " Значение `classic` удалено; используйте `on`."
            if mode == "classic"
            else " Значение `spa` переименовано в `on`."
            if mode == "spa"
            else ""
        )
        raise DashboardModeError(
            "MCP1C_DASHBOARD должен быть одним из: on, off. "
            f"Получено: {mode or '<пусто>'}.{legacy}"
        )
    return mode


def access_mode(environ: Mapping[str, str] | None = None) -> str:
    """Выбрать loopback, прямой HTTP либо доверенный внешний HTTPS proxy."""
    source = os.environ if environ is None else environ
    mode = source.get("MCP1C_ACCESS", ACCESS_LOCAL).strip().lower()
    if mode not in ACCESS_MODES:
        raise AccessModeError(
            "MCP1C_ACCESS должен быть одним из: local, http, https-proxy. "
            f"Получено: {mode or '<пусто>'}."
        )
    return mode


def _validate_token(name: str, value: str) -> None:
    if not value:
        raise TokenConfigurationError(f"{name} обязателен для Docker-запуска.")
    if len(value) < MIN_TOKEN_CHARS:
        raise TokenConfigurationError(
            f"{name} должен содержать не менее {MIN_TOKEN_CHARS} символов."
        )
    if any(char not in _TOKEN_CHARS for char in value):
        raise TokenConfigurationError(
            f"{name} должен состоять из печатных ASCII-символов без пробелов."
        )
    if value.casefold() in _PLACEHOLDER_TOKENS:
        raise TokenConfigurationError(
            f"{name} содержит известное тестовое значение; сгенерируйте секрет."
        )


def token_is_strong(value: str) -> bool:
    """Выдерживает ли токен онлайн-перебор без блокировки.

    Формат обязательных секретов (32+ печатных ASCII-символа, не известная
    заглушка) и признаки случайности: не меньше 8 различных символов, без
    слова-заглушки внутри и без повторяющегося периода. Такой токен ответ
    «совпало» не раскрывает — догадаться до него за время жизни процесса
    нельзя, — поэтому его сверяют и под блокировкой перебора.
    """
    try:
        _validate_token("token", value)
    except TokenConfigurationError:
        return False
    return not _looks_predictable(value)


# Длина сама по себе не делает токен случайным: `change-me-change-me-…` или
# `aaaa…` длиннее 32 символов, но угадываются. Порог различных символов
# учитывает алфавит hex: в `openssl rand -hex 32` из README их не больше 16,
# и в замере на миллионе `secrets.token_hex(16)` ни один не оказался ниже 8;
# `token_urlsafe(32)` из `.env.example` даёт больше 20.
_MIN_DISTINCT_CHARS = 8


def _looks_predictable(value: str) -> bool:
    if len(set(value)) < _MIN_DISTINCT_CHARS:
        return True
    folded = value.casefold()
    if any(marker in folded for marker in _PLACEHOLDER_TOKENS if len(marker) >= 6):
        return True
    # Минимальный период строки — len минус длина наибольшей собственной
    # грани (префикс-функция): линейное время и любой период, в том числе не
    # делящий длину (`0123…f0123…f0123`).
    border = [0] * len(value)
    for i in range(1, len(value)):
        k = border[i - 1]
        while k and value[i] != value[k]:
            k = border[k - 1]
        if value[i] == value[k]:
            k += 1
        border[i] = k
    return len(value) - border[-1] <= len(value) // 2


def require_tokens(environ: Mapping[str, str] | None = None) -> None:
    """Проверить два независимых секрета, не возвращая и не печатая их."""
    source = os.environ if environ is None else environ
    api_token = source.get("API_TOKEN", "")
    admin_token = source.get("ADMIN_TOKEN", "")
    _validate_token("API_TOKEN", api_token)
    _validate_token("ADMIN_TOKEN", admin_token)
    for name, value in (("API_TOKEN", api_token), ("ADMIN_TOKEN", admin_token)):
        if _looks_predictable(value):
            raise TokenConfigurationError(
                f"{name} предсказуем; сгенерируйте стойкий случайный токен."
            )
    if api_token == admin_token:
        raise TokenConfigurationError(
            "API_TOKEN и ADMIN_TOKEN должны различаться."
        )


def is_loopback_bind(host: str) -> bool:
    """Слушает ли сервер только собственную машину."""
    value = host.strip().removeprefix("[").removesuffix("]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def split_host(value: str) -> tuple[str, int | None] | None:
    """Разобрать `host[:port]` так же, как его пишет клиент в заголовке Host."""
    try:
        parsed = urlsplit("//" + value)
        port = parsed.port
    except ValueError:
        return None
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(char in hostname for char in "*/\\ ")
    ):
        return None
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return hostname, port


def _host_entry(raw: str) -> str:
    split = split_host(raw.strip())
    if split is None:
        raise HostConfigurationError(
            "MCP1C_ALLOWED_HOSTS ждёт имена или адреса через запятую в виде "
            f"`host` либо `host:port`, без схемы и пути. Получено: {raw.strip()!r}."
        )
    hostname, port = split
    return hostname if port is None else f"{hostname}:{port}"


def _published_host(source: Mapping[str, str], bind_host: str) -> str | None:
    # Compose знает внешний bind, тогда как процесс внутри слушает 0.0.0.0.
    # При bare-запуске адрес самого процесса используется как публикация.
    raw = source.get("MCP1C_PUBLISHED_ADDRESS", "").strip() or bind_host.strip()
    if not raw:
        return None
    try:
        address = ipaddress.ip_address(raw.removeprefix("[").removesuffix("]"))
    except ValueError:
        parsed = split_host(raw)
        if parsed is None or parsed[1] is not None:
            raise HostConfigurationError(
                "MCP1C_PUBLISHED_ADDRESS должен быть адресом или именем без порта."
            ) from None
        return parsed[0]
    if address.is_unspecified:
        return None
    return f"[{address}]" if address.version == 6 else str(address)


def allowed_hosts(
    environ: Mapping[str, str] | None = None, *, bind_host: str = "",
) -> tuple[str, ...]:
    """Имена, под которыми браузер вправе обращаться к серверу.

    Без такого списка страница с чужого сайта после DNS rebinding читает API
    дашборда как свой origin. Источники зависят от MCP1C_ACCESS: loopback,
    локальный Docker alias, конкретный адрес публикации, публичный URL
    и явный MCP1C_ALLOWED_HOSTS. Неверный публичный адрес здесь не роняет старт: о нём
    сообщит сам инструмент скачивания, как и раньше.
    """
    source = os.environ if environ is None else environ
    entries = list(LOOPBACK_HOSTS)
    mode = access_mode(source)
    if mode == ACCESS_LOCAL:
        entries.append("host.docker.internal")
    if mode in (ACCESS_LOCAL, ACCESS_HTTP):
        published = _published_host(source, bind_host)
        if published is not None:
            entries.append(published)
    base = source.get("MCP1C_PUBLIC_BASE_URL", "").strip()
    if base:
        try:
            parsed = urlsplit(base)
        except ValueError as error:
            raise HostConfigurationError(
                f"MCP1C_PUBLIC_BASE_URL не разбирается как адрес: {error}."
            ) from error
        split = split_host(parsed.netloc) if parsed.scheme in ("http", "https") else None
        if split is not None:
            hostname, port = split
            entries.append(hostname if port is None else f"{hostname}:{port}")
    for item in source.get("MCP1C_ALLOWED_HOSTS", "").split(","):
        if item.strip():
            entries.append(_host_entry(item))
    return tuple(dict.fromkeys(entries))


def host_check_required(bind_host: str, environ: Mapping[str, str] | None = None) -> bool:
    """Вывести защиту Host/Origin из топологии и явных настроек доступа."""
    source = os.environ if environ is None else environ
    mode = access_mode(source)
    if not source.get("API_TOKEN", "") or is_loopback_bind(bind_host):
        return True
    if mode == ACCESS_LOCAL:
        return True
    if (
        source.get("MCP1C_PUBLIC_BASE_URL", "").strip()
        or source.get("MCP1C_ALLOWED_HOSTS", "").strip()
    ):
        return True
    return mode == ACCESS_HTTP and _published_host(source, bind_host) is not None


def forwarded_allow_ips(
    trust_proxy_headers: bool, environ: Mapping[str, str] | None = None
) -> str | None:
    """Адреса proxy, чьим `X-Forwarded-*` верит Uvicorn.

    Без явного списка остаётся `*`: в Compose запрос от proxy хоста приходит
    в контейнер с адреса шлюза Docker-сети, который заранее неизвестен.
    Тогда защита держится на том, что порт backend доступен только proxy.
    Uvicorn молча превращает нераспознанный адрес в литерал, и опечатка
    отключила бы доверие без следа, — поэтому список проверяется здесь.
    """
    source = os.environ if environ is None else environ
    raw = source.get("MCP1C_TRUSTED_PROXIES", "").strip()
    if not trust_proxy_headers:
        if raw:
            raise HostConfigurationError(
                "MCP1C_TRUSTED_PROXIES действует только при MCP1C_ACCESS=https-proxy "
                "или --trust-proxy-headers."
            )
        return None
    if not raw:
        return "*"
    items = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        try:
            if "/" in value:
                # Строго, как Uvicorn: адрес с битами хоста он не считает
                # сетью, и доверие тогда молча выключилось бы.
                ipaddress.ip_network(value, strict=True)
            else:
                ipaddress.ip_address(value)
        except ValueError as error:
            hint = ""
            if "/" in value:
                try:
                    network = ipaddress.ip_network(value, strict=False)
                    hint = f" Возможно, имелась в виду сеть {network}."
                except ValueError:
                    pass
            raise HostConfigurationError(
                "MCP1C_TRUSTED_PROXIES ждёт IP-адреса или сети CIDR без битов "
                f"хоста через запятую. Получено: {value!r}.{hint}"
            ) from error
        items.append(value)
    if not items:
        raise HostConfigurationError(
            "MCP1C_TRUSTED_PROXIES не содержит ни одного адреса; оставьте "
            "переменную пустой или перечислите IP и сети CIDR."
        )
    return ",".join(items)
