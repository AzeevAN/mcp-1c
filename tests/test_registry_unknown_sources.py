"""Неизвестные строки реестра сохраняются, не мешая рабочим источникам."""

import json

from conftest import build_configuration, write_export
from mcp1c.registry import STATUS_READY, Registry, Source


def _добавить_неизвестный_источник(registry, source_id):
    row = Source(
        id=source_id,
        kind="unknown-source-kind",
        origin="unknown.json",
        sha256="a" * 64,
        loaded_at="2026-10-23T00:00:00+00:00",
        status=STATUS_READY,
        stored_path="sources/missing.json",
    ).to_dict()
    # Неизвестные дополнительные поля тоже нельзя терять при save().
    row["unknown_metadata"] = {"preserve": [1, 2, 3]}
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    payload["sources"].append(row)
    registry.registry_path.write_text(json.dumps(payload), encoding="utf-8")
    return row


def test_неизвестный_источник_не_мешает_старту_и_сохраняется(tmp_path):
    incoming = tmp_path / "input"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(incoming, build_configuration(name="Розница")))
    registry.save()
    row = _добавить_неизвестный_источник(registry, "UnknownSource")

    for _ in range(2):
        restored = Registry(registry.data_dir)
        messages = restored.startup()
        assert [message for message in messages if message.startswith("UnknownSource: ")] == [
            "UnknownSource: неизвестный вид источника `unknown-source-kind`; "
            "запись сохранена в registry.json."
        ]
        assert restored.resolve("Розница").name == "Розница"
        assert "UnknownSource" not in restored.snapshot().sources
        payload = json.loads(restored.registry_path.read_text(encoding="utf-8"))
        assert next(item for item in payload["sources"] if item["id"] == "UnknownSource") == row


def test_неизвестная_строка_не_удаляет_журнал_native_корпуса(tmp_path, корень_кода):
    from mcp1c import coverage_log, source_ids
    from native_export import опубликовать_код

    work = tmp_path / "work"
    registry = Registry(work / "data")
    опубликовать_код(
        registry, корень_кода,
        configuration=build_configuration(name="Розница"), рабочий=work,
    )
    registry.save()
    source_id = source_ids.modules_id("Розница")
    row = _добавить_неизвестный_источник(registry, source_id)
    journal = coverage_log.log_path(registry.data_dir, source_id)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{}", encoding="utf-8")

    restored = Registry(registry.data_dir)
    messages = restored.restore()
    restored.save()

    assert source_id in restored.modules
    assert any("неизвестный вид источника" in message for message in messages)
    # Восстановление вправе обновить отчёт корпуса; неизвестная строка
    # с тем же id не должна удалить его после публикации runtime.
    assert journal.is_file()
    payload = json.loads(restored.registry_path.read_text(encoding="utf-8"))
    assert next(item for item in payload["sources"] if item["id"] == source_id) == row


def test_cli_reg_add_не_принимает_json_снимка(tmp_path, capsys):
    from mcp1c.cli import main

    снимок = tmp_path / "снимок.json"
    снимок.write_text('{"extensions": []}', encoding="utf-8")
    data = tmp_path / "data"

    code = main(["reg-add", str(снимок), "--data", str(data)])

    captured = capsys.readouterr()
    assert code != 0
    assert "добавлено" not in captured.out
    assert captured.err.strip()
    registry_json = data / "registry.json"
    if registry_json.exists():
        assert ":extension-runtime" not in registry_json.read_text(encoding="utf-8")
    assert not list((data / "sources").glob("*.json"))

