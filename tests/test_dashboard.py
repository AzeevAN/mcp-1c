"""Дашборд: страницы, загрузка источников, прогон запросов.

Маршруты вешаются на голый `Starlette`: дашборд не знает про MCP, и тесты не
поднимают протокол. Данные — синтетические из `conftest.py`, `data/` не нужен.
"""

from __future__ import annotations

import time

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c import dashboard
from mcp1c.registry import Registry

from conftest import build_configuration, write_export, живой_клиент


def client_for(tmp_path) -> tuple[TestClient, Registry]:
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    registry.add_configuration(write_export(incoming, build_configuration()))
    app = Starlette(routes=dashboard.routes(registry))
    return живой_клиент(app), registry


def дождаться_разбора(client, *признаки: str, таймаут: float = 20.0) -> str:
    """Разбор уходит в фон, поэтому результат ждём на странице источников.

    Признаков может быть несколько, и ждём появления **всех**. Одного имени
    файла недостаточно: `_start_job` кладёт его в журнал синхронно, ещё до
    редиректа, — такое ожидание возвращается на первом же опросе, пока задание
    только принимается. Ждать надо терминальное состояние («готово», «ошибка»)
    либо имя конфигурации, которое появляется после разбора.
    """
    предел = time.monotonic() + таймаут
    текст = ""
    while time.monotonic() < предел:
        текст = client.get("/sources").text
        if all(признак in текст for признак in признаки):
            return текст
        time.sleep(0.05)
    # Молча вернуть последнюю страницу — значит превратить «ожидание истекло»
    # в невнятный провал следующего assert. Падаем здесь и называем причину.
    raise AssertionError(
        f"за {таймаут} с не дождались {признаки} на /sources; страница:\n{текст}"
    )


