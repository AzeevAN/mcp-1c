"""Проверка локальной сборки комплектов управляемых форм."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills/1c-form-creator/scripts/form_creator.py"


def _call(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        text=True,
        capture_output=True,
        check=False,
    )


def _spec() -> dict[str, object]:
    owner = "Обработка.Импорт"
    return {
        "schema_version": 2,
        "context": {"owner": owner, "role": "object"},
        "form_name": "Форма",
        "format_version": "2.20",
        "title": {"ru": "Импорт Excel"},
        "attributes": [
            {"name": "Объект", "type": {"kind": "metadata_object", "object": owner}, "main": True},
            {
                "name": "ТаблицаДанных",
                "type": {"kind": "value_table", "columns": [
                    {"name": "Артикул", "type": {"kind": "string", "length": 0}},
                    {"name": "Цена", "type": {"kind": "string", "length": 0}},
                ]},
            },
        ],
        "elements": [
            {"kind": "button", "name": "ЗагрузитьКнопка", "command": "Загрузить"},
            {"kind": "table", "name": "ТаблицаДанныхПоле", "data_path": "ТаблицаДанных", "columns": [
                {"kind": "input_field", "name": "ПолеАртикул", "data_path": "ТаблицаДанных.Артикул"},
                {"kind": "input_field", "name": "ПолеЦена", "data_path": "ТаблицаДанных.Цена"},
            ]},
        ],
        "commands": [{"name": "Загрузить", "title": {"ru": "Загрузить Excel"}, "action": "Загрузить"}],
        "events": [],
    }


def test_build_and_validate_embedded_data_processor(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    source_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<MetaDataObject version="2.20"><Configuration><ChildObjects>\n'
        '\t<Catalog>Существующий</Catalog>\n'
        '</ChildObjects></Configuration></MetaDataObject>\n'
    )
    (config / "Configuration.xml").write_text(source_xml, encoding="utf-8")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(), ensure_ascii=False), encoding="utf-8")
    module_path = tmp_path / "Module.bsl"
    module_path.write_text('&НаКлиенте\nПроцедура Загрузить(Команда)\n\tСообщить("Старт");\nКонецПроцедуры\n', encoding="utf-8")
    output = tmp_path / "build"

    built = _call("build", "--spec", spec_path, "--module", module_path,
                  "--auto-data-processor", "--config", config, "--output", output)
    assert built.returncode == 0, built.stderr
    assert (config / "Configuration.xml").read_text(encoding="utf-8") == source_xml
    owner_dir = output / "DataProcessors" / "Импорт"
    form_xml = owner_dir / "Forms" / "Форма" / "Ext" / "Form.xml"
    form_bsl = owner_dir / "Forms" / "Форма" / "Ext" / "Form" / "Module.bsl"
    assert form_xml.is_file() and form_bsl.is_file()
    assert form_xml.read_bytes().startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in form_xml.read_bytes()
    assert form_bsl.read_bytes().startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in form_bsl.read_bytes()
    assert (output / "Configuration.xml").read_bytes().startswith(b"\xef\xbb\xbf")
    assert "<DataProcessor>Импорт</DataProcessor>" in (output / "Configuration.xml").read_text(encoding="utf-8-sig")
    report = json.loads((output / "verification.json").read_text(encoding="utf-8"))
    assert report["status"] == "static_passed"
    assert report["acceptance"]["native_import"] == "not_checked"
    assert report["acceptance"]["bsl_api"] == "not_checked"

    checked = _call("validate", "--form-xml", form_xml, "--module", form_bsl,
                    "--owner", "Обработка.Импорт", "--role", "object", "--form-name", "Форма",
                    "--bundle", output, "--format-version", "2.20")
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["status"] == "static_passed"

    extracted_spec = tmp_path / "extracted.json"
    extracted = _call("extract", "--form-xml", form_xml, "--module", form_bsl,
                      "--owner", "Обработка.Импорт", "--role", "object", "--form-name", "Форма",
                      "--output", extracted_spec)
    assert extracted.returncode == 0, extracted.stderr
    assert json.loads(extracted_spec.read_text(encoding="utf-8"))["form_name"] == "Форма"


def test_build_refuses_version_mismatch_without_writing(tmp_path: Path) -> None:
    config = tmp_path / "Configuration.xml"
    config.write_text('<MetaDataObject version="2.16"><Configuration><ChildObjects/></Configuration></MetaDataObject>', encoding="utf-8")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(), ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "build"
    result = _call("build", "--spec", spec_path, "--auto-data-processor",
                   "--config", config, "--output", output)
    assert result.returncode == 2
    assert "не совпадает" in result.stderr
    assert not output.exists()


def test_build_refuses_existing_registration_without_partial_bundle(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "Configuration.xml").write_text(
        '<MetaDataObject version="2.20"><Configuration><ChildObjects>'
        '<DataProcessor>Импорт</DataProcessor></ChildObjects></Configuration></MetaDataObject>',
        encoding="utf-8",
    )
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(), ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "build"
    result = _call("build", "--spec", spec_path, "--auto-data-processor",
                   "--config", config, "--output", output)
    assert result.returncode == 2
    assert "уже регистрирует" in result.stderr
    assert not output.exists()
