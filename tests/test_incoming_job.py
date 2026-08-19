"""Разбор по кнопке: право, единственность, отказ по месту."""
import zipfile
from pathlib import Path

from conftest import build_configuration, состарить, write_export, живой_клиент
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
    _выгрузка(registry.incoming_dir / "модули.zip")
    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))
    return client, registry


def _выгрузка(путь: Path, модуль: str = "Процедура А() КонецПроцедуры") -> Path:
    """Выгрузка в файлы из одного модуля. Возраст — «копирование закончено»."""
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr("Configuration.xml", "<x/>")
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", модуль)
    return состарить(путь)


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


def test_битый_архив_не_роняет_обработчик(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})
    битый = registry.incoming_dir / "битый.zip"
    битый.write_bytes(b"this is not a zip, just garbage bytes")
    состарить(битый)

    ответ = client.post(
        "/sources/incoming/parse", data={"name": "битый.zip"}, follow_redirects=False
    )

    assert ответ.status_code == 303
    текст = client.get("/sources").text
    assert "битый.zip" in текст
    assert "zip-архив" in текст
    assert not any(
        job["name"] == "битый.zip" and job["state"] == dashboard.JOB_READING
        for job in dashboard._JOBS
    )


def test_нет_места_отражается_в_задании(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})
    monkeypatch.setattr("mcp1c.intake.enough_space", lambda нужно, каталог: (False, 0))

    ответ = client.post(
        "/sources/incoming/parse", data={"name": "модули.zip"}, follow_redirects=False
    )

    assert ответ.status_code == 303
    текст = client.get("/sources").text
    assert "нужно" in текст and "свободно" in текст


def test_несколько_конфигураций_разбор_не_привязывается(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    ещё_входящее = tmp_path / "in2"
    ещё_входящее.mkdir()
    registry.add_configuration(
        write_export(ещё_входящее, build_configuration(name="УправлениеТорговлей"))
    )
    client.post("/login", data={"token": "секрет"})

    ответ = client.post(
        "/sources/incoming/parse", data={"name": "модули.zip"}, follow_redirects=False
    )

    assert ответ.status_code == 303
    дождаться(client, lambda t: "модули.zip" in t and "ошибка" in t)
    assert "Розница:modules" not in registry.sources
    assert "УправлениеТорговлей:modules" not in registry.sources


def test_разбор_записан_в_registry_json(tmp_path, monkeypatch):
    """Память процесса — не результат работы: рестарт её не переживает.

    `add_modules` пишет только в `self.sources`; без `registry.save()` после
    разбора страница после рестарта говорила бы «не разобрано» при 351 МБ кода
    на диске, и человек гонял бы гигабайтный архив заново.
    """
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})

    client.post(
        "/sources/incoming/parse", data={"name": "модули.zip"}, follow_redirects=False
    )
    дождаться(client, lambda t: "Розница:modules" in t)

    # Смотрим в файл, а не в память: проверка по `registry.sources` зелена и
    # без записи на диск.
    записано = registry.registry_path.read_text(encoding="utf-8")
    assert "Розница:modules" in записано

    заново = Registry(registry.data_dir)
    assert заново.restore() == []
    assert "Розница:modules" in заново.sources


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
