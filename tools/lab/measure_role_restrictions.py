"""Read-only замер размеров RLS в source B без раскрытия имён и текста.

Запуск из корня проекта:

    .venv/bin/python tools/lab/measure_role_restrictions.py /path/to/export.zip

Архив читается напрямую через ``zipfile`` и нигде не извлекается. Tree- и
flat-раскладки с одной общей директорией распознаются одинаково. P95 —
nearest-rank по числу байт UTF-8 во всех узлах ``condition``: как в
``restrictionByCondition``, так и в ``restrictionTemplate``. Для inline-RLS
отдельно считаются пустые условия и все прямые дочерние ``field``.
"""

from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
import zipfile
from pathlib import PurePosixPath


SCHEMA = "role-restriction-sizes-v2"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _is_role_rights(path: str) -> bool:
    parts = PurePosixPath(path.replace("\\", "/")).parts
    tree = (
        len(parts) in {4, 5}
        and parts[-4] == "Roles"
        and parts[-2:] == ("Ext", "Rights.xml")
    )
    name = parts[-1] if parts else ""
    flat = (
        len(parts) in {1, 2}
        and name.startswith("Role.")
        and name.endswith(".Rights.xml")
        and len(name) > len("Role..Rights.xml")
    )
    return tree or flat


def _measure(archive_path: str) -> dict[str, object]:
    sizes: list[int] = []
    rights_files = 0
    restrictions = 0
    restriction_templates = 0
    empty_restriction_conditions = 0
    restriction_fields_total = 0
    maximum_fields = 0
    multiple_fields = 0
    with zipfile.ZipFile(archive_path) as archive:
        members = sorted(
            (item for item in archive.infolist() if _is_role_rights(item.filename)),
            key=lambda item: item.filename,
        )
        if not members:
            raise ValueError("role Rights.xml не найдены")
        for member in members:
            rights_files += 1
            with archive.open(member) as source:
                root = ET.parse(source).getroot()
            for item in root.iter():
                kind = _local_name(item.tag)
                if kind not in {"restrictionByCondition", "restrictionTemplate"}:
                    continue
                if kind == "restrictionByCondition":
                    restrictions += 1
                    fields = sum(
                        _local_name(child.tag) == "field"
                        for child in item
                    )
                    restriction_fields_total += fields
                    maximum_fields = max(maximum_fields, fields)
                    multiple_fields += fields > 1
                else:
                    restriction_templates += 1
                condition = next(
                    (
                        child
                        for child in item
                        if _local_name(child.tag) == "condition"
                    ),
                    None,
                )
                if condition is None:
                    raise ValueError("ограничение не содержит condition")
                size = len("".join(condition.itertext()).encode("utf-8"))
                sizes.append(size)
                if kind == "restrictionByCondition" and size == 0:
                    empty_restriction_conditions += 1
    if not sizes:
        raise ValueError("condition ограничений не найдены")
    sizes.sort()
    p95_index = math.ceil(0.95 * len(sizes)) - 1
    return {
        "schema": SCHEMA,
        "completed": True,
        "rights_files": rights_files,
        "restrictions": restrictions,
        "restriction_templates": restriction_templates,
        "conditions": len(sizes),
        "empty_restriction_conditions": empty_restriction_conditions,
        "restriction_fields": {
            "total": restriction_fields_total,
            "maximum_per_restriction": maximum_fields,
            "multiple_fields": multiple_fields,
        },
        "condition_utf8_bytes": {
            "maximum": sizes[-1],
            "p95_nearest_rank": sizes[p95_index],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Обезличенный read-only замер размеров RLS в source B",
    )
    parser.add_argument("archive", help="путь к ZIP-выгрузке source B")
    args = parser.parse_args()
    try:
        payload = _measure(args.archive)
        status = 0
    except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile):
        payload = {
            "schema": SCHEMA,
            "completed": False,
            "error": "замер не завершён",
        }
        status = 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
