"""Поведение локального CLI без server runtime."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills/1c-metadata-creator/scripts/metadata_creator.py"
FORM_SCRIPTS = ROOT / "skills/1c-form-creator/scripts"


def _run(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *(str(arg) for arg in args)], capture_output=True, text=True)


def _catalog() -> dict[str, object]:
    return {
        "schema_version": 1, "object_ref": "Справочник.Тест", "format_version": "2.20",
        "identity": "40000000-0000-0000-0000-000000000001", "synonym": "Тест",
        "code_length": 9, "description_length": 150, "attributes": [], "forms": [],
    }


def test_build_without_form_and_validate_bundle(tmp_path: Path) -> None:
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(_catalog(), ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "out"
    built = _run("build", "--spec", spec, "--output", output)
    assert built.returncode == 0, built.stderr
    descriptor = (output / "Catalogs/Тест.xml").read_bytes()
    assert descriptor.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in descriptor
    assert b"\n" not in descriptor.replace(b"\r\n", b"")
    assert json.loads((output / "verification.json").read_text())["acceptance"]["native_import"] == "not_checked"
    validated = _run("validate", "--bundle", output, "--object-ref", "Справочник.Тест", "--format-version", "2.20")
    assert validated.returncode == 0, validated.stderr
    assert json.loads(validated.stdout)["status"] == "static_passed"


def test_build_with_exact_form_files(tmp_path: Path) -> None:
    sys.path.insert(0, str(FORM_SCRIPTS))
    from form_core import compile_managed_form

    form_spec = json.loads((ROOT / "skills/1c-form-creator/templates/data-processor-form.json").read_text())
    artifacts = {item.path: item.content for item in compile_managed_form(form_spec).artifacts}
    (tmp_path / "Form.xml").write_text(artifacts["Forms/Форма/Ext/Form.xml"], encoding="utf-8")
    (tmp_path / "Module.bsl").write_text(artifacts["Forms/Форма/Ext/Form/Module.bsl"], encoding="utf-8")
    spec = {
        "schema_version": 1, "object_ref": "Обработка.Импорт", "format_version": "2.20",
        "identity": "40000000-0000-0000-0000-000000000002", "synonym": "Импорт",
        "attributes": [], "forms": [{"name": "Форма", "synonym": "Импорт", "role": "object",
        "default": True, "form_xml_file": "Form.xml", "module_bsl_file": "Module.bsl"}],
    }
    (tmp_path / "spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "out"
    built = _run("build", "--spec", tmp_path / "spec.json", "--output", output)
    assert built.returncode == 0, built.stderr
    assert (output / "DataProcessors/Импорт/Forms/Форма/Ext/Form.xml").read_bytes().decode("utf-8-sig").replace("\r\n", "\n") == artifacts["Forms/Форма/Ext/Form.xml"].replace("\r\n", "\n")
    assert (output / "DataProcessors/Импорт/Forms/Форма/Ext/Form/Module.bsl").is_file()
    assert json.loads((output / "verification.json").read_text())["metadata_check"]["status"] == "passed"


def test_build_rejects_wrong_configuration_version_before_writing(tmp_path: Path) -> None:
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(_catalog(), ensure_ascii=False), encoding="utf-8")
    config = tmp_path / "Configuration.xml"
    config.write_text('<MetaDataObject version="2.19"><Configuration><ChildObjects /></Configuration></MetaDataObject>', encoding="utf-8")
    output = tmp_path / "out"
    built = _run("build", "--spec", spec, "--config", config, "--output", output)
    assert built.returncode == 2
    assert "не совпадает" in built.stderr
    assert not output.exists()
