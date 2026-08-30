"""Загрузка источника не держит браузер и показывает, что происходит.

Разбор справки занимает около пяти секунд, выгрузки — меньше, но тоже
заметно. Страница отвечала после разбора: человек всё это время смотрел на
пустой экран и не знал, идёт работа или всё зависло.

Теперь ответ приходит сразу, а разбор уходит в фон и виден на странице
источников. Обновление страницы — обычный `meta refresh`, без JS: дашборд
обязан работать с выключенным JS, это записано в спеке.
"""

from __future__ import annotations

import time

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c import dashboard
from mcp1c.registry import Registry

from conftest import build_configuration, write_export, живой_клиент


def client_for(tmp_path) -> tuple[TestClient, Registry, bytes]:
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    выгрузка = write_export(incoming, build_configuration(name="ВтораяКонфигурация"))
    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))
    return client, registry, выгрузка.read_bytes()


def дождаться(client, условие, таймаут: float = 20.0) -> str:
    """Фоновая работа завершается не мгновенно — опрашиваем страницу.

    Таймаут щедрый намеренно: на быстрой машине он не стоит ничего, а на
    загруженном раннере пяти секунд не хватало — и падали разные тесты от
    прогона к прогону.
    """
    предел = time.monotonic() + таймаут
    текст = ""
    while time.monotonic() < предел:
        текст = client.get("/sources").text
        if условие(текст):
            return текст
        time.sleep(0.05)
    raise AssertionError(
        f"за {таймаут} с условие не выполнилось на /sources; страница:\n{текст}"
    )


