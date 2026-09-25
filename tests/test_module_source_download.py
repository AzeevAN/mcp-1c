"""Передача полного исходника модуля через MCP-билет и HTTP-файл."""

from __future__ import annotations

import gzip
import hashlib
import json
from urllib.parse import urlsplit

import pytest
from starlette.applications import Starlette

from conftest import живой_клиент
from mcp1c.server import build_server, mcp_guard
from module_samples import v8_container_bytes


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def сервер_с_модулями(корень_кода, реестр_из_кода, monkeypatch):
    monkeypatch.setenv("MCP1C_PUBLIC_BASE_URL", "http://testserver")
    monkeypatch.setenv("API_TOKEN", "synthetic-api-token")
    form_path = корень_кода / "Documents" / "Пример" / "Forms" / "Двоичная" / "Ext"
    form_path.mkdir(parents=True)
    form_path.parent.with_suffix(".xml").write_text("<MetaDataObject/>", encoding="utf-8")
    form_path.joinpath("Form.bin").write_bytes(
        v8_container_bytes(
            [("module", "Процедура Открыть()\r\nКонецПроцедуры\r\n".encode("utf-8")),
             ("form", b"{19}")]
        )
    )
    empty_path = корень_кода / "CommonModules" / "Пустой" / "Ext"
    empty_path.mkdir(parents=True)
    empty_path.joinpath("Module.bsl").write_bytes(b"")
    missing_path = корень_кода / "CommonForms" / "БезТела" / "Ext"
    missing_path.mkdir(parents=True)
    missing_path.joinpath("Form.bin").write_bytes(v8_container_bytes([("form", b"{19}")]))
    реестр = реестр_из_кода(корень_кода)
    сервер = build_server(реестр)
    клиент = живой_клиент(Starlette(routes=сервер._custom_starlette_routes))
    return сервер, клиент, реестр


def _json_инструмента(ответ):
    assert not ответ.is_error
    assert len(ответ.content) == 1
    return json.loads(ответ.content[0].text)


async def _ссылка(сервер, адрес):
    return _json_инструмента(
        await сервер.call_tool(
            "get_module_source_file", {"address": адрес, "config": "Пример"}
        )
    )


def _скачать(клиент, метаданные, *, headers=None):
    url = urlsplit(метаданные["download_url"])
    assert (url.scheme, url.netloc, url.path) == (
        "http", "testserver", "/api/v1/modules/source"
    )
    assert "synthetic-api-token" not in метаданные["download_url"]
    assert "synthetic-api-token" not in json.dumps(метаданные, ensure_ascii=False)
    assert "path" not in метаданные
    assert not url.query
    return клиент.get(
        url.path,
        headers=метаданные["headers"] if headers is None else headers,
    )


@pytest.mark.parametrize(
    ("адрес", "ожидаемый_текст"),
    [
        (
            "ОбщийМодуль.ОбщийПример",
            "// Складывает два числа.\n"
            "Функция Сложить(Первый, Второй) Экспорт\n"
            "\tВозврат Первый + Второй;\n"
            "КонецФункции\n\n"
            "Процедура Внутренняя()\n"
            "\tСложить(1, 2);\n"
            "КонецПроцедуры\n",
        ),
        (
            "Документ.Пример.Форма.Двоичная",
            "Процедура Открыть()\nКонецПроцедуры\n",
        ),
    ],
)
async def test_скачанный_gzip_содержит_полный_нормализованный_исходник(
    сервер_с_модулями, адрес, ожидаемый_текст
):
    сервер, клиент, _ = сервер_с_модулями
    ожидаемый = ожидаемый_текст.encode("utf-8")

    метаданные = await _ссылка(сервер, адрес)
    ответ = _скачать(клиент, метаданные)

    assert метаданные["address"] == адрес
    assert метаданные["config"] == "Пример"
    assert метаданные["extension"] is None
    assert метаданные["size_bytes"] == len(ожидаемый)
    assert метаданные["sha256"] == hashlib.sha256(ожидаемый).hexdigest()
    assert метаданные["revision"]
    assert метаданные["state"] == "readable"
    assert метаданные["expires_at"]
    assert set(метаданные["headers"]) == {"X-Mcp1c-Download-Ticket"}
    assert ответ.status_code == 200
    assert ответ.headers["content-type"] == "application/gzip"
    assert gzip.decompress(ответ.content) == ожидаемый
    assert ответ.headers["x-content-sha256"] == метаданные["sha256"]
    assert int(ответ.headers["x-content-size"]) == len(ожидаемый)


async def test_пустой_модуль_скачивается_как_пустой_исходник(сервер_с_модулями):
    сервер, клиент, _ = сервер_с_модулями

    метаданные = await _ссылка(сервер, "ОбщийМодуль.Пустой")
    ответ = _скачать(клиент, метаданные)

    assert метаданные["state"] == "empty"
    assert метаданные["size_bytes"] == 0
    assert метаданные["sha256"] == hashlib.sha256(b"").hexdigest()
    assert ответ.status_code == 200
    assert gzip.decompress(ответ.content) == b""


@pytest.mark.parametrize(
    "адрес",
    ["ОбщаяФорма.БезТела", "ОбщийМодуль.Отсутствует"],
)
async def test_без_читаемого_исходника_билет_не_выдаётся(
    сервер_с_модулями, адрес
):
    сервер, _, _ = сервер_с_модулями

    ответ = await сервер.call_tool(
        "get_module_source_file", {"address": адрес, "config": "Пример"}
    )

    assert "download_url" not in str(ответ)
    assert "X-Mcp1c-Download-Ticket" not in str(ответ)


