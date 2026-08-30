"""Каталог в incoming: отпечаток, скан, разбор и повтор."""
from pathlib import Path

from conftest import (
    build_configuration,
    modules_configuration_xml,
    состарить,
    write_export,
    живой_клиент,
)
from starlette.applications import Starlette

from mcp1c import dashboard
from mcp1c.dashboard_runtime import DASHBOARD_SPA, routes as spa_routes
from mcp1c.incoming import STATE_FAILED, STATE_NEW, STATE_READY, IncomingScanner
from mcp1c.intake import (
    INDEX_RESERVE,
    extract,
    identity_digest,
    identity_files,
    planned_size,
)
from mcp1c.registry import Registry


def _каталог_выгрузки(
    корень: Path,
    *,
    dump_info: str | None = "dump-1",
    version: str | None = None,
    модуль: str = "Процедура А() КонецПроцедуры",
) -> Path:
    корень.mkdir(parents=True, exist_ok=True)
    (корень / "Configuration.xml").write_text(
        modules_configuration_xml(), encoding="utf-8"
    )
    (корень / "Catalogs/Т/Ext").mkdir(parents=True, exist_ok=True)
    (корень / "Catalogs/Т/Ext/ObjectModule.bsl").write_text(модуль, encoding="utf-8")
    (корень / "Ext/ParentConfigurations").mkdir(parents=True, exist_ok=True)
    (корень / "Ext/ParentConfigurations/П.cf").write_bytes(b"C" * 100)
    if dump_info is not None:
        (корень / "ConfigDumpInfo.xml").write_text(dump_info, encoding="utf-8")
    if version is not None:
        (корень / "VERSION").write_text(version, encoding="utf-8")
    for путь in корень.rglob("*"):
        if путь.is_file():
            состарить(путь)
    return корень


def test_extract_каталога_кладёт_только_отобранное(tmp_path):
    выгрузка = _каталог_выгрузки(tmp_path / "dump")
    корень = tmp_path / "modules"

    файлов, байт = extract(выгрузка, корень)
    нужно, формат = planned_size(выгрузка)

    assert формат == "tree"
    assert файлов == 1
    assert (корень / "Catalogs/Т/Ext/ObjectModule.bsl").read_text(
        encoding="utf-8"
    ) == "Процедура А() КонецПроцедуры"
    assert not (корень / "Ext").exists()
    assert not (корень / "Configuration.xml").exists()
    assert нужно == байт + INDEX_RESERVE


def test_обёртка_каталога_снимается_как_у_zip(tmp_path):
    внутренний = _каталог_выгрузки(tmp_path / "dump" / "Розница")
    корень = tmp_path / "modules"

    файлов, _ = extract(внутренний.parent, корень)

    assert файлов == 1
    assert (корень / "Catalogs/Т/Ext/ObjectModule.bsl").is_file()
    assert not (корень / "Розница").exists()


def test_отпечаток_каталога_из_config_dump_info(tmp_path):
    первый = _каталог_выгрузки(tmp_path / "a", dump_info="alpha")
    второй = _каталог_выгрузки(tmp_path / "b", dump_info="beta")

    assert identity_digest(первый) != identity_digest(второй)
    assert [p.name for p in identity_files(первый)] == ["ConfigDumpInfo.xml"]


def test_без_dump_info_хешируется_configuration_xml(tmp_path):
    каталог = _каталог_выгрузки(tmp_path / "dump", dump_info=None)
    другой = _каталог_выгрузки(tmp_path / "other", dump_info=None)
    (другой / "Configuration.xml").write_text(
        modules_configuration_xml(name="Другая"), encoding="utf-8"
    )
    состарить(другой / "Configuration.xml")

    assert [p.name for p in identity_files(каталог)] == ["Configuration.xml"]
    assert identity_digest(каталог) != identity_digest(другой)


def test_version_gitsync_входит_в_отпечаток(tmp_path):
    без_версии = _каталог_выгрузки(tmp_path / "a", dump_info="same")
    с_версией = _каталог_выгрузки(tmp_path / "b", dump_info="same", version="1.2.3")

    assert [p.name for p in identity_files(с_версией)] == [
        "ConfigDumpInfo.xml",
        "VERSION",
    ]
    assert identity_digest(без_версии) != identity_digest(с_версией)


def test_gitsync_без_dump_info_хеширует_configuration_и_version(tmp_path):
    каталог = _каталог_выгрузки(
        tmp_path / "dump", dump_info=None, version="1.2.3"
    )
    другой = _каталог_выгрузки(
        tmp_path / "other", dump_info=None, version="1.2.4"
    )

    assert [p.name for p in identity_files(каталог)] == [
        "Configuration.xml",
        "VERSION",
    ]
    assert identity_digest(каталог) != identity_digest(другой)


def test_нет_манифеста_не_хешируется_по_дереву(tmp_path):
    пустой = tmp_path / "empty"
    пустой.mkdir()
    (пустой / "Catalogs").mkdir()
    (пустой / "VERSION").write_text("1.0", encoding="utf-8")

    assert identity_files(пустой) == ()


