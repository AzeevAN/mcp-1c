"""Разбор по кнопке: право, единственность, отказ по месту."""
import zipfile
from pathlib import Path

from conftest import build_configuration, modules_configuration_xml, состарить, write_export, живой_клиент
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
        zf.writestr("Configuration.xml", modules_configuration_xml())
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
    страница = дождаться(client, lambda t: "модули.zip" in t and "ошибка" in t)
    assert "Розница:modules" not in registry.sources
    assert "УправлениеТорговлей:modules" not in registry.sources
    # Причина — сообщение человеку, а не отчёт для разработчика: имя класса
    # исключения в него не протекает. Неожиданная ошибка печатается иначе,
    # с именем класса и стеком в логе, — так же делит ошибки `_run_job`.
    assert "конфигураций" in страница
    assert "RegistryError" not in страница


def test_выбор_конфигурации_привязывает_код_к_ней(tmp_path, monkeypatch):
    """Форма шлёт выбранное имя — оно и есть хозяйка, вторая не задета."""
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
        "/sources/incoming/parse",
        data={"name": "модули.zip", "configuration": "УправлениеТорговлей"},
        follow_redirects=False,
    )

    assert ответ.status_code == 303
    дождаться(client, lambda t: "УправлениеТорговлей:modules" in t or dashboard.JOB_DONE in t)
    assert "УправлениеТорговлей:modules" in registry.sources
    assert "Розница:modules" not in registry.sources
    assert (registry.modules_dir / "УправлениеТорговлей").is_dir()


def test_неизвестная_конфигурация_отклоняется_до_разбора(tmp_path, monkeypatch):
    """Форму присылает человек — подставить можно что угодно, проверяем."""
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
        "/sources/incoming/parse",
        data={"name": "модули.zip", "configuration": "НетТакой"},
        follow_redirects=False,
    )

    assert ответ.status_code == 303
    текст = client.get("/sources").text
    assert "НетТакой" in текст
    assert "реестре" in текст
    assert "Розница:modules" not in registry.sources
    assert "УправлениеТорговлей:modules" not in registry.sources
    assert not (registry.modules_dir / "НетТакой").exists()


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
    # Ждём «готово» у задания, а не появления источника на странице: источник
    # виден из памяти сразу после `add_modules`, а `save()` идёт следом —
    # ожидание по нему попадало бы в это окно и давало флейк.
    дождаться(client, lambda t: dashboard.JOB_DONE in t)

    # Смотрим в файл, а не в память: проверка по `registry.sources` зелена и
    # без записи на диск.
    записано = registry.registry_path.read_text(encoding="utf-8")
    assert "Розница:modules" in записано

    заново = Registry(registry.data_dir)
    assert заново.restore() == []
    assert "Розница:modules" in заново.sources


def test_ноль_отобранных_файлов_не_даёт_разобрано(tmp_path, monkeypatch):
    """Выгрузка метаданных — тоже .zip, и отбор не находит в ней ничего."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})
    метаданные = registry.incoming_dir / "метаданные.zip"
    with zipfile.ZipFile(метаданные, "w") as zf:
        zf.writestr("manifest.json", '{"schema_version": "1"}')
    состарить(метаданные)

    client.post(
        "/sources/incoming/parse",
        data={"name": "метаданные.zip"},
        follow_redirects=False,
    )

    # Ждём состояние строки, а не текст задания: задание получает ошибку
    # раньше, чем `note_failure` успевает её записать.
    текст = дождаться(client, lambda t: "разбор не удался" in t)
    assert "ни модулей, ни форм" in текст
    assert "Розница:modules" not in registry.sources


def test_копирующийся_файл_обработчик_не_берёт(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})
    свежий = registry.incoming_dir / "свежий.zip"
    with zipfile.ZipFile(свежий, "w") as zf:
        zf.writestr("Catalogs/Т/Ext/ObjectModule.bsl", "Процедура А() КонецПроцедуры")
    # `состарить` намеренно не зовём: файл только что записан.

    ответ = client.post(
        "/sources/incoming/parse", data={"name": "свежий.zip"}, follow_redirects=False
    )

    assert ответ.status_code == 303
    текст = client.get("/sources").text
    assert "копирование ещё" in текст
    assert "Розница:modules" not in registry.sources


def test_занятость_объясняется(tmp_path, monkeypatch):
    """Молчаливый редирект выглядел бы как «нажал, и ничего не произошло»."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})
    dashboard._scanner(registry).running.add("другая.zip")
    try:
        ответ = client.post(
            "/sources/incoming/parse",
            data={"name": "модули.zip"},
            follow_redirects=False,
        )
    finally:
        dashboard._scanner(registry).running.discard("другая.zip")

    assert ответ.status_code == 303
    текст = client.get("/sources").text
    assert "уже идёт разбор" in текст
    assert "Розница:modules" not in registry.sources