async def test_скомпилированный_модуль_не_получает_билет(
    tmp_path, реестр_из_кода, monkeypatch
):
    monkeypatch.setenv("MCP1C_PUBLIC_BASE_URL", "http://testserver")
    путь = tmp_path / "CommonModules" / "Закрытый.Module"
    путь.parent.mkdir(parents=True)
    путь.write_bytes(b"compiled")
    сервер = build_server(реестр_из_кода(tmp_path))

    ответ = await сервер.call_tool(
        "get_module_source_file",
        {"address": "ОбщийМодуль.Закрытый", "config": "Пример"},
    )

    assert "download_url" not in str(ответ)
    assert "X-Mcp1c-Download-Ticket" not in str(ответ)


async def test_скачивание_требует_выданный_билет(сервер_с_модулями):
    сервер, клиент, _ = сервер_с_модулями
    метаданные = await _ссылка(сервер, "ОбщийМодуль.ОбщийПример")
    url = urlsplit(метаданные["download_url"])
    ticket = метаданные["headers"]["X-Mcp1c-Download-Ticket"]

    for headers in ({}, {"X-Mcp1c-Download-Ticket": ticket + "x"}):
        ответ = клиент.get(url.path, headers=headers)
        assert ответ.status_code in {401, 403}
        assert b"\x1f\x8b" not in ответ.content
    assert _скачать(клиент, метаданные).status_code == 200


async def test_http_охрана_пропускает_только_get_с_билетом(сервер_с_модулями):
    сервер, _, _ = сервер_с_модулями
    клиент = живой_клиент(
        mcp_guard(Starlette(routes=сервер._custom_starlette_routes))
    )
    метаданные = await _ссылка(сервер, "ОбщийМодуль.ОбщийПример")

    assert клиент.get("/api/v1/modules/source").status_code == 401
    assert клиент.get(
        "/api/v1/modules/source", headers={"X-Api-Token": "synthetic-api-token"}
    ).status_code == 401
    assert _скачать(клиент, метаданные).status_code == 200
    assert клиент.post(
        "/api/v1/modules/source", headers=метаданные["headers"]
    ).status_code == 401


async def test_https_proxy_требует_https_public_origin(
    сервер_с_модулями, monkeypatch
):
    сервер, _, _ = сервер_с_модулями
    monkeypatch.setenv("MCP1C_ACCESS", "https-proxy")

    отказ = await сервер.call_tool(
        "get_module_source_file",
        {"address": "ОбщийМодуль.ОбщийПример", "config": "Пример"},
    )
    assert отказ.is_error
    assert "download_url" not in str(отказ)

    monkeypatch.setenv("MCP1C_PUBLIC_BASE_URL", "https://mcp.example.com")
    ответ = await _ссылка(сервер, "ОбщийМодуль.ОбщийПример")
    assert ответ["download_url"] == "https://mcp.example.com/api/v1/modules/source"


async def test_просроченный_билет_не_отдаёт_файл(сервер_с_модулями, monkeypatch):
    from mcp1c import module_download

    сервер, клиент, _ = сервер_с_модулями
    метаданные = await _ссылка(сервер, "ОбщийМодуль.ОбщийПример")
    выдан_в = module_download.time.time()
    monkeypatch.setattr(module_download.time, "time", lambda: выдан_в + 301)

    ответ = _скачать(клиент, метаданные)

    assert ответ.status_code in {401, 403, 410}
    assert b"\x1f\x8b" not in ответ.content


async def test_билет_однозначно_указывает_на_свой_модуль(сервер_с_модулями):
    сервер, клиент, _ = сервер_с_модулями
    первый = await _ссылка(сервер, "ОбщийМодуль.ОбщийПример")
    второй = await _ссылка(сервер, "Документ.Пример.Форма.Двоичная")
    первый_файл = gzip.decompress(_скачать(клиент, первый).content)
    второй_файл = gzip.decompress(_скачать(клиент, второй).content)

    assert первый["download_url"] == второй["download_url"]
    assert первый["headers"] != второй["headers"]
    assert hashlib.sha256(первый_файл).hexdigest() == первый["sha256"]
    assert hashlib.sha256(второй_файл).hexdigest() == второй["sha256"]
    assert первый_файл != второй_файл


async def test_после_смены_поколения_старый_билет_не_отдаёт_новый_код(
    сервер_с_модулями, корень_кода, архив_кода
):
    сервер, клиент, реестр = сервер_с_модулями
    адрес = "ОбщийМодуль.ОбщийПример"
    старый = await _ссылка(сервер, адрес)
    исходник = корень_кода / "CommonModules" / "ОбщийПример" / "Ext" / "Module.bsl"
    исходник.write_text("Процедура Новая()\nКонецПроцедуры\n", encoding="utf-8")
    реестр.add_modules(архив_кода(корень_кода), configuration="Пример")

    отказ = _скачать(клиент, старый)
    новый = await _ссылка(сервер, адрес)
    ответ = _скачать(клиент, новый)

    assert отказ.status_code in {401, 403, 409, 410}
    assert старый["revision"] != новый["revision"]
    assert ответ.status_code == 200
    assert gzip.decompress(ответ.content) == исходник.read_bytes()


@pytest.mark.parametrize(
    "адрес",
    ["", "../data/secret.bsl", "/tmp/Module.bsl", "ОбщийМодуль.ОбщийПример::Сложить"],
)
async def test_неверный_адрес_не_выдаёт_ссылку(сервер_с_модулями, адрес):
    сервер, _, _ = сервер_с_модулями
    ответ = await сервер.call_tool(
        "get_module_source_file", {"address": адрес, "config": "Пример"}
    )
    assert "download_url" not in str(ответ)
