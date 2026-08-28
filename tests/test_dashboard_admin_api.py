"""Административный API SPA управляет тем же Registry, что и MCP."""

from __future__ import annotations

import time
import zipfile

from starlette.applications import Starlette

from conftest import (
    build_configuration,
    живой_клиент,
    modules_configuration_xml,
    состарить,
    write_export,
)
from mcp1c import dashboard
from mcp1c.dashboard_runtime import DASHBOARD_SPA, routes
from mcp1c.registry import Registry


def _client(registry: Registry):
    return живой_клиент(
        Starlette(routes=routes(registry, mode=DASHBOARD_SPA))
    )


def _login(client, token: str = "admin-token") -> None:
    response = client.post(
        "/login", data={"token": token}, follow_redirects=False
    )
    assert response.status_code == 303


def _wait_until(condition, timeout: float = 20.0) -> None:
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"Условие не выполнено за {timeout} с.")


def test_admin_snapshot_скрыт_от_токена_чтения(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "read-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "_JOBS", [])
    client = _client(Registry(tmp_path / "data"))

    _login(client, "read-token")
    denied = client.get("/api/v1/sources/admin")
    client.cookies.clear()
    _login(client)
    allowed = client.get("/api/v1/sources/admin")

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json() == {
        "api_version": "v1",
        "limits": {"upload_bytes": dashboard.MAX_UPLOAD},
        "configuration_names": [],
        "jobs": [],
        "incoming": [],
        "incoming_exists": False,
        "incoming_dir": "data/incoming/",
        "orphans": [],
        "snapshot_error": "",
    }


def test_spa_upload_запрещён_токену_чтения(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "read-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "_JOBS", [])
    registry = Registry(tmp_path / "data")
    client = _client(registry)
    _login(client, "read-token")

    response = client.post(
        "/api/v1/sources/upload",
        files={"file": ("структура.zip", b"synthetic")},
    )

    assert response.status_code == 403
    assert registry.snapshot().sources == {}
    assert dashboard._JOBS == []


def test_spa_cookie_mutation_отвергает_чужой_origin(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "_JOBS", [])
    exports = tmp_path / "exports"
    exports.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(
            exports,
            build_configuration(name="Отраслевая конфигурация"),
        )
    )
    client = _client(registry)
    _login(client)

    response = client.post(
        "/api/v1/sources/remove",
        headers={"origin": "http://sibling.test"},
        json={
            "id": "Отраслевая конфигурация",
            "confirmation": "Отраслевая конфигурация",
        },
    )

    assert response.status_code == 403
    assert "Отраслевая конфигурация" in registry.snapshot().sources


def test_spa_upload_принимает_файл_и_публикует_источник(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "_JOBS", [])
    registry = Registry(tmp_path / "data")
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    payload = write_export(
        export_dir, build_configuration(name="СинтетическаяКонфигурация")
    ).read_bytes()
    client = _client(registry)
    _login(client)

    response = client.post(
        "/api/v1/sources/upload",
        files={"file": ("структура.zip", payload)},
    )

    assert response.status_code == 202
    assert response.json()["job"]["name"] == "структура.zip"
    _wait_until(
        lambda: "СинтетическаяКонфигурация"
        in registry.snapshot().configurations
    )
    jobs = client.get("/api/v1/sources/admin").json()["jobs"]
    assert jobs[0]["state"] == dashboard.JOB_DONE


def test_spa_incoming_разбирается_в_выбранную_конфигурацию(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "_JOBS", [])
    registry = Registry(tmp_path / "data")
    exports = tmp_path / "exports"
    exports.mkdir()
    for name in ("Отраслевая конфигурация А", "Отраслевая конфигурация Б"):
        registry.add_configuration(
            write_export(exports, build_configuration(name=name))
        )
    registry.incoming_dir.mkdir(parents=True)
    archive = registry.incoming_dir / "полная-выгрузка.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("Configuration.xml", modules_configuration_xml())
        package.writestr(
            "Catalogs/Тест/Ext/ObjectModule.bsl",
            "Процедура Проверка()\nКонецПроцедуры",
        )
    состарить(archive)
    client = _client(registry)
    _login(client)

    snapshot = client.get("/api/v1/sources/admin").json()
    response = client.post(
        "/api/v1/sources/incoming/parse",
        json={
            "name": archive.name,
            "configuration": "Отраслевая конфигурация Б",
        },
    )

    assert snapshot["incoming"][0]["can_parse"] is True
    assert response.status_code == 202
    _wait_until(
        lambda: "Отраслевая конфигурация Б:modules" in registry.snapshot().sources
    )
    assert "Отраслевая конфигурация А:modules" not in registry.snapshot().sources
    assert archive.is_file()


def test_spa_remove_требует_точного_подтверждения_и_удаляет_каскад(
    корень_кода, реестр_из_кода, monkeypatch
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "_JOBS", [])
    registry = реестр_из_кода(корень_кода, name="Отраслевая конфигурация А")
    client = _client(registry)
    _login(client)

    denied = client.post(
        "/api/v1/sources/remove",
        json={"id": "Отраслевая конфигурация А", "confirmation": "другое"},
    )
    removed = client.post(
        "/api/v1/sources/remove",
        json={
            "id": "Отраслевая конфигурация А",
            "confirmation": "Отраслевая конфигурация А",
        },
    )

    assert denied.status_code == 400
    assert removed.status_code == 200
    assert registry.snapshot().sources == {}


def test_spa_forget_удаляет_только_файл_из_списка_orphans(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "_JOBS", [])
    registry = Registry(tmp_path / "data")
    orphan = registry.sources_dir / "hbk" / "устаревшая.hbk"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"synthetic")
    client = _client(registry)
    _login(client)

    snapshot = client.get("/api/v1/sources/admin").json()
    response = client.post(
        "/api/v1/sources/forget",
        json={
            "path": "sources/hbk/устаревшая.hbk",
            "confirmation": "sources/hbk/устаревшая.hbk",
        },
    )

    assert snapshot["orphans"] == [
        {"path": "sources/hbk/устаревшая.hbk", "size": 9}
    ]
    assert response.status_code == 200
    assert not orphan.exists()
