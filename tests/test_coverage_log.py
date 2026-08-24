"""Один актуальный JSON-журнал покрытия на независимо удаляемый корпус."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from conftest import build_configuration, write_export
from mcp1c import coverage_log
from mcp1c.registry import KIND_EXTENSION, KIND_MODULES, Registry
from module_samples import v8_container_bytes


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _registry(tmp_path: Path) -> tuple[Registry, Path]:
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Пример"))
    )
    root = tmp_path / "code"
    root.mkdir()
    return registry, root


def _source(registry: Registry, kind: str):
    return next(source for source in registry.sources.values() if source.kind == kind)


def test_json_schema_v1_содержит_identity_таблицы_и_полные_проблемы(
    tmp_path, архив_кода
):
    registry, root = _registry(tmp_path)
    _write(
        root,
        "CommonModules/Полный/Ext/Module.bsl",
        "Процедура Полная()\nКонецПроцедуры",
    )
    _write(
        root,
        "CommonModules/Частичный/Ext/Module.bsl",
        "Процедура Незакрытая()",
    )
    _write(root, "CommonModules/Пустой/Ext/Module.bsl", "")

    source = registry.add_modules(архив_кода(root), configuration="Пример")
    payload = coverage_log.load_current(registry.data_dir, source)

    assert payload is not None
    assert payload["schema_version"] == 1
    assert payload["kind"] == "module_coverage"
    assert payload["source"] == {
        "id": source.id,
        "kind": KIND_MODULES,
        "sha256": source.sha256,
        "loaded_at": source.loaded_at,
        "selection_version": source.selection_version,
        "locator_generation": source.locator_generation,
        "code_version": source.code_version,
    }
    assert payload["identity"] == {
        "source_id": source.id,
        "source_sha256": source.sha256,
        "generation": source.locator_generation,
    }
    assert set(payload["coverage"]) == {
        "modules",
        "procedures",
        "form_structures",
        "form_modules",
        "limitations",
    }
    modules = payload["coverage"]["modules"]
    assert modules["total"] == sum(
        modules[key]
        for key in (
            "source_available",
            "empty",
            "partial",
            "unreadable",
            "conflict",
            "compiled_without_source",
        )
    )
    procedures = payload["coverage"]["procedures"]
    assert procedures == {"total": 2, "full": 1, "partial": 1}
    assert isinstance(payload["problems"], list)
    assert all(set(row) == {"category", "address", "ordinal", "reason", "marker"}
               for row in payload["problems"])
    assert json.loads(coverage_log.log_path(registry.data_dir, source.id).read_text())


def test_у_основного_корпуса_и_расширения_разные_актуальные_журналы(
    tmp_path, архив_кода
):
    registry, root = _registry(tmp_path)
    _write(root, "CommonModules/База/Ext/Module.bsl", "Процедура База()\nКонецПроцедуры")
    base = registry.add_modules(архив_кода(root), configuration="Пример")

    extension_root = tmp_path / "extension"
    extension_root.mkdir()
    _write(
        extension_root,
        "CommonModules/Доп/Ext/Module.bsl",
        "Процедура Доп()\nКонецПроцедуры",
    )
    extension = registry.add_modules(
        архив_кода(extension_root, extension="Доп"), configuration="Пример"
    )

    assert base.kind == KIND_MODULES
    assert extension.kind == KIND_EXTENSION
    assert coverage_log.log_path(registry.data_dir, base.id).is_file()
    assert coverage_log.log_path(registry.data_dir, extension.id).is_file()
    assert coverage_log.log_path(registry.data_dir, base.id) != coverage_log.log_path(
        registry.data_dir, extension.id
    )
    assert len(tuple((registry.data_dir / "logs").glob("*.json"))) == 2


def test_reparse_заменяет_тот_же_файл_новым_поколением(tmp_path, архив_кода):
    registry, root = _registry(tmp_path)
    _write(root, "CommonModules/Первый/Ext/Module.bsl", "Процедура Первый()\nКонецПроцедуры")
    first = registry.add_modules(архив_кода(root), configuration="Пример")
    path = coverage_log.log_path(registry.data_dir, first.id)
    first_payload = json.loads(path.read_text(encoding="utf-8"))

    replacement = tmp_path / "replacement"
    replacement.mkdir()
    _write(
        replacement,
        "CommonModules/Второй/Ext/Module.bsl",
        "Процедура Второй()\nКонецПроцедуры",
    )
    second = registry.add_modules(архив_кода(replacement), configuration="Пример")
    second_payload = json.loads(path.read_text(encoding="utf-8"))

    assert coverage_log.log_path(registry.data_dir, second.id) == path
    assert second_payload["identity"]["generation"] > first_payload["identity"][
        "generation"
    ]
    assert second_payload["identity"]["source_sha256"] == second.sha256
    assert second_payload["identity"] != first_payload["identity"]


def test_remove_удаляет_только_журнал_своего_корпуса(tmp_path, архив_кода):
    registry, root = _registry(tmp_path)
    _write(root, "CommonModules/База/Ext/Module.bsl", "Процедура База()\nКонецПроцедуры")
    base = registry.add_modules(архив_кода(root), configuration="Пример")
    extension_root = tmp_path / "extension"
    extension_root.mkdir()
    _write(extension_root, "CommonModules/Доп/Ext/Module.bsl", "Процедура Доп()\nКонецПроцедуры")
    extension = registry.add_modules(
        архив_кода(extension_root, extension="Доп"), configuration="Пример"
    )
    base_log = coverage_log.log_path(registry.data_dir, base.id)
    extension_log = coverage_log.log_path(registry.data_dir, extension.id)

    registry.remove(extension.id)

    assert base_log.is_file()
    assert not extension_log.exists()
    registry.remove("Пример")
    assert not base_log.exists()


def test_remove_не_оставляет_журнал_при_гонке_с_его_записью(
    tmp_path, архив_кода, monkeypatch
):
    registry, root = _registry(tmp_path)
    _write(root, "CommonModules/База/Ext/Module.bsl", "Процедура База()\nКонецПроцедуры")
    archive = архив_кода(root)
    entered = threading.Event()
    release = threading.Event()
    original_write = coverage_log.write

    def blocked_write(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(coverage_log, "write", blocked_write)
    errors: list[BaseException] = []

    def add() -> None:
        try:
            registry.add_modules(archive, configuration="Пример")
        except BaseException as error:  # pragma: no cover — попадёт в assert ниже
            errors.append(error)

    adding = threading.Thread(target=add)
    adding.start()
    assert entered.wait(timeout=5)

    removing = threading.Thread(
        target=lambda: registry.remove("Пример:modules")
    )
    removing.start()
    release.set()
    adding.join(timeout=5)
    removing.join(timeout=5)

    assert not adding.is_alive()
    assert not removing.is_alive()
    assert errors == []
    assert "Пример:modules" not in registry.sources
    assert not coverage_log.log_path(
        registry.data_dir, "Пример:modules"
    ).exists()


def test_отказ_журнала_не_отклоняет_корпус(tmp_path, архив_кода, monkeypatch):
    registry, root = _registry(tmp_path)
    _write(root, "CommonModules/База/Ext/Module.bsl", "Процедура База()\nКонецПроцедуры")

    def fail(*args, **kwargs):
        raise OSError("/private/secret")

    monkeypatch.setattr(coverage_log, "write", fail)
    source = registry.add_modules(архив_кода(root), configuration="Пример")

    assert source.status == "ready"
    assert any("Журнал покрытия не записан" in warning for warning in source.warnings)
    assert all("/private/secret" not in warning for warning in source.warnings)


def test_symlink_каталога_логов_не_ведёт_запись_наружу(tmp_path, архив_кода):
    registry, root = _registry(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    logs = registry.data_dir / "logs"
    logs.parent.mkdir(parents=True, exist_ok=True)
    logs.symlink_to(outside, target_is_directory=True)
    _write(root, "CommonModules/База/Ext/Module.bsl", "Процедура База()\nКонецПроцедуры")

    source = registry.add_modules(архив_кода(root), configuration="Пример")

    assert source.status == "ready"
    assert not tuple(outside.iterdir())
    assert any("Журнал покрытия не записан" in warning for warning in source.warnings)


def test_warm_restart_пересобирает_битый_журнал_с_полным_списком(
    tmp_path, архив_кода
):
    registry, root = _registry(tmp_path)
    container = v8_container_bytes([("module", b""), ("form", b"{99}")])
    for index in range(25):
        path = root / f"CommonForm.Форма{index:02d}.Form"
        path.write_bytes(container)
    source = registry.add_modules(архив_кода(root), configuration="Пример")
    path = coverage_log.log_path(registry.data_dir, source.id)
    path.write_text('{"schema_version":1}', encoding="utf-8")

    restored = Registry(registry.data_dir)
    assert restored.startup() == []
    assert restored.wait_for_module_builds()
    current = _source(restored, KIND_MODULES)
    payload = coverage_log.load_current(restored.data_dir, current)

    assert payload is not None
    assert payload["coverage"]["limitations"]["problem_rows_total"] == 25
    assert len(payload["problems"]) == 25
    assert {row["marker"] for row in payload["problems"]} == {99}
    assert payload["problems"][0]["address"] == "ОбщаяФорма.Форма00"
    assert payload["problems"][-1]["address"] == "ОбщаяФорма.Форма24"


def test_incomplete_журнал_не_считается_актуальным(tmp_path, архив_кода):
    registry, root = _registry(tmp_path)
    _write(root, "CommonModules/База/Ext/Module.bsl", "Процедура База()\nКонецПроцедуры")
    source = registry.add_modules(архив_кода(root), configuration="Пример")
    path = coverage_log.log_path(registry.data_dir, source.id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("problems")
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert coverage_log.load_current(registry.data_dir, source) is None
