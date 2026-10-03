"""Замер встроенного BSL во всех ZIP только для чтения, без привязки расширений.

PYTHONPATH=src .venv/bin/python tools/lab/measure_module_containers.py --archives data/incoming
Вывод — только счётчики и размеры; исходники и имена не выводятся.
"""
from __future__ import annotations

import argparse
import json
import zipfile
from collections import Counter
from pathlib import Path

from mcp1c.readers.modules import read_module_body
from mcp1c.resource_limits import V8_LIMITS
from mcp1c.v8container import V8Container, V8ContainerError


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archives", required=True, type=Path)
    args = parser.parse_args()
    counts = Counter()
    maxima = {"container_bytes": 0, "module_entry_bytes": 0, "bsl_utf8_bytes": 0}
    for archive in sorted(args.archives.glob("*.zip")):
        counts["archives"] += 1
        with zipfile.ZipFile(archive) as source:
            for info in source.infolist():
                if info.is_dir() or not info.filename.endswith((".Form", "/Form.bin")):
                    continue
                counts["form_container_members"] += 1
                maxima["container_bytes"] = max(maxima["container_bytes"], info.file_size)
                # Замер не отключает прежний V8-бюджет ради недоверенного ввода.
                if info.file_size > V8_LIMITS.max_entry_bytes:
                    counts["over_existing_read_budget"] += 1
                    continue
                with source.open(info) as member:
                    raw = member.read(V8_LIMITS.max_entry_bytes + 1)
                if len(raw) > V8_LIMITS.max_entry_bytes:
                    counts["over_existing_read_budget"] += 1
                    continue
                try:
                    with V8Container(raw) as container:
                        if "module" not in container:
                            counts["no_module_entry"] += 1
                            continue
                        body = container.read("module")
                except (V8ContainerError, OSError):
                    counts["unreadable_containers"] += 1
                    continue
                maxima["module_entry_bytes"] = max(maxima["module_entry_bytes"], len(body))
                result = read_module_body(body)
                if result.text is None:
                    counts["unreadable_module_body"] += 1
                    continue
                counts["readable_module_entries"] += 1
                maxima["bsl_utf8_bytes"] = max(maxima["bsl_utf8_bytes"], len(result.text.encode("utf-8")))
    print(json.dumps({"counts": dict(counts), "max_bytes": maxima,
                      "module_entry_max_times_2_5": maxima["module_entry_bytes"] * 2.5}, sort_keys=True))


if __name__ == "__main__":
    main()
