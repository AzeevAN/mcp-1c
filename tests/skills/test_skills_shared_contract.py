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
