"""Fail-closed runtime-настройки публичного HTTP-сервера."""

from __future__ import annotations

import os
import string
from collections.abc import Mapping


DASHBOARD_ON = "on"
DASHBOARD_OFF = "off"
DASHBOARD_MODES = (DASHBOARD_ON, DASHBOARD_OFF)

ACCESS_LOCAL = "local"
ACCESS_HTTPS_PROXY = "https-proxy"
ACCESS_MODES = (ACCESS_LOCAL, ACCESS_HTTPS_PROXY)

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
    """Выбрать прямой loopback либо доверенный внешний HTTPS proxy."""
    source = os.environ if environ is None else environ
    mode = source.get("MCP1C_ACCESS", ACCESS_LOCAL).strip().lower()
    if mode not in ACCESS_MODES:
        raise AccessModeError(
            "MCP1C_ACCESS должен быть одним из: local, https-proxy. "
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


def require_tokens(environ: Mapping[str, str] | None = None) -> None:
    """Проверить два независимых секрета, не возвращая и не печатая их."""
    source = os.environ if environ is None else environ
    api_token = source.get("API_TOKEN", "")
    admin_token = source.get("ADMIN_TOKEN", "")
    _validate_token("API_TOKEN", api_token)
    _validate_token("ADMIN_TOKEN", admin_token)
    if api_token == admin_token:
        raise TokenConfigurationError(
            "API_TOKEN и ADMIN_TOKEN должны различаться."
        )