def test_обзор_показывает_загруженную_конфигурацию(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "ТестоваяКонфигурация" in response.text


def test_страница_источников_перечисляет_загруженное(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get("/sources")

    assert response.status_code == 200
    assert "ТестоваяКонфигурация" in response.text
    assert "configuration" in response.text


def _zip_bytes(directory, name: str = "ВтораяКонфигурация") -> bytes:
    return write_export(directory, build_configuration(name=name)).read_bytes()


def test_загрузка_без_токена_отклоняется(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    payload = _zip_bytes(tmp_path / "incoming")

    response = client.post("/sources", files={"file": ("Вторая.zip", payload)})

    assert response.status_code == 403
    assert sorted(registry.configurations) == ["ТестоваяКонфигурация"]


def test_загрузка_с_токеном_добавляет_источник(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    payload = _zip_bytes(tmp_path / "incoming")

    client.post("/login", data={"token": "секрет"})
    response = client.post(
        "/sources", files={"file": ("Вторая.zip", payload)}, follow_redirects=False
    )

    # Ответ немедленный: разбор идёт в фоне и виден на странице источников.
    assert response.status_code == 303
    дождаться_разбора(client, "ВтораяКонфигурация")
    assert "ВтораяКонфигурация" in registry.configurations


def test_неверный_токен_не_выдаёт_сессию(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, _ = client_for(tmp_path)

    response = client.post("/login", data={"token": "не тот"})

    assert response.status_code == 403
    assert "Загрузить" not in client.get("/sources").text


def test_без_переменной_окружения_загрузки_нет(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    client, _ = client_for(tmp_path)
    payload = _zip_bytes(tmp_path / "incoming")

    response = client.post("/sources", files={"file": ("Вторая.zip", payload)})

    assert response.status_code == 404


def test_файл_чужого_расширения_отклоняется(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    response = client.post("/sources", files={"file": ("заметки.txt", b"text")})

    assert response.status_code == 200
    assert "только .zip и .hbk" in response.text
    assert sorted(registry.configurations) == ["ТестоваяКонфигурация"]


def test_битый_архив_не_меняет_реестр(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    response = client.post(
        "/sources", files={"file": ("Плохая.zip", b"not a zip")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    # Ошибку возвращать уже некуда — она ложится в задание.
    страница = дождаться_разбора(client, "Плохая.zip", "ошибка")
    assert "ошибка" in страница.lower()
    assert sorted(registry.configurations) == ["ТестоваяКонфигурация"]


def test_чужой_hbk_объясняет_причину_а_не_падает(tmp_path, monkeypatch):
    """В каталоге установки 1С 38 файлов `.hbk`, подходит один.

    Человек берёт соседний — и обязан получить объяснение, а не 500:
    `V8ContainerError` не наследует ни один из перехватываемых классов, и
    сообщение парсера («не контейнер 1С») до страницы не доходило.
    """
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    response = client.post(
        "/sources", files={"file": ("shlang_ru.hbk", b"not a container")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    страница = дождаться_разбора(client, "shlang_ru.hbk", "ошибка")
    assert "ошибка" in страница.lower()
    assert registry.syntax is None


def test_удаление_источника_убирает_его_из_реестра(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    response = client.post(
        "/sources/remove",
        data={"id": "ТестоваяКонфигурация"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert registry.configurations == {}


def test_удаление_без_токена_отклоняется(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)

    response = client.post("/sources/remove", data={"id": "ТестоваяКонфигурация"})

    assert response.status_code == 403
    assert "ТестоваяКонфигурация" in registry.configurations


def test_прогон_возвращает_попадания_с_причиной(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.post(
        "/queries",
        data={
            "config": "ТестоваяКонфигурация",
            "scope": "objects",
            "phrases": "контрагенты\nреализация услуг",
        },
    )

    assert response.status_code == 200
    assert "Справочник.Контрагенты" in response.text
    assert "Документ.РеализацияТоваровУслуг" in response.text
    assert "все слова запроса" in response.text


def test_прогон_объясняет_превышение_лимита_поисковой_фразы(tmp_path):
    client, _ = client_for(tmp_path)
    words = ["слово" + chr(1072 + index // 32) + chr(1072 + index % 32)
             for index in range(33)]

    response = client.post(
        "/queries",
        data={
            "config": "ТестоваяКонфигурация",
            "scope": "objects",
            "phrases": " ".join(words),
        },
    )

    assert response.status_code == 200
    assert "не более 32 различных токенов" in response.text


def test_прогон_по_реквизитам(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.post(
        "/queries",
        data={
            "config": "ТестоваяКонфигурация",
            "scope": "fields",
            "phrases": "номер телефона",
        },
    )

    assert "Справочник.Контрагенты.Телефон" in response.text


def test_фраза_отображается_экранированной(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.post(
        "/queries",
        data={
            "config": "ТестоваяКонфигурация",
            "scope": "objects",
            "phrases": "<script>alert(1)</script>",
        },
    )

    assert response.status_code == 200
    assert "&lt;script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_справка_фильтруется_по_версии_платформы(tmp_path):
    """Конфигурация на 8.3.5 не должна видеть то, что появилось в 8.3.6."""
    from conftest import write_syntax

    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    config = build_configuration()
    config.platform = "8.3.5.1570"
    registry = Registry(data_dir)
    registry.add_configuration(write_export(incoming, config))
    registry.add_syntax(write_syntax(data_dir / "index" / "syntax"))
    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))

    response = client.post(
        "/queries",
        data={
            "config": "ТестоваяКонфигурация",
            "scope": "syntax",
            # Две фразы: одна ищет элемент без ограничения по версии, другая —
            # появившийся в 8.3.6. Первая доказывает, что поиск отработал,
            # вторая — что фильтр по версии применён.
            "phrases": "ЗаписатьНачалоОбъекта\nСтрНайти",
        },
    )

    assert response.status_code == 200
    assert "ЗаписатьНачалоОбъекта" in response.text
    assert "method.СтрНайти" not in response.text


def test_результат_ссылается_на_карточку_объекта(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.post(
        "/queries",
        data={
            "config": "ТестоваяКонфигурация",
            "scope": "objects",
            "phrases": "контрагенты",
        },
    )

    assert "/object?" in response.text
    assert "%D0%A1%D0%BF%D1%80%D0%B0%D0%B2%D0%BE%D1%87%D0%BD%D0%B8%D0%BA" in response.text


def test_карточка_объекта_показывает_реквизиты(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get(
        "/object",
        params={"config": "ТестоваяКонфигурация", "name": "Справочник.Контрагенты"},
    )

    assert response.status_code == 200
    assert "ИНН" in response.text
    assert "Телефон" in response.text


def test_реквизит_ведёт_на_карточку_объекта_владельца(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.post(
        "/queries",
        data={
            "config": "ТестоваяКонфигурация",
            "scope": "fields",
            "phrases": "номер телефона",
        },
    )

    # У реквизита своей карточки нет: ссылка ведёт на объект-владелец.
    assert "name=%D0%A1%D0%BF%D1%80%D0%B0%D0%B2%D0%BE%D1%87%D0%BD%D0%B8%D0%BA." in response.text


def test_карточка_справки_открывается(tmp_path):
    from conftest import write_syntax

    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    registry.add_configuration(write_export(incoming, build_configuration()))
    registry.add_syntax(write_syntax(data_dir / "index" / "syntax"))
    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))

    response = client.get(
        "/syntax",
        params={"config": "ТестоваяКонфигурация", "name": "СтрНайти"},
    )

    assert response.status_code == 200
    # Текст из фикстуры conftest.build_syntax, не из настоящей справки.
    assert "Находит вхождение подстроки" in response.text
    assert "8.3.6" in response.text


def test_несуществующий_объект_объясняет_а_не_падает(tmp_path):
    client, _ = client_for(tmp_path)

    response = client.get(
        "/object",
        params={"config": "ТестоваяКонфигурация", "name": "Документ.ТакогоНет"},
    )

    assert response.status_code == 200
    assert "ТакогоНет" in response.text


def test_имя_конфигурации_не_исполняется_как_разметка(tmp_path):
    """Манифест делает чужая обработка в чужой базе — это внешние данные."""
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    # Без косой черты: имя конфигурации попадает в имя файла выгрузки.
    config = build_configuration(name="<script>alert(1)")
    registry = Registry(data_dir)
    registry.add_configuration(write_export(incoming, config))
    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))

    response = client.get("/")

    assert "<script>alert(1)" not in response.text
    assert "&lt;script&gt;" in response.text
