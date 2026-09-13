"""Многоуровневая статическая проверка артефактов управляемой формы."""

from __future__ import annotations

import re
from collections import Counter
from xml.etree import ElementTree as ET

from mcp1c.bsl_lex import разобрать

from .decompiler import decompile_managed_form
from .diagnostics import Coverage, Diagnostic, FormsResult
from .event_catalog import event_signature
from .models import is_reserved_bsl_keyword


_LOGFORM = "http://v8.1c.ru/8.3/xcf/logform"
_V8 = "http://v8.1c.ru/8.1/data/core"
_NORMAL_DATA_PATH = re.compile(
    r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*"
    r"(?:\.[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)*\Z"
)
_BSL_IDENTIFIER = r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*"
_NEW_FILE_ASSIGNMENT = re.compile(
    rf"(?im)^\s*(?P<name>{_BSL_IDENTIFIER})\s*=\s*Новый\s+Файл\s*\("
)


def _q(local: str, namespace: str = _LOGFORM) -> str:
    return f"{{{namespace}}}{local}"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _diagnostic(
    status: str,
    code: str,
    path: str,
    message: str,
    *,
    level: str = "structural",
) -> Diagnostic:
    return Diagnostic(level, status, code, path, message)


def _check_id_space(
    container: ET.Element | None,
    *,
    scope: str,
    duplicate_code: str,
    recursive: bool = False,
) -> list[Diagnostic]:
    if container is None:
        return []
    diagnostics: list[Diagnostic] = []
    seen: set[int] = set()
    nodes = container.iter() if recursive else iter(container)
    for node in nodes:
        if node is container or "id" not in node.attrib:
            continue
        path = f"/Form/{scope}/{_local(node.tag)}[@id='{node.attrib['id']}']"
        try:
            value = int(node.attrib["id"])
        except ValueError:
            diagnostics.append(
                _diagnostic(
                    "failed",
                    "invalid_id",
                    path,
                    f"ID в пространстве {scope} должен быть целым числом.",
                )
            )
            continue
        if value <= 0:
            diagnostics.append(
                _diagnostic(
                    "failed",
                    "invalid_id",
                    path,
                    f"ID в пространстве {scope} должен быть положительным.",
                )
            )
        if value in seen:
            diagnostics.append(
                _diagnostic(
                    "failed",
                    duplicate_code,
                    path,
                    f"ID {value} повторяется внутри пространства {scope}.",
                )
            )
        seen.add(value)
    return diagnostics


def _check_ids(root: ET.Element) -> list[Diagnostic]:
    diagnostics = [
        *_check_id_space(
            root.find(_q("ChildItems")),
            scope="elements",
            duplicate_code="duplicate_element_id",
            recursive=True,
        ),
        *_check_id_space(
            root.find(_q("Attributes")),
            scope="attributes",
            duplicate_code="duplicate_attribute_id",
        ),
        *_check_id_space(
            root.find(_q("Commands")),
            scope="commands",
            duplicate_code="duplicate_command_id",
        ),
    ]
    if not diagnostics:
        diagnostics.append(
            _diagnostic(
                "passed",
                "separate_id_spaces_valid",
                "/Form",
                "ID элементов, реквизитов и команд проверены раздельно.",
            )
        )
    return diagnostics


def _check_localizations(root: ET.Element) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    checked = 0
    for node in root.iter():
        if node.tag not in {_q("Title"), _q("ToolTip")}:
            continue
        checked += 1
        items = [child for child in node if child.tag == _q("item", _V8)]
        # Пустой Title встречается в валидных выгрузках: он не содержит
        # многоязычной структуры, которую здесь можно проверить.
        if not items:
            continue
        languages: list[str] = []
        for item in items:
            lang = [child for child in item if child.tag == _q("lang", _V8)]
            content = [child for child in item if child.tag == _q("content", _V8)]
            if len(lang) != 1 or len(content) != 1 or not (lang[0].text or ""):
                diagnostics.append(
                    _diagnostic(
                        "failed",
                        "invalid_localization_item",
                        f"/Form/{_local(node.tag)}",
                        "Каждая локализация требует ровно lang и content.",
                    )
                )
                continue
            languages.append(lang[0].text or "")
        duplicates = [name for name, count in Counter(languages).items() if count > 1]
        if duplicates:
            diagnostics.append(
                _diagnostic(
                    "failed",
                    "duplicate_localization_language",
                    f"/Form/{_local(node.tag)}",
                    "Язык локализации повторяется внутри одного заголовка.",
                )
            )
    if checked and not diagnostics:
        diagnostics.append(
            _diagnostic(
                "passed",
                "localization_structure_valid",
                "/Form",
                "Структура локализованных заголовков согласована.",
            )
        )
    return diagnostics


