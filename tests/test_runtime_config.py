"""Публичные runtime-переменные отказывают небезопасным значениям до старта."""

from __future__ import annotations

import pytest

from mcp1c.runtime_config import token_is_strong

from mcp1c.runtime_config import (
    ACCESS_HTTP,
    ACCESS_HTTPS_PROXY,
    ACCESS_LOCAL,
    DASHBOARD_OFF,
    DASHBOARD_ON,
    AccessModeError,
    DashboardModeError,
    TokenConfigurationError,
    access_mode,
    allowed_hosts,
    host_check_required,
    dashboard_mode,
    require_tokens,
)


# Только синтетические значения, совпадающие по классу с fixtures token_throttle.
_L1_API = "Qm7vR2xK9pLt4WcZ8hNbE3sYgU6dJfA1oTq5"
_L1_ADMIN = "Zx4Pk8Lw2Ns6Hd9Vb3Tm7Rq1Gy5Cf0Ja8Ue2"


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


@pytest.mark.parametrize("mode", [ACCESS_LOCAL, ACCESS_HTTP, ACCESS_HTTPS_PROXY])
def test_access_принимает_три_явные_топологии(mode):
    assert access_mode({"MCP1C_ACCESS": mode}) == mode


@pytest.mark.parametrize("mode", ["", "remote", "on", "proxy"])
def test_access_отклоняет_неявные_и_неизвестные_значения(mode):
    with pytest.raises(AccessModeError, match="local, http, https-proxy"):
        access_mode({"MCP1C_ACCESS": mode})


def test_обязательные_токены_принимаются_только_разными_и_длинными():
    require_tokens(
        {
            "API_TOKEN": _L1_API,
            "ADMIN_TOKEN": _L1_ADMIN,
        }
    )


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({}, "API_TOKEN обязателен"),
        ({"API_TOKEN": _L1_API}, "ADMIN_TOKEN обязателен"),
        (
            {"API_TOKEN": "short", "ADMIN_TOKEN": "b" * 32},
            "не менее 32",
        ),
        (
            {"API_TOKEN": "а" * 32, "ADMIN_TOKEN": "b" * 32},
            "печатных ASCII",
        ),
        (
            {"API_TOKEN": _L1_API, "ADMIN_TOKEN": _L1_API},
            "должны различаться",
        ),
    ],
)
def test_обязательные_токены_fail_closed(environment, message):
    with pytest.raises(TokenConfigurationError, match=message):
        require_tokens(environment)






def _l1_weak(kind):
    return "abcdefgh" * 4 if kind == "periodic" else "a" * 32


def _l1_no_tokens_in(text, values):
    if any(value and value in text for value in values):
        pytest.fail("Значение токена попало в диагностику", pytrace=False)


@pytest.mark.parametrize("token_name", ["API_TOKEN", "ADMIN_TOKEN"])
@pytest.mark.parametrize("weak_kind", ["periodic", "low-distinct"])
def test_require_tokens_отвергает_предсказуемый_токен_любой_роли(
    token_name, weak_kind, capsys
):
    environment = {"API_TOKEN": _L1_API, "ADMIN_TOKEN": _L1_ADMIN}
    environment[token_name] = _l1_weak(weak_kind)
    assert not token_is_strong(environment[token_name])

    with pytest.raises(TokenConfigurationError) as caught:
        require_tokens(environment)

    assert token_name in str(caught.value)
    captured = capsys.readouterr()
    _l1_no_tokens_in(str(caught.value) + captured.out + captured.err, environment.values())


def test_require_tokens_принимает_разные_стойкие_токены_без_рекурсии():
    assert token_is_strong(_L1_API)
    assert token_is_strong(_L1_ADMIN)

    assert require_tokens({"API_TOKEN": _L1_API, "ADMIN_TOKEN": _L1_ADMIN}) is None




@pytest.mark.parametrize(
    ('mode', 'bind', 'extra', 'required'),
    [
        ('local', '0.0.0.0', {}, True),
        ('local', '::', {}, True),
        ('local', '192.0.2.10', {}, True),
        ('https-proxy', '0.0.0.0', {}, False),
        ('https-proxy', '192.0.2.10', {}, False),
        ('https-proxy', '0.0.0.0', {'MCP1C_PUBLISHED_ADDRESS': '192.0.2.10'}, False),
        ('https-proxy', '0.0.0.0', {'MCP1C_PUBLIC_BASE_URL': 'https://mcp.example'}, True),
        ('https-proxy', '0.0.0.0', {'MCP1C_ALLOWED_HOSTS': 'mcp.lan'}, True),
        ('http', '0.0.0.0', {}, False),
        ('http', '::', {'MCP1C_PUBLISHED_ADDRESS': '::'}, False),
        ('http', '192.0.2.10', {}, True),
        ('http', '192.0.2.10', {'MCP1C_PUBLISHED_ADDRESS': '0.0.0.0'}, False),
        ('http', '0.0.0.0', {'MCP1C_PUBLIC_BASE_URL': 'http://mcp.example'}, True),
        ('http', '0.0.0.0', {'MCP1C_ALLOWED_HOSTS': 'mcp.lan'}, True),
    ],
)
def test_i2_host_guard_следует_топологии(mode, bind, extra, required):
    environment = {'API_TOKEN': _L1_API, 'MCP1C_ACCESS': mode, **extra}
    assert host_check_required(bind, environment) is required


@pytest.mark.parametrize('mode', ['local', 'http', 'https-proxy'])
@pytest.mark.parametrize('bare', ['no-api', 'loopback'])
def test_i2_bare_override_сохраняет_защиту(mode, bare):
    environment = {'MCP1C_ACCESS': mode}
    bind = '0.0.0.0'
    if bare == 'loopback':
        environment['API_TOKEN'] = _L1_API
        bind = '127.0.0.1'
    assert host_check_required(bind, environment) is True
