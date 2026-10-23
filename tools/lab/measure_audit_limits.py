#!/usr/bin/env python3
"""Замер пределов приёма на исходных архивах без их изменения."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import signal
import sys
import tempfile
import time
from typing import BinaryIO
from xml.parsers.expat import ParserCreate
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mcp1c.resource_limits import V8_LIMITS
from mcp1c.syntax_parser import open_file_storage
from mcp1c.v8container import V8Container


class MeasurementDeadline(BaseException):
    """Таймаут не превращается в обычную ошибку отдельного входа."""


def read_eocd(path: Path) -> dict:
    with Path(path).open("rb") as stream:
        record = zipfile._EndRecData(stream)
    if record is None:
        raise zipfile.BadZipFile("Нет конечной записи ZIP.")
    return {"size_cd": record[zipfile._ECD_SIZE],
            "entries": record[zipfile._ECD_ENTRIES_TOTAL]}


def measure_xml(stream: BinaryIO, *, kind: str, chunk_bytes: int = 65536) -> dict:
    if kind not in {"rights", "configuration"} or chunk_bytes <= 0:
        raise ValueError("Нужен вид rights/configuration и положительный размер чанка.")
    parser = ParserCreate(namespace_separator="}")
    size = text_bytes = text_chars = max_bytes = max_chars = 0
    in_version = False
    version_size = version_trailing = max_version = 0
    version_started = False

    def reject(*_):
        raise ValueError("DTD/ENTITY не поддерживаются в XML выгрузки.")

    def boundary():
        nonlocal text_bytes, text_chars, max_bytes, max_chars
        max_bytes, max_chars = max(max_bytes, text_bytes), max(max_chars, text_chars)
        text_bytes = text_chars = 0

    def start(name, _attrs):
        nonlocal in_version, version_size, version_trailing, version_started
        boundary()
        if kind == "configuration" and name.rsplit("}", 1)[-1] == "Version":
            in_version, version_started = True, False
            version_size = version_trailing = 0

    def end(name):
        nonlocal in_version, max_version
        boundary()
        if in_version and name.rsplit("}", 1)[-1] == "Version":
            max_version = max(max_version, version_size - version_trailing)
            in_version = False

    def data(text):
        nonlocal text_bytes, text_chars, version_size, version_trailing, version_started
        text_bytes += len(text.encode("utf-8"))
        text_chars += len(text)
        if in_version:
            if not version_started:
                text = text.lstrip()
                version_started = bool(text)
            version_size += len(text)
            trailing = len(text) - len(text.rstrip())
            version_trailing = version_trailing + len(text) if text.isspace() else trailing

    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = data
    while True:
        chunk = stream.read(chunk_bytes)
        size += len(chunk)
        parser.Parse(chunk, not chunk)
        if not chunk:
            break
    if kind == "configuration":
        return {"file_bytes": size, "max_version_chars": max_version}
    return {"file_bytes": size, "max_text_node_bytes": max_bytes,
            "max_text_node_chars": max_chars}


def measure_archive(path: Path) -> dict:
    result = {
        "eocd": read_eocd(path),
        "rights": {"files": 0, "max_file_bytes": 0, "max_text_node_bytes": 0, "max_text_node_chars": 0},
        "forms": {"files": 0, "max_container_bytes": 0, "max_module_bytes": 0, "max_form_bytes": 0,
                  "module_entries": 0, "form_entries": 0},
        "configuration": {"files": 0, "max_version_chars": 0},
    }
    # Сам измеритель не должен загружать многогигабайтный каталог ZIP в память.
    # Срабатывание означает неполный замер, а не нулевой максимум.
    if result["eocd"]["size_cd"] > 256 * 1024 * 1024:
        raise ValueError("Центральный каталог больше бюджета измерителя 256 МиБ.")
    with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory(prefix="mcp1c-audit-form-") as directory:
        temporary = Path(directory) / "form.bin"
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.rsplit("/", 1)[-1]
            if name in {"Rights.xml", "Configuration.xml"}:
                kind = "rights" if name == "Rights.xml" else "configuration"
                with archive.open(info) as stream:
                    measured = measure_xml(stream, kind=kind)
                aggregate = result[kind]
                aggregate["files"] += 1
                if kind == "rights":
                    aggregate["max_file_bytes"] = max(aggregate["max_file_bytes"], measured["file_bytes"])
                for key in ("max_text_node_bytes", "max_text_node_chars", "max_version_chars"):
                    if key in measured:
                        aggregate[key] = max(aggregate[key], measured[key])
            elif name == "Form.bin" or name.endswith(".Form"):
                if info.file_size > V8_LIMITS.max_entry_bytes:
                    raise ValueError("Контейнер формы больше существующего бюджета V8.")
                total = 0
                with archive.open(info) as stream, temporary.open("wb") as output:
                    while chunk := stream.read(65536):
                        total += len(chunk)
                        if total > V8_LIMITS.max_entry_bytes:
                            raise ValueError("Контейнер формы превысил бюджет во время чтения.")
                        output.write(chunk)
                forms = result["forms"]
                forms["files"] += 1
                forms["max_container_bytes"] = max(forms["max_container_bytes"], total)
                with V8Container(temporary) as container:
                    for entry, key in (("module", "max_module_bytes"), ("form", "max_form_bytes")):
                        if entry in container:
                            body = container.read_bounded(entry, V8_LIMITS.max_entry_bytes)
                            forms[key] = max(forms[key], len(body))
                            forms[entry + "_entries"] += 1
                            del body
    return result


class _ChapterCounter(HTMLParser):
    def __init__(self):
        super().__init__()
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "div"} and any(name == "class" and "V8SH_chapter" in (value or "").split()
                                        for name, value in attrs):
            self.count += 1


def measure_hbk(path: Path) -> dict:
    result = {"pages": 0, "max_html_bytes": 0, "max_chapters": 0}
    with open_file_storage(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.endswith(".html"):
                continue
            parser = _ChapterCounter()
            size = 0
            with archive.open(info.filename) as stream:
                while chunk := stream.read(65536):
                    size += len(chunk)
                    parser.feed(chunk.decode("utf-8", errors="replace"))
            parser.close()
            result["pages"] += 1
            result["max_html_bytes"] = max(result["max_html_bytes"], size)
            result["max_chapters"] = max(result["max_chapters"], parser.count)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, action="append", default=[])
    parser.add_argument("--archives", type=Path, action="append", default=[])
    parser.add_argument("--hbk", type=Path, action="append", default=[])
    parser.add_argument("--hbk-root", type=Path, action="append", default=[])
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    options = parser.parse_args(argv)
    if options.timeout_seconds <= 0:
        parser.error("Таймаут должен быть положительным.")
    archives = set(path.resolve() for path in options.archive)
    hbks = set(path.resolve() for path in options.hbk)
    for directory in options.archives:
        if not directory.is_dir():
            parser.error("Нет каталога архивов: " + str(directory))
        archives.update(path.resolve() for path in directory.rglob("*.zip") if not path.is_symlink())
    for directory in options.hbk_root:
        if not directory.is_dir():
            parser.error("Нет каталога HBK: " + str(directory))
        hbks.update(path.resolve() for path in directory.rglob("*.hbk") if not path.is_symlink())
    if not archives and not hbks:
        parser.error("Нужен хотя бы один --archive/--archives/--hbk/--hbk-root.")
    output = options.report.resolve()
    forbidden = [(ROOT / name).resolve() for name in ("data", "__data", "reference-lab/hbk")]
    if output in archives | hbks or any(output == root or root in output.parents for root in forbidden):
        parser.error("Отчёт нельзя записывать поверх источников.")
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "inputs": [],
              "complete": False, "maxima": {}, "limits_times_2_5": {}}
    start = time.monotonic()
    previous = signal.getsignal(signal.SIGALRM)

    def deadline(*_):
        raise MeasurementDeadline("Истёк общий таймаут замера.")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(options.timeout_seconds)
    try:
        for kind, paths, measure in (("zip", archives, measure_archive), ("hbk", hbks, measure_hbk)):
            for path in sorted(paths):
                started = time.monotonic()
                record = {"kind": kind, "path": str(path)}
                try:
                    record.update(status="PASS", measurements=measure(path))
                except Exception as error:
                    record.update(status="FAIL", error=type(error).__name__ + ": " + str(error))
                record["duration_seconds"] = round(time.monotonic() - started, 3)
                report["inputs"].append(record)
                print(json.dumps(record, ensure_ascii=False), flush=True)
        report["complete"] = all(item["status"] == "PASS" for item in report["inputs"])
    except (MeasurementDeadline, KeyboardInterrupt) as error:
        report["fatal_error"] = str(error) or type(error).__name__
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        maxima = report["maxima"]
        for item in report["inputs"]:
            if item["status"] != "PASS":
                continue
            values = item["measurements"]
            flattened = {"html_bytes": values.get("max_html_bytes", 0),
                         "chapters": values.get("max_chapters", 0)} if item["kind"] == "hbk" else {
                "central_directory_bytes": values["eocd"]["size_cd"],
                "central_directory_entries": values["eocd"]["entries"],
                "module_bytes": values["forms"]["max_module_bytes"],
                "form_bytes": values["forms"]["max_form_bytes"],
                "rights_bytes": values["rights"]["max_file_bytes"],
                "text_node_bytes": values["rights"]["max_text_node_bytes"],
                "text_node_chars": values["rights"]["max_text_node_chars"],
                "version_chars": values["configuration"]["max_version_chars"],
            }
            for key, value in flattened.items():
                maxima[key] = max(maxima.get(key, 0), value)
        report["limits_times_2_5"] = {key: math.ceil(value * 2.5) for key, value in maxima.items()}
        report["maxima_are_lower_bounds"] = not report["complete"]
        report["coverage"] = {"requested": len(archives) + len(hbks), "checked": len(report["inputs"]),
                              "passed": sum(item["status"] == "PASS" for item in report["inputs"])}
        report["duration_seconds"] = round(time.monotonic() - start, 3)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
