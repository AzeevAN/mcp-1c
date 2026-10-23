"""Административный API SPA управляет тем же Registry, что и MCP."""

from __future__ import annotations

import json
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
from mcp1c import dashboard_backend as dashboard
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.registry import Registry


def _client(registry: Registry):
    return живой_клиент(
        Starlette(routes=routes(registry, mode=DASHBOARD_ON))
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
    monkeypatch.setattr(dashboard, "JOBS", [])
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
        "reference": {
            "api_version": "v1",
            "active": {
                "state": "missing",
                "ready": False,
                "message": "Каноническая база не загружена.",
                "signature": "not-checked",
                "schema_version": None,
                "content_sha256": None,
                "file_sha256": None,
                "items": None,
                "index_cache": None,
                "key_id": None,
                "action": None,
            },
            "catalog": None,
        },
        "runtime": {"self_restart": False},
    }


def test_spa_upload_запрещён_токену_чтения(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "read-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "JOBS", [])
    registry = Registry(tmp_path / "data")
    client = _client(registry)
    _login(client, "read-token")

    response = client.post(
        "/api/v1/sources/upload",
        files={"file": ("структура.zip", b"synthetic")},
    )

    assert response.status_code == 403
    assert registry.snapshot().sources == {}
    assert dashboard.JOBS == []


def test_spa_upload_отвергает_json_снимка_расширений(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "JOBS", [])
    registry = Registry(tmp_path / "data")
    client = _client(registry)
    _login(client)

    response = client.post(
        "/api/v1/sources/upload",
        files={"file": ("снимок.json", b'{"extensions": []}')},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Принимаются только .zip и .hbk."
    assert registry.snapshot().sources == {}
    assert dashboard.JOBS == []


def test_spa_sources_не_содержит_extension_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "JOBS", [])
    exports = tmp_path / "exports"
    exports.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(exports, build_configuration(name="СинтетическаяКонфигурация"))
    )
    client = _client(registry)
    _login(client)

    response = client.get("/api/v1/sources")

    assert response.status_code == 200
    configurations = response.json()["configurations"]
    assert [c["id"] for c in configurations] == ["СинтетическаяКонфигурация"]
    assert "extension_runtime" not in configurations[0]


def test_spa_cookie_mutation_отвергает_чужой_origin(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "JOBS", [])
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
    monkeypatch.setattr(dashboard, "JOBS", [])
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


def test_spa_upload_schema_v1_другой_редакции_завершается_ошибкой_с_причиной(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "JOBS", [])
    registry = Registry(tmp_path / "data")
    старая = tmp_path / "old"
    новая = tmp_path / "new"
    старая.mkdir()
    новая.mkdir()
    registry.add_configuration(
        write_export(
            старая, build_configuration(name="СинтетическаяКонфигурация", version="2.3.26.24")
        ),
        keep_source=False,
    )
    payload = write_export(
        новая, build_configuration(name="СинтетическаяКонфигурация", version="3.0.14.178")
    ).read_bytes()
    client = _client(registry)
    _login(client)

    response = client.post(
        "/api/v1/sources/upload",
        files={"file": ("структура.zip", payload)},
    )

    assert response.status_code == 202
    _wait_until(
        lambda: client.get("/api/v1/sources/admin").json()["jobs"][0]["state"]
        == dashboard.JOB_FAILED
    )
    job = client.get("/api/v1/sources/admin").json()["jobs"][0]
    assert "редакции 2.3" in job["error"]
    assert "редакции 3.0" in job["error"]
    loaded = registry.snapshot().configurations["СинтетическаяКонфигурация"]
    assert loaded.config.version == "2.3.26.24"


def test_spa_incoming_не_разбирает_частичный_код(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "JOBS", [])
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

    assert "can_parse" not in snapshot["incoming"][0]
    assert response.status_code == 410
    assert "Отраслевая конфигурация Б:modules" not in registry.snapshot().sources
    assert "Отраслевая конфигурация А:modules" not in registry.snapshot().sources
    assert archive.is_file()


def test_spa_remove_требует_точного_подтверждения_и_удаляет_каскад(
    корень_кода, реестр_из_кода, monkeypatch
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "JOBS", [])
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
    monkeypatch.setattr(dashboard, "JOBS", [])
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


def _truncated_payload(directory) -> bytes:
    """Выгрузка schema v1 с манифестом truncated=true."""
    target = write_export(
        directory, build_configuration(name="СинтетическаяКонфигурация")
    )
    with zipfile.ZipFile(target) as archive:
        members = {
            item.filename: archive.read(item.filename)
            for item in archive.infolist()
        }
    manifest = json.loads(members["manifest.json"])
    manifest["truncated"] = True
    members["manifest.json"] = json.dumps(
        manifest, ensure_ascii=False
    ).encode("utf-8")
    with zipfile.ZipFile(target, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return target.read_bytes()


def test_spa_upload_отвергает_поле_allow_truncated(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "JOBS", [])
    registry = Registry(tmp_path / "data")
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    payload = _truncated_payload(export_dir)
    client = _client(registry)
    _login(client)

    response = client.post(
        "/api/v1/sources/upload",
        data={"allow_truncated": "1"},
        files={"file": ("структура.zip", payload)},
    )

    assert response.status_code == 400
    assert (
        "Некорректная multipart-форма: разрешён один файл `file`."
        in response.text
    )
    assert dashboard.JOBS == []
    assert registry.snapshot().sources == {}


def test_spa_upload_truncated_выгрузка_отклоняется_заданием(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(dashboard, "JOBS", [])
    registry = Registry(tmp_path / "data")
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    payload = _truncated_payload(export_dir)
    client = _client(registry)
    _login(client)

    response = client.post(
        "/api/v1/sources/upload",
        files={"file": ("структура.zip", payload)},
    )

    assert response.status_code == 202
    _wait_until(
        lambda: client.get("/api/v1/sources/admin").json()["jobs"][0]["state"]
        == dashboard.JOB_FAILED
    )
    job = client.get("/api/v1/sources/admin").json()["jobs"][0]
    assert "truncated=true" in job["error"]
    snapshot = registry.snapshot()
    assert snapshot.sources == {}
    assert snapshot.configurations == {}
