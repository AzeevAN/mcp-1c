"""Параллельные сохранения и административные правки словаря."""

from __future__ import annotations

import threading
from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from conftest import build_configuration, write_export
from mcp1c import dashboard
from mcp1c.dictionary import Dictionary
from mcp1c.registry import Registry


def _параллельно(*functions) -> list[Exception]:
    старт = threading.Barrier(len(functions) + 1)
    ошибки: list[Exception] = []
    замок = threading.Lock()

    def выполнить(function) -> None:
        try:
            старт.wait(timeout=5)
            function()
        except Exception as error:
            with замок:
                ошибки.append(error)

    потоки = [
        threading.Thread(target=выполнить, args=(function,), daemon=True)
        for function in functions
    ]
    for поток in потоки:
        поток.start()
    старт.wait(timeout=5)
    for поток in потоки:
        поток.join(timeout=10)
        assert not поток.is_alive()
    return ошибки


def test_параллельные_save_используют_уникальные_временные_файлы(
    tmp_path: Path,
    monkeypatch,
) -> None:
    путь = tmp_path / "dictionary.json"
    первый = Dictionary(path=путь)
    первый.add_alias("первый", ["Справочник.Первый"])
    второй = Dictionary(path=путь)
    второй.add_alias("второй", ["Справочник.Второй"])
    встретились = threading.Barrier(2)
    временные: list[Path] = []
    замок = threading.Lock()
    настоящий_replace = Path.replace

    def заменить_одновременно(self: Path, target: Path) -> Path:
        if Path(target) == путь:
            with замок:
                временные.append(self)
            встретились.wait(timeout=5)
        return настоящий_replace(self, target)

    monkeypatch.setattr(Path, "replace", заменить_одновременно)

    ошибки = _параллельно(первый.save, второй.save)

    assert ошибки == []
    assert len(временные) == 2
    assert временные[0] != временные[1]
    assert Dictionary.load(путь).aliases
    assert not list(tmp_path.glob(".dictionary.json.*.tmp"))


def test_параллельные_admin_правки_сериализованы_и_обе_сохранены(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    registry.add_configuration(write_export(incoming, build_configuration()))
    app = Starlette(routes=dashboard.routes(registry))
    clients = [TestClient(app), TestClient(app)]

    настоящий_add_alias = Dictionary.add_alias
    состояние_мутации = threading.Lock()
    второй_мутирует = threading.Event()
    активных_мутаций = 0
    максимум_мутаций = 0
    мутаций = 0

    def удержать_первую_мутацию(self: Dictionary, *args, **kwargs):
        nonlocal активных_мутаций, максимум_мутаций, мутаций
        with состояние_мутации:
            мутаций += 1
            номер = мутаций
            активных_мутаций += 1
            максимум_мутаций = max(максимум_мутаций, активных_мутаций)
            if номер == 2:
                второй_мутирует.set()
        if номер == 1:
            второй_мутирует.wait(timeout=1)
        try:
            return настоящий_add_alias(self, *args, **kwargs)
        finally:
            with состояние_мутации:
                активных_мутаций -= 1

    настоящий_save = Dictionary.save
    состояние = threading.Lock()
    второй_вошёл = threading.Event()
    активных = 0
    максимум = 0
    вызовов = 0

    def удержать_первое_сохранение(self: Dictionary, *args, **kwargs):
        nonlocal активных, максимум, вызовов
        with состояние:
            вызовов += 1
            номер = вызовов
            активных += 1
            максимум = max(максимум, активных)
            if номер == 2:
                второй_вошёл.set()
        if номер == 1:
            второй_вошёл.wait(timeout=1)
        try:
            return настоящий_save(self, *args, **kwargs)
        finally:
            with состояние:
                активных -= 1

    monkeypatch.setattr(Dictionary, "add_alias", удержать_первую_мутацию)
    monkeypatch.setattr(Dictionary, "save", удержать_первое_сохранение)
    ответы = []
    замок_ответов = threading.Lock()

    def добавить(client: TestClient, phrase: str, target: str) -> None:
        response = client.post(
            "/dictionary/alias",
            headers={"x-api-token": "secret"},
            data={
                "phrase": phrase,
                "targets": target,
                "config": "ТестоваяКонфигурация",
            },
            follow_redirects=False,
        )
        with замок_ответов:
            ответы.append(response.status_code)

    try:
        ошибки = _параллельно(
            lambda: добавить(
                clients[0], "первый параллельный", "Справочник.Контрагенты"
            ),
            lambda: добавить(
                clients[1], "второй параллельный", "Документ.ЗаказПокупателя"
            ),
        )
    finally:
        for client in clients:
            client.close()

    assert ошибки == []
    assert sorted(ответы) == [303, 303]
    assert максимум_мутаций == 1
    assert максимум == 1

    ожидаемые = {
        "первый параллельный": ["Справочник.Контрагенты"],
        "второй параллельный": ["Документ.ЗаказПокупателя"],
    }
    сохранённый = Dictionary.load(registry.dictionary_path)
    assert сохранённый.aliases_for(
        "ТестоваяКонфигурация", with_builtin=False
    ) == ожидаемые
    assert registry.dictionary.aliases_for(
        "ТестоваяКонфигурация", with_builtin=False
    ) == ожидаемые
    aliases_индекса = registry.configurations[
        "ТестоваяКонфигурация"
    ].index.aliases
    assert {phrase: aliases_индекса[phrase] for phrase in ожидаемые} == ожидаемые
