"""Отдельный снимок фактического состояния расширений в сеансе 1С."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import build_configuration, write_export
from mcp1c import tools
from mcp1c.cli import main
from mcp1c.dashboard_runtime import _sources_payload
from mcp1c.extension_runtime import ExtensionRuntimeError, load_extension_runtime
from mcp1c.registry import KIND_EXTENSION_RUNTIME, Registry, RegistryError


PROJECT_ROOT = Path(__file__).parents[1]


def _snapshot(
    *,
    configuration: str = "ТестоваяКонфигурация",
    version: str = "1.0",
    changed: bool | None = False,
) -> dict:
    def extension(number: int, name: str, *, enabled: bool = True) -> dict:
        return {
            "uuid": f"00000000-0000-0000-0000-{number:012d}",
            "name": name,
            "synonym": f"Синоним {number}",
            "version": f"1.0.{number}",
            "purpose": "Дополнение",
            "scope": "ИнформационнаяБаза",
            "enabled": enabled,
        }

    first = extension(1, "ПервоеРасширение")
    second = extension(2, "ВтороеРасширение", enabled=False)
    third = extension(3, "ТретьеРасширение")
    return {
        "format": "mcp1c-extension-runtime",
        "schema_version": 1,
        "snapshot_id": "10000000-0000-0000-0000-000000000001",
        "captured_at": "2026-08-28T10:11:12Z",
        "scope": "current_session_current_data_area",
        "configuration": {
            "name": configuration,
            "version": version,
            "platform": "8.3.23.1997",
        },
        "database_changed_since_session_start": changed,
        "database": [first, second, third],
        # Порядок намеренно не совпадает ни с именами, ни с database.
        "session_active": [third, first],
        "session_disabled": [second],
    }


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _registry(tmp_path: Path) -> Registry:
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry.add_configuration(write_export(incoming, build_configuration()))
    return registry


def test_загрузчик_сохраняет_порядок_ответа_платформы(tmp_path):
    loaded = load_extension_runtime(_write(tmp_path / "runtime.json", _snapshot()))

    assert loaded.configuration.name == "ТестоваяКонфигурация"
    assert [item.name for item in loaded.session_active] == [
        "ТретьеРасширение",
        "ПервоеРасширение",
    ]
    assert [item.session_position for item in loaded.session_active] == [1, 2]
    assert loaded.by_uuid[loaded.session_active[0].uuid].active_in_session is True
    assert loaded.by_uuid[loaded.session_disabled[0].uuid].active_in_session is False


def test_загрузчик_отвергает_одно_расширение_сразу_в_двух_состояниях(tmp_path):
    payload = _snapshot()
    payload["session_disabled"] = [payload["session_active"][0]]

    with pytest.raises(ExtensionRuntimeError, match="одновременно"):
        load_extension_runtime(_write(tmp_path / "runtime.json", payload))


def test_runtime_источник_привязан_к_загруженной_конфигурации(tmp_path):
    registry = Registry(tmp_path / "data")
    path = _write(tmp_path / "runtime.json", _snapshot())

    with pytest.raises(RegistryError, match="Конфигурация не загружена"):
        registry.add_extension_runtime(path)


def test_runtime_источник_независим_и_переживает_restore(tmp_path):
    registry = _registry(tmp_path)
    source = registry.add_extension_runtime(
        _write(tmp_path / "runtime.json", _snapshot())
    )
    registry.save()

    assert source.id == "ТестоваяКонфигурация:extension-runtime"
    assert source.kind == KIND_EXTENSION_RUNTIME
    assert source.items_total == 3
    assert source.stored_path.endswith(".json")

    restored = Registry(tmp_path / "data")
    assert restored.restore() == []
    assert restored.extension_runtime["ТестоваяКонфигурация"].snapshot.snapshot_id == (
        "10000000-0000-0000-0000-000000000001"
    )


def test_cli_reg_add_распознаёт_runtime_json(tmp_path, capsys):
    registry = _registry(tmp_path)
    registry.save()
    path = _write(tmp_path / "runtime.json", _snapshot())

    code = main(
        ["reg-add", str(path), "--data", str(tmp_path / "data")]
    )

    assert code == 0
    assert "ТестоваяКонфигурация:extension-runtime" in capsys.readouterr().out
    restored = Registry(tmp_path / "data")
    assert restored.restore() == []
    assert "ТестоваяКонфигурация" in restored.extension_runtime


def test_list_extensions_без_снимка_говорит_unknown(tmp_path):
    answer = tools.list_extensions(_registry(tmp_path), "ТестоваяКонфигурация")

    assert "Состояние: **unknown**" in answer
    assert "Фактическая активность и порядок неизвестны" in answer


def test_list_extensions_показывает_фактическую_активность_и_позиции(tmp_path):
    registry = _registry(tmp_path)
    registry.add_extension_runtime(
        _write(tmp_path / "runtime.json", _snapshot())
    )

    answer = tools.list_extensions(registry, "ТестоваяКонфигурация")

    assert "Состояние: **snapshot**" in answer
    assert "2026-08-28T10:11:12Z" in answer
    assert "1. `ТретьеРасширение`" in answer
    assert "2. `ПервоеРасширение`" in answer
    assert "Не применены в этом сеансе" in answer
    assert "`ВтороеРасширение`" in answer
    assert "не является доказанным порядком исполнения" in answer


def test_неизвестный_признак_изменения_на_платформе_до_8_3_22_не_лжёт(tmp_path):
    registry = _registry(tmp_path)
    registry.add_extension_runtime(
        _write(tmp_path / "runtime.json", _snapshot(changed=None))
    )

    answer = tools.list_extensions(registry, "ТестоваяКонфигурация")

    assert "Состояние: **snapshot**" in answer
    assert "набор расширений изменён" not in answer
    assert "Признак изменения набора после старта: **unknown**" in answer


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (_snapshot(version="2.0"), "версия конфигурации"),
        (_snapshot(changed=True), "набор расширений изменён"),
    ],
)
def test_list_extensions_помечает_снимок_stale(tmp_path, payload, reason):
    registry = _registry(tmp_path)
    registry.add_extension_runtime(_write(tmp_path / "runtime.json", payload))

    answer = tools.list_extensions(registry, "ТестоваяКонфигурация")

    assert "Состояние: **stale**" in answer
    assert reason in answer


def test_удаление_конфигурации_каскадно_снимает_runtime_источник(tmp_path):
    registry = _registry(tmp_path)
    source = registry.add_extension_runtime(
        _write(tmp_path / "runtime.json", _snapshot())
    )

    registry.remove("ТестоваяКонфигурация")

    assert source.id not in registry.sources
    assert "ТестоваяКонфигурация" not in registry.extension_runtime
    # Как и остальные малые исходники, файл остаётся recoverable orphan до
    # отдельного действия «забыть файл» в управлении источниками.
    assert (registry.data_dir / source.stored_path).exists()


def test_сборка_публикует_отдельный_bsl_и_не_меняет_модули_epf():
    source = (
        PROJECT_ROOT
        / "exporter-1c/src/extension_runtime_managed_json.bsl"
    ).read_bytes()
    built = (
        PROJECT_ROOT
        / "exporter-1c/dist/СнимокРасширений_УправляемаяФорма_JSON.bsl"
    ).read_bytes()

    assert built == source
    text = built.decode("utf-8")
    for api_value in ("БазаДанных", "СеансАктивные", "СеансОтключенные"):
        assert f"ИсточникРасширенийКонфигурации.{api_value}" in text
    assert "mcp1c-extension-runtime" in text
    assert "неизвестность нельзя подменять значением Ложь" in text

    # Два существующих EPF связаны с прежними модулями структуры. Новый код
    # не встраивается в их core и потому не требует недоступной здесь ручной
    # пересборки конфигуратором.
    for path in (PROJECT_ROOT / "exporter-1c/dist").glob("*Форма_*.bsl"):
        if path.name.startswith("СнимокРасширений_"):
            continue
        assert "mcp1c-extension-runtime" not in path.read_text(encoding="utf-8")
