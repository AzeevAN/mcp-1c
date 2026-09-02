"""Обезличенный read-only замер размеров RLS в source B."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/lab/measure_role_restrictions.py"
RIGHTS_NS = "http://v8.1c.ru/8.2/roles"


def _rights(
    conditions: list[str | tuple[str, tuple[str, ...]]],
    *,
    templates: list[str] | None = None,
) -> bytes:
    restrictions = "".join(
        "<restrictionByCondition>"
        + "".join(f"<field>{field}</field>" for field in fields)
        + "<condition>"
        f"{condition}"
        "</condition></restrictionByCondition>"
        for condition, fields in (
            item if isinstance(item, tuple) else (item, ())
            for item in conditions
        )
    )
    template_xml = "".join(
        "<restrictionTemplate><name>Synthetic</name>"
        f"<condition>{condition}</condition></restrictionTemplate>"
        for condition in templates or []
    )
    return (
        f'<Rights xmlns="{RIGHTS_NS}" version="2.20">'
        "<setForNewObjects>false</setForNewObjects>"
        "<setForAttributesByDefault>false</setForAttributesByDefault>"
        "<independentRightsOfChildObjects>false</independentRightsOfChildObjects>"
        "<object><name>Catalog.Synthetic</name>"
        f"<right><name>Read</name><value>true</value>{restrictions}</right>"
        f"{template_xml}"
        "</object></Rights>"
    ).encode("utf-8")


def test_measure_role_restrictions_считает_nearest_rank_и_не_печатает_путь(
    tmp_path,
):
    archive = tmp_path / "private-acceptance-name.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "Roles/First/Ext/Rights.xml",
            _rights(["x" * length for length in range(1, 11)]),
        )
        output.writestr(
            "Roles/Second/Ext/Rights.xml",
            _rights(
                ["x" * length for length in range(11, 21)],
                templates=["x" * 21],
            ),
        )
        output.writestr("Other/Rights.xml", _rights(["secret"]))

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(archive)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "schema": "role-restriction-sizes-v2",
        "completed": True,
        "rights_files": 2,
        "restrictions": 20,
        "restriction_templates": 1,
        "conditions": 21,
        "empty_restriction_conditions": 0,
        "restriction_fields": {
            "total": 0,
            "maximum_per_restriction": 0,
            "multiple_fields": 0,
        },
        "condition_utf8_bytes": {
            "maximum": 21,
            "p95_nearest_rank": 20,
        },
    }
    assert archive.name not in result.stdout
    assert str(archive) not in result.stdout
    assert result.stderr == ""


def test_measure_role_restrictions_принимает_flat_wrapper_и_считает_fields(
    tmp_path,
):
    archive = tmp_path / "flat-private-name.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "wrapper/Role.Flat.Rights.xml",
            _rights(
                [
                    ("", ("Catalog.Synthetic.Attribute.Code", "Catalog.Synthetic.Attribute.Number")),
                    "xx",
                    ("yyy", ("Catalog.Synthetic.Attribute.Owner",)),
                ],
                templates=["zzzz"],
            ),
        )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(archive)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "schema": "role-restriction-sizes-v2",
        "completed": True,
        "rights_files": 1,
        "restrictions": 3,
        "restriction_templates": 1,
        "conditions": 4,
        "empty_restriction_conditions": 1,
        "restriction_fields": {
            "total": 3,
            "maximum_per_restriction": 2,
            "multiple_fields": 1,
        },
        "condition_utf8_bytes": {
            "maximum": 4,
            "p95_nearest_rank": 4,
        },
    }
    assert archive.name not in result.stdout
    assert str(archive) not in result.stdout
    assert result.stderr == ""


def test_measure_role_restrictions_возвращает_bounded_error(tmp_path):
    missing = tmp_path / "missing.zip"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(missing)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "schema": "role-restriction-sizes-v2",
        "completed": False,
        "error": "замер не завершён",
    }
    assert str(missing) not in result.stdout
    assert result.stderr == ""