def _check_local_links(root: ET.Element, *, strict: bool) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    attribute_names = {
        node.attrib["name"]
        for node in root.findall(f"{_q('Attributes')}/{_q('Attribute')}")
        if "name" in node.attrib
    }
    command_names = {
        node.attrib["name"]
        for node in root.findall(f"{_q('Commands')}/{_q('Command')}")
        if "name" in node.attrib
    }
    for node in root.iter(_q("InputField")):
        data_path = node.find(_q("DataPath"))
        value = "" if data_path is None else (data_path.text or "")
        if not _NORMAL_DATA_PATH.fullmatch(value):
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "special_data_path_not_checked",
                    "/Form/ChildItems/InputField/DataPath",
                    "Специальный DataPath сохранён, но статически не разрешён.",
                )
            )
        elif value.split(".", 1)[0] not in attribute_names:
            diagnostics.append(
                _diagnostic(
                    "failed" if strict else "warning",
                    (
                        "unresolved_data_path"
                        if strict
                        else "data_path_not_resolved_in_inventory"
                    ),
                    "/Form/ChildItems/InputField/DataPath",
                    "Первый сегмент DataPath не найден среди реквизитов формы.",
                )
            )
    prefix = "Form.Command."
    for node in root.iter(_q("Button")):
        command = node.find(_q("CommandName"))
        value = "" if command is None else (command.text or "")
        if value.startswith(prefix):
            if value.removeprefix(prefix) not in command_names:
                diagnostics.append(
                    _diagnostic(
                        "failed",
                        "unresolved_command",
                        "/Form/ChildItems/Button/CommandName",
                        "Кнопка ссылается на неизвестную команду формы.",
                    )
                )
        else:
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "non_form_command_not_checked",
                    "/Form/ChildItems/Button/CommandName",
                    "Нестандартная ссылка команды сохранена без разрешения.",
                )
            )
    if not diagnostics:
        diagnostics.append(
            _diagnostic(
                "passed",
                "local_form_links_resolved",
                "/Form",
                "Обычные DataPath и Form.Command ссылки разрешены локально.",
            )
        )
    return diagnostics


def _arity(parameters: str) -> int:
    stripped = parameters.strip()
    return 0 if not stripped else len(stripped.split(","))


def _expected_handlers(root: ET.Element) -> list[tuple[str, str, int, str]]:
    result: list[tuple[str, str, int, str]] = []

    def append_events(node: ET.Element, owner_kind: str, path: str) -> None:
        events = node.find(_q("Events"))
        if events is None:
            return
        for event in events.findall(_q("Event")):
            signature = event_signature(owner_kind, event.attrib.get("name", ""))
            if signature is not None and (event.text or ""):
                result.append(
                    (
                        event.text or "",
                        signature.directive.casefold(),
                        len(signature.parameters),
                        f"{path}/Events/Event",
                    )
                )

    append_events(root, "form", "/Form")
    owner_kinds = {
        _q("InputField"): "input_field",
        _q("CheckBoxField"): "check_box_field",
        _q("Pages"): "pages",
        _q("Table"): "table",
    }
    for node in root.iter():
        owner_kind = owner_kinds.get(node.tag)
        if owner_kind is not None:
            append_events(
                node,
                owner_kind,
                f"/Form/ChildItems/{_local(node.tag)}[@name='{node.attrib.get('name', '')}']",
            )
    commands = root.find(_q("Commands"))
    if commands is not None:
        for command in commands.findall(_q("Command")):
            action = command.find(_q("Action"))
            if action is not None and (action.text or ""):
                result.append(
                    (action.text or "", "наклиенте", 1, "/Form/Commands/Command")
                )
    return result


