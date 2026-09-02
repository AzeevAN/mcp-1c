"""Read-only замер размеров RLS в source B без раскрытия имён и текста.

Запуск из корня проекта:

    .venv/bin/python tools/lab/measure_role_restrictions.py /path/to/export.zip

Архив читается напрямую через ``zipfile`` и нигде не извлекается. P95 —
nearest-rank по числу байт UTF-8 во всех узлах ``condition``: как в
``restrictionByCondition``, так и в ``restrictionTemplate``.
"""

from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
import zipfile
from pathlib import PurePosixPath


SCHEMA = "role-restriction-sizes-v1"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _is_role_rights(path: str) -> bool:
    parts = PurePosixPath(path.replace("\\", "/")).parts
    return (
        len(parts) == 4
        and parts[0] == "Roles"
        and parts[2:] == ("Ext", "Rights.xml")
    )


def _measure(archive_path: str) -> dict[str, object]:
    sizes: list[int] = []
    rights_files = 0
    restrictions = 0
    restriction_templates = 0
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
                sizes.append(len("".join(condition.itertext()).encode("utf-8")))
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
