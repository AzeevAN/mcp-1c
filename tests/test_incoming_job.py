"""Старая code-only ручка не может публиковать часть Source B."""

import zipfile
from pathlib import Path

from conftest import build_configuration, живой_клиент, modules_configuration_xml, write_export
from starlette.applications import Starlette

from mcp1c import dashboard_backend as dashboard
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.registry import Registry


def _стенд(tmp_path):
    incoming = tmp_path / "in"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(incoming, build_configuration(name="Розница")))
    registry.incoming_dir.mkdir(parents=True, exist_ok=True)
    archive = registry.incoming_dir / "модули.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("Configuration.xml", modules_configuration_xml())
        package.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "Процедура А() КонецПроцедуры")
    client = живой_клиент(Starlette(routes=routes(registry, mode=DASHBOARD_ON)))
    return client, registry, archive


def test_старая_ручка_требует_админский_токен(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _, _ = _стенд(tmp_path)
    response = client.post("/api/v1/sources/incoming/parse", json={"name": "модули.zip"})
    assert response.status_code == 403


def test_без_admin_token_маршрута_нет(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _, _ = _стенд(tmp_path)
    response = client.post("/api/v1/sources/incoming/parse", json={"name": "модули.zip"})
    assert response.status_code == 404


def test_старая_ручка_отключена_без_разбора_и_записи(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setattr(dashboard, "_JOBS", [])
    client, registry, archive = _стенд(tmp_path)
    before = archive.read_bytes()
    client.post("/login", data={"token": "секрет"})

    response = client.post(
        "/api/v1/sources/incoming/parse",
        json={"name": archive.name, "configuration": "Розница"},
    )

    assert response.status_code == 410
    assert "update_full" in response.json()["error"]
    assert "Розница:modules" not in registry.sources
    assert dashboard._JOBS == []
    assert archive.read_bytes() == before
