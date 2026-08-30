"""Ключевые публичные документы описывают текущее поведение, а не планы."""

from pathlib import Path
import zipfile

import pytest

from mcp1c.loader import ExportError, load


ROOT = Path(__file__).parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_readme_не_выдаёт_локальный_registry_за_состав_установки():
    readme = _read("README.md")
    state = readme.split("## Состояние", 1)[1].split("## Навигация", 1)[0]

    assert "2026-08-30" in state
    assert "| Тесты |" in state
    assert "| Конфигурации |" not in state
    assert "group_by(.kind)" not in state
    assert "5 конфигураций, 20 522 объекта" not in state


def test_schema_описывает_рабочие_предопределённые_на_8_3_5():
    schema = _read("docs/schema-v1.md")
    exporter = _read("exporter-1c/src/core.bsl")

    assert "предопределённые на 8.3.5 и 8.3.23" in schema.lower()
    assert "`ОбъектМетаданных.ПолучитьИменаПредопределенных()`" in schema
    assert "`ИмяПредопределенныхДанных`" in schema
    assert "перечислить их из встроенного языка нельзя" not in schema
    assert "МетаОбъект.ПолучитьИменаПредопределенных()" in exporter
    assert "ВЫБРАТЬ ИмяПредопределенныхДанных КАК Имя" in exporter


def test_legacy_архив_документирован_как_явный_отказ_с_перевыгрузкой(tmp_path):
    archive = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("objects.csv", "full_name,type\n")

    with pytest.raises(ExportError, match="перевыгрузите"):
        load(archive)

    schema = _read("docs/schema-v1.md")
    legacy = schema.split("### Legacy-формат", 1)[1]
    assert "текущим загрузчиком не\nподдерживается" in legacy
    assert "требует перевыгрузки" in legacy
    assert "read-only" not in legacy.lower()


def test_публичные_документы_не_обещают_общую_schema_v2():
    for relative in ("docs/schema-v1.md", "docs/data-sources.md"):
        text = _read(relative)
        assert "Общая `schema_version 2` не запланирована" in text
        assert "отложены до следующей версии схемы" not in text
        assert "Отложено до schema_version 2" not in text


def test_data_sources_фиксирует_завершённый_провайдер_и_проверенные_виды():
    text = _read("docs/data-sources.md")

    assert "Тексты модулей из источника B | ✓ реализовано" in text
    assert "Планы счетов и регистры бухгалтерии и расчёта | ✓ проверено" in text
    assert "Тексты модулей из источника B | не начато" not in text
    assert "не проверены — таких объектов нет" not in text
    assert "tests/test_registry_modules_provider.py" in text
    assert "python -m mcp1c.cli stats" in text