def test_скан_видит_каталог_рядом_с_zip(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.incoming_dir.mkdir(parents=True)
    zip_path = registry.incoming_dir / "модули.zip"
    zip_path.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    состарить(zip_path)
    _каталог_выгрузки(registry.incoming_dir / "Розница")

    строки = IncomingScanner(registry).scan()
    имена = [row["name"] for row in строки]

    assert имена == ["Розница", "модули.zip"]
    по_имени = {row["name"]: row for row in строки}
    assert по_имени["Розница"]["kind"] == "directory"
    assert по_имени["модули.zip"]["kind"] == "archive"
    assert по_имени["Розница"]["state"] == STATE_NEW
    assert по_имени["модули.zip"]["state"] == STATE_NEW


def test_вложенные_папки_выгрузки_не_отдельные_строки(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.incoming_dir.mkdir(parents=True)
    _каталог_выгрузки(registry.incoming_dir / "Розница")

    имена = [row["name"] for row in IncomingScanner(registry).scan()]

    assert имена == ["Розница"]


def test_каталог_без_манифеста_показан_как_неудача(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.incoming_dir.mkdir(parents=True)
    (registry.incoming_dir / "мусор").mkdir()
    состарить(registry.incoming_dir / "мусор")

    строки = IncomingScanner(registry).scan()

    assert строки[0]["state"] == STATE_FAILED
    assert "ConfigDumpInfo.xml" in строки[0]["detail"]
    assert "Configuration.xml" in строки[0]["detail"]


def test_add_modules_из_каталога_и_повтор_заменяет_код(tmp_path):
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(входящее, build_configuration(name="Розница"))
    )
    каталог = _каталог_выгрузки(tmp_path / "dump", dump_info="v1", модуль="Процедура Старая() КонецПроцедуры")

    первый = registry.add_modules(каталог, configuration="Розница")
    корень = registry.modules[первый.id].корень
    assert "Старая" in (корень / "Catalogs/Т/Ext/ObjectModule.bsl").read_text(
        encoding="utf-8"
    )

    (каталог / "Catalogs/Т/Ext/ObjectModule.bsl").write_text(
        "Процедура Новая() КонецПроцедуры", encoding="utf-8"
    )
    (каталог / "ConfigDumpInfo.xml").write_text("v2", encoding="utf-8")
    состарить(каталог / "ConfigDumpInfo.xml")
    состарить(каталог / "Catalogs/Т/Ext/ObjectModule.bsl")

    второй = registry.add_modules(каталог, configuration="Розница")
    assert второй.sha256 != первый.sha256
    assert "Новая" in (корень / "Catalogs/Т/Ext/ObjectModule.bsl").read_text(
        encoding="utf-8"
    )
    assert каталог.is_dir()


def _стенд(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    данные = tmp_path / "data"
    входящее = tmp_path / "in"
    данные.mkdir()
    входящее.mkdir()
    registry = Registry(данные)
    registry.add_configuration(
        write_export(входящее, build_configuration(name="Розница"))
    )
    registry.incoming_dir.mkdir(parents=True, exist_ok=True)
    каталог = _каталог_выгрузки(registry.incoming_dir / "Розница")
    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))
    client.post("/login", data={"token": "секрет"})
    return client, registry, каталог


def test_classic_кнопка_переразобрать_у_разобранного_каталога(tmp_path, monkeypatch):
    client, registry, каталог = _стенд(tmp_path, monkeypatch)
    хеш = IncomingScanner(registry).digest(каталог)
    from mcp1c.intake import SELECTION_VERSION
    from mcp1c.registry import KIND_MODULES, Source

    registry.sources["Розница:modules"] = Source(
        id="Розница:modules",
        kind=KIND_MODULES,
        origin="Розница",
        sha256=хеш,
        selection_version=SELECTION_VERSION,
    )

    страница = client.get("/sources").text

    assert "разобрано" in страница
    assert "<button>переразобрать</button>" in страница.split("Входящие выгрузки")[1]


def test_spa_разбирает_каталог_и_повтор_даёт_202(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setattr(dashboard, "_JOBS", [])
    входящее = tmp_path / "in"
    входящее.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(входящее, build_configuration(name="Розница"))
    )
    каталог = _каталог_выгрузки(registry.incoming_dir / "Розница")
    client = живой_клиент(
        Starlette(routes=spa_routes(registry, mode=DASHBOARD_SPA))
    )
    client.post("/login", data={"token": "admin-token"})

    snapshot = client.get("/api/v1/sources/admin").json()
    row = next(item for item in snapshot["incoming"] if item["name"] == "Розница")
    assert row["kind"] == "directory"
    assert row["can_parse"] is True
    assert row["action"] == "parse"

    first = client.post(
        "/api/v1/sources/incoming/parse",
        json={"name": "Розница", "configuration": "Розница"},
    )
    assert first.status_code == 202
    _wait_until(lambda: "Розница:modules" in registry.snapshot().sources)

    ready = client.get("/api/v1/sources/admin").json()
    parsed = next(item for item in ready["incoming"] if item["name"] == "Розница")
    assert parsed["state"] == STATE_READY
    assert parsed["can_parse"] is True
    assert parsed["action"] == "reparse"

    second = client.post(
        "/api/v1/sources/incoming/parse",
        json={"name": "Розница", "configuration": "Розница"},
    )
    assert second.status_code == 202
    assert каталог.is_dir()


def _wait_until(условие, таймаут: float = 20.0) -> None:
    import time

    предел = time.monotonic() + таймаут
    while time.monotonic() < предел:
        if условие():
            return
        time.sleep(0.05)
    raise AssertionError("условие не выполнилось")