def _check_forbidden_synchronous_client_calls(
    module_bsl: str,
    procedures: list[object],
) -> list[Diagnostic]:
    """Поймать доказанный нативной приёмкой синхронный клиентский вызов."""

    lines = module_bsl.splitlines(keepends=True)
    diagnostics: list[Diagnostic] = []
    for procedure in procedures:
        directive = procedure.директива.casefold()
        if directive not in {"наклиенте", "наклиентенасерверебезконтекста"}:
            continue
        start = max(procedure.строка - 1, 0)
        end = procedure.конец if procedure.конец else len(lines)
        body = "".join(lines[start:end])
        for assignment in _NEW_FILE_ASSIGNMENT.finditer(body):
            variable = assignment.group("name")
            exists_call = re.search(
                rf"(?i)\b{re.escape(variable)}\s*\.\s*Существует\s*\(",
                body[assignment.end() :],
            )
            if exists_call is None:
                continue
            call_offset = assignment.end() + exists_call.start()
            line = procedure.строка + body[:call_offset].count("\n")
            diagnostics.append(
                _diagnostic(
                    "failed",
                    "forbidden_synchronous_client_call",
                    f"$module_bsl:{line}",
                    (
                        f"Синхронный вызов {variable}.Существует() запрещён "
                        "в клиентском контексте управляемого приложения."
                    ),
                    level="bsl_static",
                )
            )
    return diagnostics


def _check_async_contract(procedures: list[object]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for procedure in procedures:
        path = f"$module_bsl:{procedure.строка}"
        directive = procedure.директива.casefold()
        if procedure.асинх and directive != "наклиенте":
            diagnostics.append(
                _diagnostic(
                    "failed",
                    "async_requires_client_context",
                    path,
                    f"Асинхронная процедура {procedure.имя} должна быть клиентской.",
                    level="bsl_static",
                )
            )
        if procedure.ожидания and not procedure.асинх:
            diagnostics.append(
                _diagnostic(
                    "failed",
                    "await_requires_async",
                    f"$module_bsl:{procedure.ожидания[0]}",
                    f"Ждать внутри {procedure.имя} требует объявления Асинх.",
                    level="bsl_static",
                )
            )
    return diagnostics


def _check_bsl(
    root: ET.Element,
    module_bsl: str | None,
) -> tuple[str, list[Diagnostic]]:
    if module_bsl is None:
        return (
            "not_checked",
            [
                _diagnostic(
                    "not_checked",
                    "module_not_provided",
                    "$module_bsl",
                    "Module.bsl не передан.",
                    level="bsl_static",
                )
            ],
        )
    expected = _expected_handlers(root)
    if not expected:
        return (
            "not_checked",
            [
                _diagnostic(
                    "not_checked",
                    "no_supported_bindings",
                    "/Form",
                    "Нет поддержанных XML-привязок для проверки BSL.",
                    level="bsl_static",
                )
            ],
        )
    procedures = разобрать(module_bsl, отслеживать_ждать=True)
    by_name: dict[str, list[object]] = {}
    for procedure in procedures:
        by_name.setdefault(procedure.имя.casefold(), []).append(procedure)
    diagnostics: list[Diagnostic] = []
    for handler, expected_directive, expected_arity, path in expected:
        if is_reserved_bsl_keyword(handler):
            diagnostics.append(
                _diagnostic(
                    "failed",
                    "reserved_bsl_keyword",
                    path,
                    f"Обработчик {handler} совпадает с зарезервированным словом BSL.",
                    level="bsl_static",
                )
            )
            continue
        matches = by_name.get(handler.casefold(), [])
        if not matches:
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "handler_not_found",
                    path,
                    f"Обработчик {handler} не найден в переданном Module.bsl.",
                    level="bsl_static",
                )
            )
            continue
        if len(matches) > 1:
            diagnostics.append(
                _diagnostic(
                    "failed",
                    "duplicate_handler",
                    path,
                    f"Обработчик {handler} объявлен несколько раз.",
                    level="bsl_static",
                )
            )
        procedure = matches[0]
        if procedure.вид != "процедура":
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "handler_kind_differs",
                    path,
                    f"Обработчик {handler} объявлен не как процедура.",
                    level="bsl_static",
                )
            )
        if procedure.конец == 0:
            diagnostics.append(
                _diagnostic(
                    "failed",
                    "unfinished_handler_declaration",
                    path,
                    f"У обработчика {handler} не найдено завершение.",
                    level="bsl_static",
                )
            )
        if procedure.директива.casefold() != expected_directive:
            diagnostics.append(
                _diagnostic(
                    "failed",
                    "handler_directive_mismatch",
                    path,
                    f"Контекст выполнения обработчика {handler} не совпадает с XML.",
                    level="bsl_static",
                )
            )
        if _arity(procedure.параметры) != expected_arity:
            diagnostics.append(
                _diagnostic(
                    "warning",
                    "handler_arity_mismatch",
                    path,
                    (
                        f"Число параметров обработчика {handler} отличается от "
                        "канонического каркаса первой вертикали."
                    ),
                    level="bsl_static",
                )
            )
    diagnostics.extend(
        _check_forbidden_synchronous_client_calls(module_bsl, procedures)
    )
    diagnostics.extend(_check_async_contract(procedures))
    if any(item.status == "failed" for item in diagnostics):
        return "failed", diagnostics
    if any(item.status == "warning" for item in diagnostics):
        return "warning", diagnostics
    return (
        "passed",
        [
            _diagnostic(
                "passed",
                "xml_bsl_handlers_matched",
                "/Form",
                "Поддержанные XML-привязки согласованы с Module.bsl.",
                level="bsl_static",
            )
        ],
    )


