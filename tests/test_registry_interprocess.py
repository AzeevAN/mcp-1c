"""Два процесса над одним data/: работающий сервер и CLI.

Сервер имитирует дочерний процесс, который поднял Registry и держит
каталог данных. Данные синтетические, в ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from conftest import build_configuration, write_export
from mcp1c.registry import (
    Registry,
    RegistryChangedExternally,
    RegistryOwnedElsewhere,
)
from test_intake_v2_registry import _manifest

ROOT = Path(__file__).parents[1]
fcntl = pytest.importorskip("fcntl")

_SERVER = """
import sys
from mcp1c.registry import Registry
registry = Registry(sys.argv[1])
registry.startup()
print("ready", flush=True)
sys.stdin.readline()
"""


def _start_server(data: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    process = subprocess.Popen(
        [sys.executable, "-c", _SERVER, str(data)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdout.readline().strip() == "ready"
    return process


def _stop(process: subprocess.Popen) -> None:
    try:
        process.stdin.write("\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    try:
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_cli_при_живом_сервере_не_откатывает_его_публикацию(tmp_path, capsys):
    from mcp1c.cli import main

    first, first_payloads = _manifest(tmp_path, "generation-001")
    second, second_payloads = _manifest(tmp_path, "generation-002", suffix="-2")
    setup = Registry(tmp_path / "data")
    setup.publish_generation(setup.stage_generation(first, first_payloads))
    data = setup.data_dir

    server = _start_server(data)
    try:
        # Публикация сервера «в середине»: staging и WAL уже на диске.
        staged = setup.stage_generation(second, second_payloads)
        old_pointer = setup.active_generation_pointer(first.identity)
        setup.generation_recovery_path.write_text(
            json.dumps(
                {
                    "previous": old_pointer.to_dict(),
                    "staged": staged.pointer.to_dict(),
                    "phase": "prepared",
                }
            ),
            encoding="utf-8",
        )
        wal_before = setup.generation_recovery_path.read_bytes()
        incoming = tmp_path / "input"
        incoming.mkdir()
        archive = write_export(incoming, build_configuration(name="Другая"))

        assert main(["reg-add", str(archive), "--data", str(data)]) == 0

        assert "POST /admin/reload" in capsys.readouterr().err
        assert setup.generation_recovery_path.read_bytes() == wal_before
        assert staged.root.exists()
        payload = json.loads((data / "registry.json").read_text(encoding="utf-8"))
        assert "Другая" in {raw["id"] for raw in payload["sources"]}
        assert payload["generation_manifests"] == [old_pointer.to_dict()]
    finally:
        _stop(server)


def test_гость_не_публикует_поколение_и_не_снимает_источники(tmp_path):
    first, first_payloads = _manifest(tmp_path, "generation-001")
    setup = Registry(tmp_path / "data")
    data = setup.data_dir
    server = _start_server(data)
    try:
        guest = Registry(data)
        guest.startup()
        with pytest.raises(RegistryOwnedElsewhere, match="работающий сервер"):
            guest.publish_generation(guest.stage_generation(first, first_payloads))
        assert not guest.generation_recovery_path.exists()
    finally:
        _stop(server)


def test_запись_поверх_чужого_registry_json_отклоняется_до_reload(tmp_path):
    incoming = tmp_path / "input"
    incoming.mkdir()
    server = Registry(tmp_path / "data")
    server.startup()
    server.add_configuration(write_export(incoming, build_configuration(name="Пример")))
    server.save()

    # Второй процесс (CLI) дописал свою строку.
    other = Registry(server.data_dir)
    other.restore()
    other.add_configuration(write_export(incoming, build_configuration(name="Другая")))
    other.save()

    with pytest.raises(RegistryChangedExternally, match="admin/reload"):
        server.save()
    payload = json.loads(server.registry_path.read_text(encoding="utf-8"))
    assert "Другая" in {raw["id"] for raw in payload["sources"]}

    server.startup()
    server.save()
    assert {"Пример", "Другая"} <= set(server.configurations)


def test_параллельная_запись_json_не_теряет_временный_файл(tmp_path):
    registry = Registry(tmp_path / "data")
    target = registry.data_dir / "journal.json"
    ошибки: list[BaseException] = []

    def писать(номер: int) -> None:
        for шаг in range(150):
            try:
                registry._write_json_durably(target, {"writer": номер, "step": шаг})
            except BaseException as error:
                ошибки.append(error)
                return

    потоки = [threading.Thread(target=писать, args=(номер,)) for номер in range(3)]
    for поток in потоки:
        поток.start()
    for поток in потоки:
        поток.join(60)

    assert ошибки == []
    assert json.loads(target.read_text(encoding="utf-8"))["step"] == 149
    assert not list(registry.data_dir.glob("*.tmp"))
