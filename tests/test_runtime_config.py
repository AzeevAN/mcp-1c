"""Публичные runtime-переменные отказывают небезопасным значениям до старта."""

from __future__ import annotations

import pytest

from mcp1c.runtime_config import (
    ACCESS_HTTPS_PROXY,
    ACCESS_LOCAL,
    DASHBOARD_OFF,
    DASHBOARD_ON,
    AccessModeError,
    DashboardModeError,
    TokenConfigurationError,
    access_mode,
    dashboard_mode,
    require_tokens,
)


def test_dashboard_по_умолчанию_включён():
    assert dashboard_mode({}) == DASHBOARD_ON


@pytest.mark.parametrize("mode", [DASHBOARD_ON, DASHBOARD_OFF])
def test_dashboard_принимает_только_два_режима(mode):
    assert dashboard_mode({"MCP1C_DASHBOARD": mode}) == mode


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("classic", "удалено"),
        ("spa", "переименовано"),
        ("", "<пусто>"),
        ("react", "Получено: react"),
    ],
)
def test_dashboard_отклоняет_старые_пустые_и_неизвестные_значения(mode, message):
    with pytest.raises(DashboardModeError, match=message):
        dashboard_mode({"MCP1C_DASHBOARD": mode})


def test_access_по_умолчанию_остаётся_local():
    assert access_mode({}) == ACCESS_LOCAL


@pytest.mark.parametrize("mode", [ACCESS_LOCAL, ACCESS_HTTPS_PROXY])
def test_access_принимает_две_явные_топологии(mode):
    assert access_mode({"MCP1C_ACCESS": mode}) == mode


@pytest.mark.parametrize("mode", ["", "remote", "on", "proxy"])
def test_access_отклоняет_неявные_и_неизвестные_значения(mode):
    with pytest.raises(AccessModeError, match="local, https-proxy"):
        access_mode({"MCP1C_ACCESS": mode})


def test_обязательные_токены_принимаются_только_разными_и_длинными():
    require_tokens(
        {
            "API_TOKEN": "a" * 32,
            "ADMIN_TOKEN": "b" * 32,
        }
    )


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({}, "API_TOKEN обязателен"),
        ({"API_TOKEN": "a" * 32}, "ADMIN_TOKEN обязателен"),
        (
            {"API_TOKEN": "short", "ADMIN_TOKEN": "b" * 32},
            "не менее 32",
        ),
        (
            {"API_TOKEN": "а" * 32, "ADMIN_TOKEN": "b" * 32},
            "печатных ASCII",
        ),
        (
            {"API_TOKEN": "a" * 32, "ADMIN_TOKEN": "a" * 32},
            "должны различаться",
        ),
    ],
)
def test_обязательные_токены_fail_closed(environment, message):
    with pytest.raises(TokenConfigurationError, match=message):
        require_tokens(environment)
