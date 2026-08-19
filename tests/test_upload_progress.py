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

from conftest import build_configuration, write_export


def client_for(tmp_path) -> tuple[TestClient, Registry, bytes]:
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    выгрузка = write_export(incoming, build_configuration(name="ВтораяКонфигурация"))
    client = TestClient(Starlette(routes=dashboard.routes(registry)))
    return client, registry, выгрузка.read_bytes()


def дождаться(client, условие, таймаут: float = 5.0) -> str:
    """Фоновая работа завершается не мгновенно — опрашиваем страницу."""
    предел = time.monotonic() + таймаут
    текст = ""
    while time.monotonic() < предел:
        текст = client.get("/sources").text
        if условие(текст):
            return текст
        time.sleep(0.05)
    return текст


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
    assert "только .zip и .hbk" in ответ.text


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
