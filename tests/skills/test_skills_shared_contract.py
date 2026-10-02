"""Контракты, общие для самостоятельных skills: vendored-копия лексера и проверка DTD."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FORM_SCRIPT = ROOT / "skills/1c-form-creator/scripts/form_creator.py"
METADATA_SCRIPT = ROOT / "skills/1c-metadata-creator/scripts/metadata_creator.py"
VENDORED_LEX = ROOT / "skills/1c-form-creator/scripts/form_core/bsl_lex.py"
SOURCE_LEX = ROOT / "src/mcp1c/bsl_lex.py"


def test_vendored_bsl_lex_совпадает_с_исходником_кроме_шапки() -> None:
    # Skill самостоятелен и не импортирует mcp1c, поэтому лексер лежит копией.
    # Копия молча расходится с оригиналом при первой же правке лексера.
    vendored = VENDORED_LEX.read_bytes()
    head, _, body = vendored.partition(b"\n")
    assert head.startswith(b"# VENDORED:"), "в копии нет шапки VENDORED"
    assert body == SOURCE_LEX.read_bytes(), (
        "skills/1c-form-creator/scripts/form_core/bsl_lex.py разошёлся с "
        "src/mcp1c/bsl_lex.py: правьте обе копии синхронно"
    )


_CONFIGURATION = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n{head}<MetaDataObject version=\"2.20\"><Configuration><ChildObjects/></Configuration></MetaDataObject>\n"


@pytest.mark.parametrize(
    "head",
    [
        "<!DOCTYPE a>\n",
        "<!doctype a>\n",
        "<! DOCTYPE a>\n",
        "<!ENTITY x \"y\">\n",
        "<! ENTITY x \"y\">\n",
        "<!\tENTITY x \"y\">\n",
    ],
)
@pytest.mark.parametrize("script", [FORM_SCRIPT, METADATA_SCRIPT], ids=["form", "metadata"])
def test_оба_skill_одинаково_отвергают_dtd_и_entity(script: Path, head: str, tmp_path: Path) -> None:
    config = tmp_path / "Configuration.xml"
    config.write_text(_CONFIGURATION.format(head=head), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script), "inspect", "--config", str(config)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert json.loads(result.stderr)["error"] == "Configuration.xml: DTD/ENTITY запрещены"


def test_инструкции_validate_называют_команды_cli_а_не_mcp_инструменты(tmp_path: Path) -> None:
    # После переноса авторства из MCP в skills агент не имеет инструментов
    # check_*/compile_*; совет вызвать их невыполним.
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    result = subprocess.run(
        [
            sys.executable, str(METADATA_SCRIPT), "validate",
            "--bundle", str(bundle), "--object-ref", "Справочник.Тест", "--format-version", "2.20",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    report = json.loads(result.stdout or result.stderr)
    text = json.dumps(report, ensure_ascii=False)
    assert report["status"] == "failed"
    assert "metadata_creator.py validate" in text
    for stale in ("check_metadata_artifacts", "check_managed_form", "compile_managed_form", "get_managed_form_rules"):
        assert stale not in text


def test_form_core_не_импортирует_отсутствующий_tools() -> None:
    import form_core

    assert not hasattr(form_core, "load")


# --- общие помощники для проверок metadata_core / form_core ---

def _data_processor_specification() -> dict[str, object]:
    from form_core.compiler import compile_managed_form

    owner = "Обработка.Импорт"
    form = compile_managed_form({
        "schema_version": 2,
        "context": {"owner": owner, "role": "object"},
        "form_name": "Форма",
        "format_version": "2.20",
        "title": {"ru": "Импорт"},
        "attributes": [
            {"name": "Объект", "type": {"kind": "metadata_object", "object": owner}, "main": True},
            {"name": "Параметр", "type": {"kind": "string", "length": 100}},
        ],
        "elements": [{"kind": "input_field", "name": "Параметр", "data_path": "Параметр"}],
        "commands": [],
        "events": [],
    })
    return {
        "schema_version": 1,
        "object_ref": owner,
        "format_version": "2.20",
        "identity": "74000000-0000-0000-0000-000000000001",
        "synonym": "Импорт",
        "attributes": [],
        "forms": [{
            "name": "Форма",
            "synonym": "Форма",
            "role": "object",
            "default": True,
            "form_xml": form.artifacts[0].content,
            "module_bsl": form.artifacts[1].content,
        }],
    }


def _with_declaration(xml: str, declaration: str) -> str:
    head, sep, tail = xml.partition("?>")
    return head + sep + "\n" + declaration + tail


_DECLARATIONS = ["<!DOCTYPE a>", "<! DOCTYPE a>", "<!doctype a>", "<! ENTITY x \"y\">", "<!\tENTITY x \"y\">"]


@pytest.mark.parametrize("declaration", _DECLARATIONS)
def test_metadata_compiler_отвергает_dtd_и_entity_в_form_xml(declaration: str) -> None:
    from metadata_core.compiler import MetadataAuthoringContractError, compile_metadata_object

    spec = _data_processor_specification()
    spec["forms"][0]["form_xml"] = _with_declaration(spec["forms"][0]["form_xml"], declaration)

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(spec)

    assert caught.value.diagnostics[0]["code"] == "forbidden_xml_declaration"


@pytest.mark.parametrize("declaration", _DECLARATIONS)
def test_metadata_checker_отвергает_dtd_и_entity_в_артефактах(declaration: str) -> None:
    from metadata_core.checker import check_metadata_artifacts
    from metadata_core.compiler import compile_metadata_object

    compiled = compile_metadata_object(_data_processor_specification())
    artifacts = [dict(item) for item in compiled["artifacts"]]
    form = next(item for item in artifacts if item["path"].endswith("Form.xml"))
    form["content"] = _with_declaration(form["content"], declaration)

    checked = check_metadata_artifacts(compiled["object_ref"], compiled["format_version"], artifacts)

    assert checked["status"] == "failed"
    assert "forbidden_xml_declaration" in {item["code"] for item in checked["diagnostics"]}


@pytest.mark.parametrize(
    ("declaration", "code"),
    [
        ("<!DOCTYPE a>", "doctype_forbidden"),
        ("<! DOCTYPE a>", "doctype_forbidden"),
        ("<!doctype a>", "doctype_forbidden"),
        ("<! ENTITY x \"y\">", "entity_forbidden"),
        ("<!\tENTITY x \"y\">", "entity_forbidden"),
    ],
)
def test_form_decompiler_отвергает_dtd_и_entity(declaration: str, code: str) -> None:
    from form_core.decompiler import decompile_managed_form

    form_xml = _data_processor_specification()["forms"][0]["form_xml"]

    result = decompile_managed_form(
        _with_declaration(form_xml, declaration),
        form_name="Форма",
        context={"owner": "Обработка.Импорт", "role": "object"},
    )

    assert code in {item.code for item in result.diagnostics}


# --- тексты инструкций ссылаются на команды CLI ---

_STALE_TOOLS = ("check_metadata_artifacts", "check_managed_form", "compile_managed_form", "get_managed_form_rules")


def test_validate_passed_называет_команду_form_creator() -> None:
    from metadata_core.checker import check_metadata_artifacts
    from metadata_core.compiler import compile_metadata_object

    compiled = compile_metadata_object(_data_processor_specification())
    checked = check_metadata_artifacts(compiled["object_ref"], compiled["format_version"], compiled["artifacts"])

    assert checked["status"] == "passed"
    text = " ".join(checked["instructions"])
    assert "form_creator.py validate" in text
    assert not any(name in text for name in _STALE_TOOLS)


def test_неканоничный_form_xml_отсылает_к_form_creator_build() -> None:
    from metadata_core.checker import check_metadata_artifacts
    from metadata_core.compiler import MetadataAuthoringContractError, compile_metadata_object

    spec = _data_processor_specification()
    compiled = compile_metadata_object(spec)
    spec["forms"][0]["form_xml"] += "\r\n"

    with pytest.raises(MetadataAuthoringContractError) as caught:
        compile_metadata_object(spec)
    compiler_text = json.dumps(caught.value.diagnostics, ensure_ascii=False)
    assert "form_creator.py build" in compiler_text
    assert not any(name in compiler_text for name in _STALE_TOOLS)

    artifacts = [dict(item) for item in compiled["artifacts"]]
    form = next(item for item in artifacts if item["path"].endswith("Form.xml"))
    form["content"] += "\r\n"
    checked = check_metadata_artifacts(compiled["object_ref"], compiled["format_version"], artifacts)
    checker_text = json.dumps(checked["diagnostics"], ensure_ascii=False)
    assert "form_creator.py build" in checker_text
    assert not any(name in checker_text for name in _STALE_TOOLS)