def test_загрузка_отвечает_сразу_и_показывает_задание(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry, payload = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    ответ = client.post(
        "/sources", files={"file": ("Вторая.zip", payload)}, follow_redirects=False
    )

    # Ответ немедленный: разбор ещё не обязан быть закончен.
    assert ответ.status_code == 303
    страница = дождаться(client, lambda t: "ВтораяКонфигурация" in t)
    assert "ВтораяКонфигурация" in страница


def test_страница_обновляется_пока_идёт_разбор(tmp_path, monkeypatch):
    """Пока есть незавершённые задания — `meta refresh`, потом его нет."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry, payload = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    client.post("/sources", files={"file": ("Вторая.zip", payload)})
    дождаться(client, lambda t: "ВтораяКонфигурация" in t)

    # Работа окончена — автообновление должно прекратиться, иначе страница
    # будет дёргаться вечно.
    спокойная = дождаться(client, lambda t: "http-equiv=refresh" not in t)
    assert "http-equiv=refresh" not in спокойная


def test_ошибка_разбора_видна_в_задании(tmp_path, monkeypatch):
    """Раньше ошибка приходила ответом; в фоне ответа уже нет — нужен след."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry, _ = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    client.post("/sources", files={"file": ("Плохая.zip", b"not a zip at all")})

    страница = дождаться(client, lambda t: "Плохая.zip" in t and "ошибка" in t.lower())
    assert "Плохая.zip" in страница
    assert registry.configurations == {}


def test_чужое_расширение_отвергается_до_фона(tmp_path, monkeypatch):
    """Проверять расширение в фоне незачем — это видно сразу."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _, _ = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    ответ = client.post("/sources", files={"file": ("заметки.txt", b"text")})

    assert ответ.status_code == 200
    assert "только .zip, .hbk, .json и .sqlite3" in ответ.text


def test_задания_видны_только_после_входа(tmp_path, monkeypatch):
    """Имя загружаемого файла — тоже сведение о том, что за база."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _, payload = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})
    client.post("/sources", files={"file": ("Вторая.zip", payload)})
    дождаться(client, lambda t: "ВтораяКонфигурация" in t)

    client.post("/logout")
    страница = client.get("/sources").text

    assert "Вторая.zip" not in страница


def test_завершённые_задания_убираются_кнопкой(tmp_path, monkeypatch):
    """История загрузок копится на экране и мешает смотреть на актуальное."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry, payload = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    client.post("/sources", files={"file": ("Вторая.zip", payload)})
    дождаться(client, lambda t: "готово" in t)

    ответ = client.post("/sources/jobs/clear", follow_redirects=False)

    assert ответ.status_code == 303
    страница = client.get("/sources").text
    assert "Вторая.zip" not in страница
    # Сам источник остаётся: убирается журнал, а не результат работы.
    assert "ВтораяКонфигурация" in страница


def test_очистка_не_трогает_незавершённые(tmp_path, monkeypatch):
    """Идущую загрузку убирать нельзя — она ещё пишет в своё задание."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _, _ = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    идёт = dashboard._start_job("Долгая.hbk", 1)
    идёт["state"] = dashboard.JOB_PARSING
    готово = dashboard._start_job("Готовая.zip", 1)
    готово["state"] = dashboard.JOB_DONE

    client.post("/sources/jobs/clear")

    остались = [j["name"] for j in dashboard._JOBS]
    assert остались == ["Долгая.hbk"]


def test_очистка_журнала_без_токена_отклоняется(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _, _ = client_for(tmp_path)

    assert client.post("/sources/jobs/clear").status_code == 403


# --- Показ передачи файла ----------------------------------------------------
#
# Сервер хода передачи не видит: `await request.form()` возвращает управление
# только когда тело пришло целиком, и `_start_job` вызывается уже после этого.
# Проверено на живом сокете 2026-08-19: 40 МБ шли 8,1 с, за это время 32 опроса
# `/sources` со второго соединения не увидели ни одного задания.
#
# Значит показать передачу может только браузер — у него есть
# `XMLHttpRequest.upload.onprogress`. Форма при этом остаётся обычной: правило
# дашборда — работать с выключенным JS.


def test_форма_загрузки_остаётся_обычной(tmp_path, monkeypatch):
    """JS только дополняет форму. Выключен — загрузка работает как раньше."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _, _ = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    страница = client.get("/sources").text

    assert "method=post action=/sources enctype=multipart/form-data" in страница
    assert "<input type=file name=file" in страница


def test_страница_источников_несёт_индикатор_передачи(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _, _ = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    страница = client.get("/sources").text

    assert "id=upload-form" in страница
    assert "id=upload-progress" in страница
    assert "upload.onprogress" in страница


def test_предел_размера_отдан_браузеру_числом(tmp_path, monkeypatch):
    """Иначе файл-переросток заливается целиком и лишь потом получает отказ.

    Проверка `MAX_UPLOAD` на сервере стоит после приёма: к моменту отказа
    полтерабайта трафика уже потрачены. Браузер знает размер до отправки —
    но только если предел ему назвали.
    """
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _, _ = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    страница = client.get("/sources").text

    assert str(dashboard.MAX_UPLOAD) in страница


def test_индикатор_не_отдаётся_невошедшему(tmp_path, monkeypatch):
    """Форма загрузки видна только администратору — и скрипт вместе с ней."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    # Токен латиницей: заголовки HTTP кодируются latin-1, и кириллический до
    # сервера физически не доходит — см. README, раздел «Безопасность».
    monkeypatch.setenv("API_TOKEN", "read-only")
    client, _, _ = client_for(tmp_path)

    страница = client.get("/sources", headers={"X-Api-Token": "read-only"}).text

    assert "id=upload-form" not in страница
    assert "upload.onprogress" not in страница


def test_размер_мелкого_файла_показан_в_килобайтах(tmp_path, monkeypatch):
    """«0.0 МБ» не сообщает ничего: выгрузка бывает и в две тысячи байт."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _, _ = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    задание = dashboard._start_job("Мелкая.zip", 1588)
    задание["state"] = dashboard.JOB_DONE

    страница = client.get("/sources").text

    assert "2 КБ" in страница
    assert "0.0 МБ" not in страница


def test_журнал_не_называет_размер_принятым(tmp_path, monkeypatch):
    """Колонка «Принято» врала: к моменту задания файл уже пришёл целиком.

    `await request.form()` возвращает управление только когда тело получено,
    и `_start_job` вызывается после этого. Хода приёма журнал не показывает и
    показать не может — значит и называться так не должен.
    """
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, _, _ = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})
    dashboard._start_job("Любая.zip", 5 * 1024 * 1024)

    страница = client.get("/sources").text

    assert "<th>Размер" in страница
    assert "<th>Принято" not in страница
