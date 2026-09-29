#!/usr/bin/env python3
"""Локальная сборка и проверка управляемых форм без запуска MCP-сервера."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _metadata_core():
    # Общий контракт артефактов живёт в соседнем локальном skill.
    scripts = Path(__file__).resolve().parents[2] / "1c-metadata-creator" / "scripts"
    if not scripts.is_dir():
        raise ValueError("Для комплекта метаданных нужен соседний skill 1c-metadata-creator")
    sys.path.insert(0, str(scripts))
    import metadata_core
    return metadata_core


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: корень JSON должен быть объектом")
    return value


def _read_text(path: Path) -> str:
    # Path.read_text переводит CRLF в LF; канонический Forms XML сравнивается побайтно.
    return path.read_bytes().decode("utf-8-sig")


def _canonical_text(value: str) -> str:
    return "\ufeff" + value.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode("utf-8"))


def _configuration_file(path: str) -> Path:
    candidate = Path(path).expanduser()
    return candidate / "Configuration.xml" if candidate.is_dir() else candidate


def _configuration_info(path: Path) -> tuple[str, ET.Element, str, str]:
    raw = path.read_bytes()
    source = raw.decode("utf-8-sig")
    if "<!DOCTYPE" in source.upper() or "<!ENTITY" in source.upper():
        raise ValueError("Configuration.xml: DTD/ENTITY запрещены")
    root = ET.fromstring(source)
    if _local(root.tag) != "MetaDataObject":
        raise ValueError("Configuration.xml: ожидался корень MetaDataObject")
    configuration = next((node for node in root if _local(node.tag) == "Configuration"), None)
    if configuration is None:
        raise ValueError("Configuration.xml: узел Configuration не найден")
    child_objects = next((node for node in configuration if _local(node.tag) == "ChildObjects"), None)
    if child_objects is None:
        raise ValueError("Configuration.xml: узел Configuration/ChildObjects не найден")
    version = root.attrib.get("version", "")
    if not version or not all(part.isdigit() for part in version.split(".")):
        raise ValueError("Configuration.xml: версия XML неизвестна")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    return version, child_objects, source, newline


def _registration(
    configuration_path: Path, *, kind: str, name: str
) -> tuple[str, str]:
    _, child_objects, source, newline = _configuration_info(configuration_path)
    if any(_local(node.tag) == kind and (node.text or "").strip() == name for node in child_objects):
        raise ValueError(f"Configuration.xml уже регистрирует {kind}.{name}")
    close_tag = "</ChildObjects>"
    if source.count(close_tag) != 1:
        raise ValueError("Configuration.xml: ожидается один закрывающий ChildObjects")
    before, after = source.split(close_tag, 1)
    # В текстовой выгрузке список находится непосредственно в Configuration.
    lines = before.splitlines()
    child_line = next((line for line in reversed(lines) if line.lstrip(" \t").startswith("<ChildObjects")), "")
    indent = child_line[: len(child_line) - len(child_line.lstrip(" \t"))] + "\t"
    if before and not before.endswith(("\n", "\r")):
        before += newline
    changed = before + f"{indent}<{kind}>{name}</{kind}>{newline}" + close_tag + after
    ET.fromstring(changed)
    patch = "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            changed.splitlines(keepends=True),
            fromfile="Configuration.xml",
            tofile="Configuration.xml (с регистрацией)",
        )
    )
    return changed, patch


def _artifact_paths(artifacts: object) -> dict[str, str]:
    if not isinstance(artifacts, (list, tuple)):
        raise ValueError("Компилятор не вернул список артефактов")
    result: dict[str, str] = {}
    for item in artifacts:
        if isinstance(item, dict):
            path, content = item.get("path"), item.get("content")
        else:
            path, content = getattr(item, "path", None), getattr(item, "content", None)
        if not isinstance(path, str) or not isinstance(content, str):
            raise ValueError("Артефакт без строкового path/content")
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts or path in result:
            raise ValueError(f"Недопустимый путь артефакта: {path}")
        result[path] = content
    return result


def _diagnostics_failed(result: object) -> bool:
    diagnostics = getattr(result, "diagnostics", ())
    return any(getattr(item, "status", None) == "failed" for item in diagnostics)


def _form_artifacts(specification: dict[str, object], module_path: Path | None):
    from form_core import check_managed_form, compile_managed_form

    compiled = compile_managed_form(specification)
    artifacts = _artifact_paths(compiled.artifacts)
    form_name = specification.get("form_name")
    context = specification.get("context")
    if not isinstance(form_name, str) or not isinstance(context, dict):
        raise ValueError("specification: нужны form_name и context")
    form_xml = artifacts[f"Forms/{form_name}/Ext/Form.xml"]
    module_key = f"Forms/{form_name}/Ext/Form/Module.bsl"
    module_bsl = _read_text(module_path) if module_path is not None else artifacts.get(module_key)
    if module_bsl is not None:
        module_bsl = module_bsl.lstrip("\ufeff")
    if module_bsl is not None:
        artifacts[module_key] = module_bsl
    checked = check_managed_form(
        form_xml,
        form_name=form_name,
        context=context,
        module_bsl=module_bsl,
        platform_version=specification.get("platform_version"),
    )
    if _diagnostics_failed(checked) or checked.coverage.xml_parse != "passed" or checked.coverage.structural != "passed":
        raise ValueError("Форма не прошла статическую проверку: " + _json(checked.to_dict()))
    return artifacts, checked


def _auto_data_processor_spec(
    specification: dict[str, object], form_xml: str, module_bsl: str | None
) -> dict[str, object]:
    context = specification["context"]
    if not isinstance(context, dict):
        raise ValueError("Неверный context")
    owner = context.get("owner")
    if not isinstance(owner, str) or not owner.startswith("Обработка."):
        raise ValueError("Автоматический metadata descriptor поддержан для Обработка.*")
    form_name = specification["form_name"]
    title = specification.get("title")
    synonym = title.get("ru") if isinstance(title, dict) else None
    if not isinstance(synonym, str) or not synonym:
        synonym = owner.split(".", 1)[1]
    return {
        "schema_version": 1,
        "object_ref": owner,
        "format_version": specification["format_version"],
        "identity": str(uuid.uuid5(uuid.NAMESPACE_URL, f"mcp1c:form-creator:{owner}")),
        "synonym": synonym,
        "attributes": [],
        "forms": [
            {
                "name": form_name,
                "synonym": synonym,
                "role": "object",
                "default": True,
                "form_xml": form_xml,
                "module_bsl": module_bsl,
            }
        ],
    }


def _merge_metadata_form(
    metadata: dict[str, object], *, form_name: str, form_xml: str, module_bsl: str | None
) -> None:
    forms = metadata.get("forms")
    if not isinstance(forms, list):
        raise ValueError("metadata specification: forms должен быть массивом")
    matches = [item for item in forms if isinstance(item, dict) and item.get("name") == form_name]
    if len(matches) != 1:
        raise ValueError("metadata specification: нужна ровно одна forms[] запись с именем формы")
    matches[0]["form_xml"] = form_xml
    matches[0]["module_bsl"] = module_bsl


def _build(args: argparse.Namespace) -> dict[str, object]:
    specification = _read_json(Path(args.spec))
    configuration_path = _configuration_file(args.config) if args.config else None
    if configuration_path is not None:
        version, _, _, _ = _configuration_info(configuration_path)
        if specification.get("format_version") != version:
            raise ValueError(f"Формат формы {specification.get('format_version')} не совпадает с Configuration.xml {version}")
    artifacts, form_check = _form_artifacts(
        specification, Path(args.module) if args.module else None
    )
    form_name = specification["form_name"]
    form_xml = artifacts[f"Forms/{form_name}/Ext/Form.xml"]
    module_bsl = artifacts.get(f"Forms/{form_name}/Ext/Form/Module.bsl")
    object_ref = specification["context"]["owner"]

    metadata_result: dict[str, object] | None = None
    metadata_check: dict[str, object] | None = None
    if args.metadata_spec or args.auto_data_processor:
        metadata_core = _metadata_core()
        metadata = (
            _read_json(Path(args.metadata_spec))
            if args.metadata_spec
            else _auto_data_processor_spec(specification, form_xml, module_bsl)
        )
        if metadata.get("object_ref") != object_ref:
            raise ValueError("Форма и metadata specification имеют разных владельцев")
        if metadata.get("format_version") != specification.get("format_version"):
            raise ValueError("Форма и metadata specification имеют разные format_version")
        _merge_metadata_form(
            metadata,
            form_name=form_name,
            form_xml=form_xml,
            module_bsl=module_bsl,
        )
        metadata_result = metadata_core.compile_metadata_object(metadata)
        artifacts = _artifact_paths(metadata_result["artifacts"])
        metadata_check = metadata_core.check_metadata_artifacts(
            object_ref, specification["format_version"], artifacts
        )
        if metadata_check.get("status") != "passed":
            raise ValueError("Комплект метаданных не прошёл статическую проверку: " + _json(metadata_check))
    elif configuration_path is not None:
        raise ValueError("Для регистрации нужен --metadata-spec или --auto-data-processor")

    output = Path(args.output).expanduser()
    if configuration_path is not None and output.resolve().is_relative_to(configuration_path.parent.resolve()):
        raise ValueError("Выходной каталог должен быть вне исходной конфигурации")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError(f"Выходной каталог не пуст: {output}")
    changed_configuration: str | None = None
    patch: str | None = None
    if configuration_path is not None:
        kind, name = object_ref.split(".", 1)
        tags = {
            "Справочник": "Catalog",
            "Документ": "Document",
            "Обработка": "DataProcessor",
            "Отчет": "Report",
            "РегистрСведений": "InformationRegister",
        }
        if kind not in tags:
            raise ValueError(f"Регистрация владельца {kind} не поддержана")
        changed_configuration, patch = _registration(configuration_path, kind=tags[kind], name=name)
    output.mkdir(parents=True, exist_ok=True)
    for relative, content in artifacts.items():
        _write_text(output / relative, _canonical_text(content))

    if changed_configuration is not None and patch is not None:
        _write_text(output / "Configuration.xml", "\ufeff" + changed_configuration.lstrip("\ufeff"))
        (output / "Configuration.xml.patch").write_text(patch, encoding="utf-8")

    report = {
        "status": "static_passed",
        "object_ref": object_ref,
        "artifacts": sorted(artifacts),
        "forms_check": form_check.to_dict(),
        "metadata_check": metadata_check,
        "acceptance": {
            "bsl_api": "not_checked",
            "native_import": "not_checked",
            "runtime_visual": "not_checked",
            "business_behavior": "not_checked",
        },
    }
    (output / "verification.json").write_text(_json(report), encoding="utf-8")
    return {
        "status": report["status"],
        "output": str(output),
        "artifacts": len(artifacts),
        "acceptance": report["acceptance"],
    }


def _inspect(args: argparse.Namespace) -> dict[str, object]:
    path = _configuration_file(args.config)
    version, child_objects, _, _ = _configuration_info(path)
    kinds: dict[str, list[str]] = {}
    for node in child_objects:
        kinds.setdefault(_local(node.tag), []).append((node.text or "").strip())
    return {
        "status": "inspected",
        "configuration_xml": str(path),
        "format_version": version,
        "objects": kinds,
    }


def _extract(args: argparse.Namespace) -> dict[str, object]:
    from form_core import decompile_managed_form, compile_managed_form

    source_xml = _read_text(Path(args.form_xml))
    module_bsl = _read_text(Path(args.module)) if args.module else None
    context = {"owner": args.owner, "role": args.role}
    decompiled = decompile_managed_form(
        source_xml,
        form_name=args.form_name,
        context=context,
        module_bsl=module_bsl,
    )
    specification = decompiled.specification
    if not isinstance(specification, dict):
        raise ValueError("Форма не восстановлена в поддержанную specification")
    rebuilt = compile_managed_form(specification)
    rebuilt_xml = _artifact_paths(rebuilt.artifacts)[f"Forms/{args.form_name}/Ext/Form.xml"]
    if rebuilt_xml.lstrip("\ufeff") != _canonical_text(source_xml).lstrip("\ufeff"):
        raise ValueError(
            "Форма не проходит точный round-trip через compiler; её нельзя "
            "переписывать из неполной specification. Используйте нативную правку."
        )
    output = Path(args.output).expanduser()
    if output.exists():
        raise ValueError(f"Файл уже существует: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_json(specification), encoding="utf-8")
    return {"status": "extracted", "specification": str(output)}


def _validate(args: argparse.Namespace) -> dict[str, object]:
    from form_core import check_managed_form

    form_path = Path(args.form_xml)
    checked = check_managed_form(
        _read_text(form_path),
        form_name=args.form_name,
        context={"owner": args.owner, "role": args.role},
        module_bsl=_read_text(Path(args.module)) if args.module else None,
        platform_version=args.platform_version,
    )
    failed = _diagnostics_failed(checked) or checked.coverage.xml_parse != "passed" or checked.coverage.structural != "passed"
    metadata_check = None
    if args.bundle:
        if not args.format_version:
            raise ValueError("Для --bundle нужен --format-version")
        bundle = Path(args.bundle)
        artifacts: dict[str, str] = {}
        for path in bundle.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".xml", ".bsl"} and path.name != "Configuration.xml":
                artifacts[str(path.relative_to(bundle))] = _read_text(path)
        metadata_check = _metadata_core().check_metadata_artifacts(args.owner, args.format_version, artifacts)
        failed = failed or metadata_check.get("status") != "passed"
    return {
        "status": "failed" if failed else "static_passed",
        "forms_check": checked.to_dict(),
        "metadata_check": metadata_check,
        "acceptance": {
            "bsl_api": "not_checked",
            "native_import": "not_checked",
            "runtime_visual": "not_checked",
            "business_behavior": "not_checked",
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="Прочитать версию и состав Configuration.xml")
    inspect.add_argument("--config", required=True)
    inspect.set_defaults(handler=_inspect)

    build = commands.add_parser("build", help="Собрать форму или комплект метаданных в отдельный каталог")
    build.add_argument("--spec", required=True, help="JSON specification управляемой формы")
    build.add_argument("--module", help="Итоговый Module.bsl вместо каркаса")
    build.add_argument("--metadata-spec", help="JSON metadata specification с forms[]")
    build.add_argument("--auto-data-processor", action="store_true", help="Создать descriptor встроенной Обработка.*")
    build.add_argument("--config", help="Целевая Configuration.xml или каталог выгрузки для сверки версии и регистрационного патча")
    build.add_argument("--output", required=True)
    build.set_defaults(handler=_build)

    extract = commands.add_parser("extract", help="Восстановить поддержанную specification существующей формы")
    extract.add_argument("--form-xml", required=True)
    extract.add_argument("--module")
    extract.add_argument("--owner", required=True)
    extract.add_argument("--role", required=True)
    extract.add_argument("--form-name", required=True)
    extract.add_argument("--output", required=True)
    extract.set_defaults(handler=_extract)

    validate = commands.add_parser("validate", help="Проверить форму и, если указан, комплект метаданных")
    validate.add_argument("--form-xml", required=True)
    validate.add_argument("--module")
    validate.add_argument("--owner", required=True)
    validate.add_argument("--role", required=True)
    validate.add_argument("--form-name", required=True)
    validate.add_argument("--platform-version")
    validate.add_argument("--bundle")
    validate.add_argument("--format-version")
    validate.set_defaults(handler=_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = args.handler(args)
    except (OSError, ValueError, KeyError, ET.ParseError) as exc:
        print(_json({"status": "failed", "error": str(exc)}), file=sys.stderr, end="")
        return 2
    except Exception as exc:
        diagnostics = getattr(exc, "diagnostics", None)
        if diagnostics is None:
            raise
        print(_json({"status": "failed", "diagnostics": diagnostics}), file=sys.stderr, end="")
        return 2
    print(_json(result), end="")
    return 2 if result.get("status") == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