def check_managed_form(
    form_xml: object,
    *,
    form_name: object,
    module_bsl: object | None = None,
) -> FormsResult:
    """Проверить доступные уровни, не обращаясь к Registry, диску или 1С."""

    decompiled = decompile_managed_form(
        form_xml,
        form_name=form_name,
        module_bsl=module_bsl,
    )
    if decompiled.coverage.xml_parse != "passed":
        return FormsResult(
            status="checked",
            specification=decompiled.specification,
            diagnostics=decompiled.diagnostics,
            coverage=decompiled.coverage,
            instructions=("Исправьте XML до выполнения остальных проверок.",),
        )
    if not isinstance(form_xml, str):
        raise AssertionError("decompiler не мог принять нестроковый XML")
    root = ET.fromstring(form_xml)

    diagnostics = [
        item
        for item in decompiled.diagnostics
        if item.level not in {"bsl_static", "configuration_links"}
    ]
    structural_checks = [
        *_check_ids(root),
        *_check_localizations(root),
        *_check_local_links(
            root,
            strict=decompiled.coverage.structural == "passed",
        ),
    ]
    diagnostics.extend(structural_checks)
    structural_failed = any(item.status == "failed" for item in structural_checks)
    if structural_failed:
        diagnostics = [
            item
            for item in diagnostics
            if not (item.level == "structural" and item.status == "passed")
        ]
        structural_status = "failed"
    else:
        structural_status = decompiled.coverage.structural

    safe_module = module_bsl if isinstance(module_bsl, str) else None
    bsl_status, bsl_diagnostics = _check_bsl(root, safe_module)
    diagnostics.extend(bsl_diagnostics)
    diagnostics.append(
        _diagnostic(
            "not_checked",
            "registry_snapshot_not_used",
            "$",
            "Проверены локальные ссылки формы; snapshot Registry не передан.",
            level="configuration_links",
        )
    )
    return FormsResult(
        status="checked",
        specification=decompiled.specification,
        diagnostics=tuple(diagnostics),
        coverage=Coverage(
            xml_parse="passed",
            structural=structural_status,
            configuration_links="not_checked",
            bsl_static=bsl_status,
            platform_import="not_checked",
            runtime_visual="not_checked",
        ),
        instructions=(
            "Статический результат не заменяет импорт и визуальную приёмку в 1С.",
        ),
    )


__all__ = ["check_managed_form"]
