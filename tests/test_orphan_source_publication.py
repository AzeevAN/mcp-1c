"""Очистка orphan не удаляет действующий или публикуемый source."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from conftest import build_configuration, write_export
from mcp1c.registry import Registry
from test_dashboard_admin_api import _client, _login


def _export(root, version):
    root.mkdir()
    return write_export(root, build_configuration(name="AuditDemo", version=version))


def _setup(tmp_path, monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "synthetic-admin")
    registry = Registry(tmp_path / "data")
    original = _export(tmp_path / "first", "1.0")
    first = registry.add_configuration(original)
    registry.add_configuration(_export(tmp_path / "second", "2.0"))
    registry.save()
    client = _client(registry)
    _login(client, "synthetic-admin")
    return registry, client, original, first.stored_path


def _forget(client, path):
    return client.post(
        "/api/v1/sources/forget", json={"path": path, "confirmation": path}
    )


def test_forget_отклоняет_повторно_активированный_orphan(tmp_path, monkeypatch):
    registry, client, original, stored = _setup(tmp_path, monkeypatch)
    stale = registry.orphan_sources()
    assert registry.data_dir / stored in [path for path, _ in stale]
    registry.add_configuration(original)
    registry.save()
    # Даже устаревший список не является разрешением удалить файл.
    monkeypatch.setattr(registry, "orphan_sources", lambda: stale)
    response = _forget(client, stored)
    assert response.status_code == 409
    assert (registry.data_dir / stored).exists()
    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert restarted.resolve("AuditDemo").configuration.config.version == "1.0"


@pytest.mark.parametrize("fails", [False, True])
def test_forget_защищает_source_до_публикации_и_освобождает_после_ошибки(
    tmp_path, monkeypatch, fails,
):
    registry, client, original, stored = _setup(tmp_path, monkeypatch)
    real_index = registry._configuration_index
    entered, release = Event(), Event()

    def paused(config, source, **kwargs):
        if config.version == "1.0":
            entered.set()
            assert release.wait(10)
            if fails:
                raise ValueError("синтетический отказ индекса")
        return real_index(config, source, **kwargs)

    monkeypatch.setattr(registry, "_configuration_index", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(registry.add_configuration, original)
        try:
            assert entered.wait(10)
            response = _forget(client, stored)
            assert response.status_code == 409
            assert registry.data_dir / stored not in [p for p, _ in registry.orphan_sources()]
            assert (registry.data_dir / stored).exists()
            assert registry.resolve("AuditDemo").configuration.config.version == "2.0"
        finally:
            release.set()
        if fails:
            with pytest.raises(ValueError, match="синтетический отказ"):
                pending.result(timeout=10)
        else:
            pending.result(timeout=10)
    if fails:
        assert _forget(client, stored).status_code == 200
    else:
        registry.save()
        restarted = Registry(registry.data_dir)
        assert restarted.restore() == []
        assert restarted.resolve("AuditDemo").configuration.config.version == "1.0"
        assert _forget(client, stored).status_code == 409
    assert original.exists()


def test_forget_не_удаляет_временный_файл_во_время_копирования(tmp_path, monkeypatch):
    registry, client, original, _stored = _setup(tmp_path, monkeypatch)
    entered, release = Event(), Event()
    real_open = Path.open

    def paused_open(path, *args, **kwargs):
        if path == original and args == ("rb",):
            # Первый open хеширует вход, второй копирует его в managed temp.
            temps = list(registry.sources_dir.rglob("*.tmp"))
            if temps:
                entered.set()
                assert release.wait(10)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", paused_open)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(registry.add_configuration, original)
        try:
            assert entered.wait(10)
            temporary = next(registry.sources_dir.rglob("*.tmp"))
            assert temporary not in [p for p, _ in registry.orphan_sources()]
            assert _forget(client, temporary.relative_to(registry.data_dir).as_posix()).status_code == 409
        finally:
            release.set()
        pending.result(timeout=10)


@pytest.mark.parametrize("target", ["../outside.txt", "sources/link/outside.txt", "sources/alias.txt"])
def test_forget_не_следует_по_ссылкам_и_не_выходит_из_sources(
    tmp_path, monkeypatch, target,
):
    registry, client, _original, _stored = _setup(tmp_path, monkeypatch)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep")
    (registry.sources_dir / "link").symlink_to(tmp_path, target_is_directory=True)
    (registry.sources_dir / "alias.txt").symlink_to(outside)
    assert _forget(client, target).status_code in (400, 404)
    assert outside.read_bytes() == b"keep"


def test_forget_защищает_читаемый_вход_при_keep_source_false(tmp_path, monkeypatch):
    registry, client, _original, stored = _setup(tmp_path, monkeypatch)
    managed_input = registry.data_dir / stored
    entered, release = Event(), Event()

    def failing_load(path, **kwargs):
        assert path == managed_input
        entered.set()
        assert release.wait(10)
        raise ValueError("синтетический отказ чтения")

    monkeypatch.setattr("mcp1c.registry.load", failing_load)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(registry.add_configuration, managed_input, keep_source=False)
        try:
            assert entered.wait(10)
            assert _forget(client, stored).status_code == 409
        finally:
            release.set()
        with pytest.raises(ValueError, match="отказ чтения"):
            pending.result(timeout=10)
    assert _forget(client, stored).status_code == 200


def test_завершение_одной_загрузки_не_снимает_резерв_другой(tmp_path, monkeypatch):
    registry, client, original, stored = _setup(tmp_path, monkeypatch)
    first_entered, second_entered, release = Event(), Event(), Event()
    real_index = registry._configuration_index

    def overlap(config, source, **kwargs):
        if not first_entered.is_set():
            first_entered.set()
            assert second_entered.wait(10)
            raise ValueError("отказ первого участника")
        second_entered.set()
        assert release.wait(10)
        return real_index(config, source, **kwargs)

    monkeypatch.setattr(registry, "_configuration_index", overlap)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(registry.add_configuration, original)
        second = None
        try:
            assert first_entered.wait(10)
            second = pool.submit(registry.add_configuration, original)
            assert second_entered.wait(10)
            with pytest.raises(ValueError, match="первого участника"):
                first.result(timeout=10)
            assert _forget(client, stored).status_code == 409
        finally:
            release.set()
        second.result(timeout=10)
    assert (registry.data_dir / stored).exists()


def test_занятый_source_не_мешает_удалить_другой_orphan(tmp_path, monkeypatch):
    registry, client, original, stored = _setup(tmp_path, monkeypatch)
    other = registry.sources_dir / "other.txt"
    other.write_bytes(b"synthetic orphan")
    entered, release = Event(), Event()
    real_index = registry._configuration_index

    def paused(config, source, **kwargs):
        entered.set()
        assert release.wait(10)
        return real_index(config, source, **kwargs)

    monkeypatch.setattr(registry, "_configuration_index", paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(registry.add_configuration, original)
        try:
            assert entered.wait(10)
            assert _forget(client, "sources/other.txt").status_code == 200
            assert _forget(client, stored).status_code == 409
            assert not other.exists()
        finally:
            release.set()
        pending.result(timeout=10)
