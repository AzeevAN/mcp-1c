"""Разбор по кнопке: право, единственность, отказ по месту."""
import zipfile
from pathlib import Path

from conftest import build_configuration, write_export, живой_клиент
from starlette.applications import Starlette

from mcp1c import dashboard
from mcp1c.registry import Registry


def _стенд(tmp_path):
    данные = tmp_path / "data"
    входящее = tmp_path / "in"
    данные.mkdir()
    входящее.mkdir()
    registry = Registry(данные)
    registry.add_configuration(write_export(входящее, build_configuration(name="Розница")))
    registry.incoming_dir.mkdir(parents=True, exist_ok=True)
    архив = registry.incoming_dir / "модули.zip"
    with zipfile.ZipFile(архив, "w") as zf:
        zf.writestr("Configuration.xml", "<x/>")
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "Процедура А() КонецПроцедуры")
    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))
    return client, registry


def test_разбор_требует_админского_токена(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _ = _стенд(tmp_path)

    ответ = client.post(
        "/sources/incoming/parse", data={"name": "модули.zip"}, follow_redirects=False
    )

    assert ответ.status_code == 403


def test_без_admin_token_маршрута_нет(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _ = _стенд(tmp_path)

    ответ = client.post("/sources/incoming/parse", data={"name": "модули.zip"})

    assert ответ.status_code == 404


def test_разбор_заводит_источник_и_не_трогает_исходник(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})

    ответ = client.post(
        "/sources/incoming/parse", data={"name": "модули.zip"}, follow_redirects=False
    )

    assert ответ.status_code == 303
    дождаться(client, lambda t: "Розница:modules" in t or "разобрано" in t)
    assert (registry.incoming_dir / "модули.zip").is_file()
    assert "Розница:modules" in registry.sources


def test_имя_с_выходом_наружу_отвергается(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _ = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})

    ответ = client.post(
        "/sources/incoming/parse",
        data={"name": "../../etc/passwd"},
        follow_redirects=False,
    )

    assert ответ.status_code == 303


def дождаться(client, условие, таймаут: float = 20.0) -> str:
    import time

    предел = time.monotonic() + таймаут
    текст = ""
    while time.monotonic() < предел:
        текст = client.get("/sources").text
        if условие(текст):
            return текст
        time.sleep(0.05)
    raise AssertionError(f"за {таймаут} с условие не выполнилось:\n{текст}")
