#!/usr/bin/env python3
"""Локальная сборка и статическая проверка объектов метаданных 1С."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

from metadata_core import (
    MetadataAuthoringContractError,
    check_metadata_artifacts,
    compile_metadata_object,
)

_TAGS = {
    "Справочник": "Catalog",
    "Документ": "Document",
    "РегистрСведений": "InformationRegister",
    "Обработка": "DataProcessor",
    "Отчет": "Report",
}
_SAFE_SEGMENT = re.compile(r"^[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*$")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def _write_artifact(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Текстовые файлы выгрузки должны быть побайтно каноническими.
    body = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    path.write_bytes(("\ufeff" + body).encode("utf-8"))


def _specification(path: Path) -> dict[str, object]:
    spec = json.loads(_read_text(path))
    if not isinstance(spec, dict):
        raise ValueError("Корень specification должен быть JSON-объектом")
    forms = spec.get("forms")
    if not isinstance(forms, list):
        raise ValueError("specification.forms должен быть массивом")
    for index, form in enumerate(forms):
        if not isinstance(form, dict):
            raise ValueError(f"forms[{index}] должен быть объектом")
        for key in ("form_xml", "module_bsl"):
            source = form.pop(f"{key}_file", None)
            if source is None:
                continue
            if key in form:
                raise ValueError(f"forms[{index}]: задайте только {key} или {key}_file")
            if not isinstance(source, str) or not source:
                raise ValueError(f"forms[{index}].{key}_file: нужен путь")
            form[key] = _read_text((path.parent / source).resolve())
    return spec


def _configuration(path: Path) -> tuple[str, str, str, ET.Element]:
    target = path / "Configuration.xml" if path.is_dir() else path
    raw = target.read_bytes()
    source = raw.decode("utf-8-sig")
    if re.search(r"<!\s*(DOCTYPE|ENTITY)\b", source, re.I):
        raise ValueError("Configuration.xml: DTD/ENTITY запрещены")
    root = ET.fromstring(source)
    if _local(root.tag) != "MetaDataObject":
        raise ValueError("Configuration.xml: ожидался MetaDataObject")
    configuration = next((node for node in root if _local(node.tag) == "Configuration"), None)
    children = next(
        (node for node in configuration if _local(node.tag) == "ChildObjects"),
        None,
    ) if configuration is not None else None
    if children is None:
        raise ValueError("Configuration.xml: нет Configuration/ChildObjects")
    version = root.attrib.get("version", "")
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)*", version):
        raise ValueError("Configuration.xml: неизвестная версия XML")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return version, source, newline, children


def _registered_configuration(path: Path, object_ref: str) -> tuple[str, str]:
    kind, name = object_ref.split(".", 1)
    tag = _TAGS[kind]
    _, source, newline, children = _configuration(path)
    if any(_local(node.tag) == tag and (node.text or "").strip() == name for node in children):
        raise ValueError(f"Configuration.xml уже регистрирует {object_ref}")
    closing = "</ChildObjects>"
    if source.count(closing) != 1:
        raise ValueError("Configuration.xml: ожидается один закрывающий ChildObjects")
    before, after = source.split(closing, 1)
    lines = before.splitlines()
    opening = next((line for line in reversed(lines) if line.lstrip(" \t").startswith("<ChildObjects")), "")
    indent = opening[:len(opening) - len(opening.lstrip(" \t"))] + "\t"
    if before and not before.endswith(("\r", "\n")):
        before += newline
    changed = before + f"{indent}<{tag}>{name}</{tag}>{newline}" + closing + after
    ET.fromstring(changed)
    patch = "".join(difflib.unified_diff(
        source.splitlines(keepends=True), changed.splitlines(keepends=True),
        fromfile="Configuration.xml", tofile="Configuration.xml (с регистрацией)",
    ))
    return changed, patch


def _owner_path(object_ref: str) -> str:
    kind, name = object_ref.split(".", 1)
    if kind not in _TAGS or not _SAFE_SEGMENT.fullmatch(name):
        raise ValueError(f"Неподдерживаемый object_ref: {object_ref}")
    return {"Catalog": "Catalogs", "Document": "Documents", "InformationRegister": "InformationRegisters", "DataProcessor": "DataProcessors", "Report": "Reports"}[_TAGS[kind]] + "/" + name


def _acceptance() -> dict[str, str]:
    return {"bsl_api": "not_checked", "native_import": "not_checked", "runtime_visual": "not_checked", "business_behavior": "not_checked"}


def _build(args: argparse.Namespace) -> dict[str, object]:
    spec = _specification(Path(args.spec))
    object_ref = spec.get("object_ref")
    version = spec.get("format_version")
    if not isinstance(object_ref, str) or not isinstance(version, str):
        raise ValueError("specification требует object_ref и format_version")
    owner = _owner_path(object_ref)
    config = Path(args.config).expanduser() if args.config else None
    if config is not None:
        config_version, _, _, _ = _configuration(config)
        if version != config_version:
            raise ValueError(f"format_version {version} не совпадает с Configuration.xml {config_version}")
    compiled = compile_metadata_object(spec)
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    checked = check_metadata_artifacts(object_ref, version, artifacts)
    if checked["status"] != "passed":
        raise ValueError("Комплект не прошёл статическую проверку: " + _json(checked))
    if not all(path == owner + ".xml" or path.startswith(owner + "/") for path in artifacts):
        raise ValueError("Компилятор вернул путь вне владельца")
    output = Path(args.output).expanduser().resolve()
    if config is not None:
        config_dir = (config if config.is_dir() else config.parent).resolve()
        if output.is_relative_to(config_dir):
            raise ValueError("Каталог результата должен быть вне исходной выгрузки")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Каталог результата не пуст: {output}")
    changed = patch = None
    if config is not None:
        changed, patch = _registered_configuration(config, object_ref)
    output.mkdir(parents=True, exist_ok=True)
    for relative, content in artifacts.items():
        _write_artifact(output / relative, content)
    if changed is not None and patch is not None:
        _write_artifact(output / "Configuration.xml", changed)
        (output / "Configuration.xml.patch").write_text(patch, encoding="utf-8")
    report = {"status": "static_passed", "object_ref": object_ref, "format_version": version,
              "artifacts": sorted(artifacts), "metadata_check": checked, "acceptance": _acceptance()}
    (output / "verification.json").write_text(_json(report), encoding="utf-8")
    return {"status": "static_passed", "output": str(output), "artifacts": len(artifacts), "acceptance": report["acceptance"]}


def _validate(args: argparse.Namespace) -> dict[str, object]:
    root = Path(args.bundle).expanduser().resolve()
    owner = _owner_path(args.object_ref)
    files = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {".xml", ".bsl"}]
    artifacts: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        if relative == "Configuration.xml":
            continue
        if relative != owner + ".xml" and not relative.startswith(owner + "/"):
            raise ValueError(f"Посторонний XML/BSL в комплекте: {relative}")
        artifacts[relative] = _read_text(path)
    checked = check_metadata_artifacts(args.object_ref, args.format_version, artifacts)
    return {"status": "static_passed" if checked["status"] == "passed" else "failed",
            "metadata_check": checked, "acceptance": _acceptance()}


def _inspect(args: argparse.Namespace) -> dict[str, object]:
    version, _, _, children = _configuration(Path(args.config).expanduser())
    return {"status": "inspected", "format_version": version,
            "objects": [{"kind": _local(node.tag), "name": (node.text or "").strip()} for node in children]}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Прочитать формат и регистрацию Configuration.xml")
    inspect.add_argument("--config", required=True)
    inspect.set_defaults(handler=_inspect)
    build = commands.add_parser("build", help="Собрать owner-relative комплект в пустой каталог")
    build.add_argument("--spec", required=True)
    build.add_argument("--config", help="Сверить XML-версию и подготовить регистрацию")
    build.add_argument("--output", required=True)
    build.set_defaults(handler=_build)
    validate = commands.add_parser("validate", help="Статически проверить комплект")
    validate.add_argument("--bundle", required=True)
    validate.add_argument("--object-ref", required=True)
    validate.add_argument("--format-version", required=True)
    validate.set_defaults(handler=_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = args.handler(args)
    except MetadataAuthoringContractError as exc:
        print(_json({"status": "failed", "diagnostics": exc.diagnostics}), file=sys.stderr, end="")
        return 2
    except (OSError, ValueError, KeyError, ET.ParseError) as exc:
        print(_json({"status": "failed", "error": str(exc)}), file=sys.stderr, end="")
        return 2
    print(_json(result), end="")
    return 2 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