def test_падение_проверки_места_названо_своей_причиной(tmp_path, monkeypatch):
    """Заголовок «не похоже на zip-архив» на этом отказе был бы ложью.

    `enough_space` падает не из-за архива, а из-за каталога данных: нет прав,
    том отвалился. Постановка (§3) требует, чтобы случай прав был в тексте
    ошибки, а не оставался догадкой.
    """
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})

    def нет_прав(нужно, каталог):
        raise PermissionError(f"[Errno 13] Permission denied: '{каталог}'")

    monkeypatch.setattr("mcp1c.intake.enough_space", нет_прав)

    client.post(
        "/sources/incoming/parse", data={"name": "модули.zip"}, follow_redirects=False
    )

    текст = client.get("/sources").text
    assert "свободное место" in текст
    assert "uid 10001" in текст
    assert "не похоже на zip-архив" not in текст
    assert "Розница:modules" not in registry.sources


def test_сканирование_идёт_не_в_цикле_событий(tmp_path, monkeypatch):
    """sha256 архива на 1,4 ГБ в цикле событий останавливает весь процесс."""
    import asyncio

    from mcp1c.incoming import IncomingScanner

    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _ = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})
    где_считали = []

    def подмена(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            где_считали.append("поток")
        else:
            где_считали.append("цикл событий")
        return []

    monkeypatch.setattr(IncomingScanner, "scan", подмена)

    client.get("/sources")

    assert где_считали == ["поток"]


def test_запись_неудачи_идёт_не_в_цикле_событий(tmp_path, monkeypatch):
    """`note_failure` считает sha256 — на 1,4 ГБ это секунды."""
    import asyncio

    from mcp1c.incoming import IncomingScanner

    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})
    битый = registry.incoming_dir / "битый.zip"
    битый.write_bytes(b"not a zip at all")
    состарить(битый)
    где_писали = []
    настоящая = IncomingScanner.note_failure

    def подмена(self, путь, причина):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            где_писали.append("поток")
        else:
            где_писали.append("цикл событий")
        return настоящая(self, путь, причина)

    monkeypatch.setattr(IncomingScanner, "note_failure", подмена)

    client.post(
        "/sources/incoming/parse", data={"name": "битый.zip"}, follow_redirects=False
    )

    assert где_писали == ["поток"]


def test_снятие_источника_идёт_не_в_цикле_событий(tmp_path, monkeypatch):
    """Снятие источника модулей уносит тысячи файлов — не словарная операция."""
    import asyncio

    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})
    где_снимали = []
    настоящий = Registry.remove

    def подмена(self, source_id):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            где_снимали.append("поток")
        else:
            где_снимали.append("цикл событий")
        return настоящий(self, source_id)

    monkeypatch.setattr(Registry, "remove", подмена)

    client.post("/sources/remove", data={"id": "Розница"}, follow_redirects=False)

    assert где_снимали == ["поток"]
    assert "Розница" not in registry.sources


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
