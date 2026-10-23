"""Код native-поколения в реестре — второе измерение `ResolvedContext`.

Код приходит только native-поколением (`native_export.опубликовать_код`).
Строки кода, загруженного отдельно от структуры до 4.2.0, restore снимает с
учёта, не поднимая их.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from starlette.applications import Starlette

from conftest import build_configuration, живой_клиент, write_export
from mcp1c.registry import Registry
from mcp1c.server import build_server
from native_export import опубликовать_код


def _реестр_с_конфигурацией(
    рабочий: Path, *, name: str = "Пример", version: str = "1.0"
) -> Registry:
    входящее = рабочий / "in"
    входящее.mkdir(parents=True, exist_ok=True)
    реестр = Registry(рабочий / "data")
    реестр.add_configuration(
        write_export(входящее, build_configuration(name=name, version=version))
    )
    return реестр


def test_модули_попадают_в_контекст(tmp_path_factory, корень_кода):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = Registry(рабочий / "data")
    опубликовать_код(
        реестр,
        корень_кода,
        configuration=build_configuration(name="Пример"),
        рабочий=рабочий,
    )

    контекст = реестр.resolve("Пример")

    assert контекст.modules is not None
    assert контекст.modules.оглавление.по_имени("сложить")


def test_без_выгрузки_кода_состояния_модулей_нет(tmp_path_factory):
    рабочий = tmp_path_factory.mktemp("реестр")
    реестр = _реестр_с_конфигурацией(рабочий)

    assert реестр.resolve("Пример").modules is None


def test_admin_reload_не_блокирует_health(tmp_path, monkeypatch):
    """`run_in_threadpool` оставляет event loop доступным во время reload."""
    реестр = Registry(tmp_path / "data")
    начат = threading.Event()
    отпустить = threading.Event()

    def медленный_старт():
        начат.set()
        отпустить.wait(timeout=2)
        return []

    monkeypatch.setattr(реестр, "startup", медленный_старт)
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    server = build_server(реестр)
    client = живой_клиент(Starlette(routes=server._custom_starlette_routes[:2]))
    ответы = []
    поток = threading.Thread(
        target=lambda: ответы.append(
            client.post("/admin/reload", headers={"x-admin-token": "secret"})
        )
    )
    поток.start()
    try:
        assert начат.wait(timeout=1)
        начало = time.monotonic()
        ответ_health = client.get("/health")
        assert time.monotonic() - начало < 0.5
        assert ответ_health.status_code == 200
    finally:
        отпустить.set()
        поток.join(timeout=2)

    assert not поток.is_alive()
    assert ответы[0].status_code == 200
