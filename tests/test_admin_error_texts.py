"""Админские ответы не раскрывают пути файловой системы из OSError.

Текст `OSError` содержит абсолютный путь внутри контейнера или хоста. Он
нужен оператору в журнале процесса, а не в JSON-ответе браузеру.
"""

from __future__ import annotations

import errno

from starlette.applications import Starlette

from conftest import живой_клиент
from mcp1c import dashboard_backend
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.registry import Registry


SECRET_PATH = "/srv/private/mcp1c/data/dictionary.json"


def _client(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = Registry(tmp_path / "data")
    client = живой_клиент(Starlette(routes=routes(registry, mode=DASHBOARD_ON)))
    client.headers["x-api-token"] = "admin-token"
    return client, registry


def test_ошибка_записи_словаря_без_пути(tmp_path, monkeypatch, capsys):
    client, _ = _client(tmp_path, monkeypatch)

    def failing(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device", SECRET_PATH)

    monkeypatch.setattr(dashboard_backend, "_apply_dictionary_change", failing)

    response = client.post(
        "/api/v1/dictionary/synonyms", json={"words": ["склад", "хранилище"]}
    )

    assert response.status_code == 409
    assert SECRET_PATH not in response.text
    assert "ENOSPC" in response.json()["error"]
    assert SECRET_PATH in capsys.readouterr().err


def test_ошибка_приёма_файла_без_пути(tmp_path, monkeypatch, capsys):
    from starlette import formparsers

    client, _ = _client(tmp_path, monkeypatch)
    real_spool = formparsers.SpooledTemporaryFile

    def failing_spool(*args, **kwargs):
        result = real_spool(*args, **kwargs)

        def failed_read(*args, **kwargs):
            raise OSError(errno.EIO, "Input/output error", SECRET_PATH)

        result.read = failed_read
        return result

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", failing_spool)

    response = client.post(
        "/api/v1/sources/upload", files={"file": ("структура.zip", b"zip")}
    )

    assert response.status_code == 500
    assert SECRET_PATH not in response.text
    assert "EIO" in response.json()["error"]
    assert SECRET_PATH in capsys.readouterr().err


def test_неожиданная_ошибка_разбора_не_уходит_в_задание_текстом(
    tmp_path, monkeypatch, capsys
):
    job = dashboard_backend._start_job("структура.zip", 1)
    directory = tmp_path / "upload"
    directory.mkdir()

    def boom(*args, **kwargs):
        raise RuntimeError(f"не открылся {SECRET_PATH}")

    monkeypatch.setattr(dashboard_backend, "_index_source", boom)

    dashboard_backend._run_job(
        Registry(tmp_path / "data"), job, str(directory),
        directory / "структура.zip", ".zip",
    )

    assert job["state"] == dashboard_backend.JOB_FAILED
    assert "RuntimeError" in job["error"]
    assert SECRET_PATH not in job["error"]
    assert SECRET_PATH in capsys.readouterr().err
