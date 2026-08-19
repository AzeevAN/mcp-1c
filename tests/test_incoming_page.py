"""Блок `incoming/` — свой, в «Исходные файлы» не подмешивается."""
from conftest import build_configuration, состарить, write_export, живой_клиент
from starlette.applications import Starlette

from mcp1c import dashboard
from mcp1c.registry import Registry


def _клиент(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    данные = tmp_path / "data"
    входящее = tmp_path / "in"
    данные.mkdir()
    входящее.mkdir()
    registry = Registry(данные)
    registry.add_configuration(write_export(входящее, build_configuration()))
    registry.incoming_dir.mkdir(parents=True, exist_ok=True)
    (registry.incoming_dir / "модули.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))
    client.post("/login", data={"token": "секрет"})
    return client


def test_входящие_показаны_своим_блоком(tmp_path, monkeypatch):
    client = _клиент(tmp_path, monkeypatch)

    страница = client.get("/sources").text

    assert "Входящие выгрузки" in страница
    assert "модули.zip" in страница
    assert "не разобрано" in страница


def test_входящие_не_попадают_в_исходные_файлы(tmp_path, monkeypatch):
    client = _клиент(tmp_path, monkeypatch)

    страница = client.get("/sources").text

    хвост = страница.split("Входящие выгрузки")[0]
    assert "модули.zip" not in хвост


def test_невошедшему_список_входящих_не_виден(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.setenv("API_TOKEN", "read-only")
    данные = tmp_path / "data"
    входящее = tmp_path / "in"
    данные.mkdir()
    входящее.mkdir()
    registry = Registry(данные)
    registry.add_configuration(write_export(входящее, build_configuration()))
    registry.incoming_dir.mkdir(parents=True, exist_ok=True)
    (registry.incoming_dir / "модули.zip").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))

    страница = client.get("/sources", headers={"X-Api-Token": "read-only"}).text

    assert "модули.zip" not in страница


def _стенд_со_свежим_реестром(tmp_path, monkeypatch):
    """Клиент и реестр: архив дописан (mtime в прошлом), кнопка возможна."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    данные = tmp_path / "data"
    входящее = tmp_path / "in"
    данные.mkdir()
    входящее.mkdir()
    registry = Registry(данные)
    registry.add_configuration(write_export(входящее, build_configuration()))
    registry.incoming_dir.mkdir(parents=True, exist_ok=True)
    архив = registry.incoming_dir / "модули.zip"
    архив.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    состарить(архив)
    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))
    client.post("/login", data={"token": "секрет"})
    return client, registry, архив


def test_у_неудачи_есть_кнопка_разобрать(tmp_path, monkeypatch):
    """«Разбор не удался» — не тупик: постановка назначает ему то же действие."""
    client, registry, архив = _стенд_со_свежим_реестром(tmp_path, monkeypatch)
    dashboard._scanner(registry).note_failure(архив, "битый архив")

    страница = client.get("/sources").text

    assert "разбор не удался" in страница
    хвост = страница.split("Входящие выгрузки")[1]
    assert "<button>разобрать</button>" in хвост
