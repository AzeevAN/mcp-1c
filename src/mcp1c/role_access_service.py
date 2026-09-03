"""Единый bounded-контракт объявленных прав для MCP и HTTP.

Модуль не знает ни о Starlette, ни о MCP SDK. Оба транспорта получают один
JSON-совместимый payload и не могут разойтись в resolver-е, пагинации или
трактовке explicit/conditional прав.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .registry import Registry
from .role_access import (
    OPERATION_PRESENTATION,
    OPERATION_RIGHTS,
    DeclaredRight,
    LoadedRoleAccess,
    RoleAccessIndex,
    RoleCandidate,
    RoleDescriptor,
    RoleObjectSummary,
    source_target,
)


API_VERSION = "v1"
DECLARATION_SCOPE = "declared_role_rights"
DISCLAIMER = (
    "Показаны объявленные права ролей, а не эффективный доступ пользователя."
)
MIN_PAGE_CHARS = 256
MAX_PAGE_CHARS = 8000
DEFAULT_PAGE_CHARS = 2000
MAX_FIND_LIMIT = 20
MAX_ACCESS_LIMIT = 100
MAX_ROLE_LIST_LIMIT = 100
MAX_ROLE_QUERY_CHARS = 256
MAX_OBJECT_QUERY_CHARS = 256
MAX_CURSOR_CHARS = 2048
MAX_NAME_CHARS = 512
MAX_COMMENT_CHARS = 2048

_SOURCE_KIND_PRESENTATION: dict[str, tuple[str, str]] = {
    "AccountingRegister": ("РегистрБухгалтерии", "Регистр бухгалтерии"),
    "AccumulationRegister": ("РегистрНакопления", "Регистр накопления"),
    "BusinessProcess": ("БизнесПроцесс", "Бизнес-процесс"),
    "CalculationRegister": ("РегистрРасчета", "Регистр расчёта"),
    "Catalog": ("Справочник", "Справочник"),
    "ChartOfAccounts": ("ПланСчетов", "План счетов"),
    "ChartOfCalculationTypes": ("ПланВидовРасчета", "План видов расчёта"),
    "ChartOfCharacteristicTypes": (
        "ПланВидовХарактеристик",
        "План видов характеристик",
    ),
    "CommonAttribute": ("ОбщийРеквизит", "Общий реквизит"),
    "CommonCommand": ("ОбщаяКоманда", "Общая команда"),
    "CommonForm": ("ОбщаяФорма", "Общая форма"),
    "CommonModule": ("ОбщийМодуль", "Общий модуль"),
    "Configuration": ("Конфигурация", "Конфигурация"),
    "Constant": ("Константа", "Константа"),
    "DataProcessor": ("Обработка", "Обработка"),
    "DefinedType": ("ОпределяемыйТип", "Определяемый тип"),
    "Document": ("Документ", "Документ"),
    "DocumentJournal": ("ЖурналДокументов", "Журнал документов"),
    "Enum": ("Перечисление", "Перечисление"),
    "EventSubscription": ("ПодпискаНаСобытие", "Подписка на событие"),
    "ExchangePlan": ("ПланОбмена", "План обмена"),
    "FilterCriterion": ("КритерийОтбора", "Критерий отбора"),
    "HTTPService": ("HTTPСервис", "HTTP-сервис"),
    "InformationRegister": ("РегистрСведений", "Регистр сведений"),
    "Interface": ("Интерфейс", "Интерфейс"),
    "Report": ("Отчет", "Отчёт"),
    "ScheduledJob": ("РегламентноеЗадание", "Регламентное задание"),
    "Sequence": ("Последовательность", "Последовательность"),
    "SessionParameter": ("ПараметрСеанса", "Параметр сеанса"),
    "Subsystem": ("Подсистема", "Подсистема"),
    "Task": ("Задача", "Задача"),
    "WebService": ("WebСервис", "Web-сервис"),
}

_CHILD_KIND_LABELS = {
    "AccountingFlag": "Признак учёта",
    "Attribute": "Реквизит",
    "Command": "Команда",
    "Dimension": "Измерение",
    "ExtDimensionAccountingFlag": "Признак учёта субконто",
    "Operation": "Операция",
    "Resource": "Ресурс",
    "StandardAttribute": "Стандартный реквизит",
    "StandardTabularSection": "Стандартная табличная часть",
    "Subsystem": "Подсистема",
    "TabularSection": "Табличная часть",
    "URLTemplate": "Шаблон URL",
}

_RIGHT_LABELS = {
    "ActiveUsers": "Активные пользователи",
    "Administration": "Администрирование",
    "AnalyticsSystemClient": "Клиент системы аналитики",
    "Automation": "Автоматизация",
    "ConfigurationExtensionsAdministration": "Администрирование расширений",
    "DataAdministration": "Администрирование данных",
    "Delete": "Удаление данных",
    "Edit": "Интерактивное редактирование",
    "EditDataHistoryVersionComment": "Изменение комментария версии истории данных",
    "EventLog": "Журнал регистрации",
    "ExclusiveMode": "Монопольный режим",
    "ExternalConnection": "Внешнее соединение",
    "Get": "Получение",
    "InputByString": "Ввод по строке",
    "Insert": "Добавление данных",
    "InteractiveChangeOfPosted": "Интерактивное изменение проведённых данных",
    "InteractiveClearDeletionMark": "Интерактивное снятие пометки удаления",
    "InteractiveClearDeletionMarkPredefinedData": "Снятие пометки удаления предопределённых данных",
    "InteractiveDelete": "Интерактивное удаление",
    "InteractiveDeleteMarked": "Удаление помеченных объектов",
    "InteractiveDeleteMarkedPredefinedData": "Удаление помеченных предопределённых данных",
    "InteractiveDeletePredefinedData": "Интерактивное удаление предопределённых данных",
    "InteractiveInsert": "Интерактивное добавление",
    "InteractiveOpenExtDataProcessors": "Открытие внешних обработок",
    "InteractiveOpenExtReports": "Открытие внешних отчётов",
    "InteractivePosting": "Интерактивное проведение",
    "InteractivePostingRegular": "Интерактивное оперативное проведение",
    "InteractiveSetDeletionMark": "Интерактивная установка пометки удаления",
    "InteractiveSetDeletionMarkPredefinedData": "Пометка удаления предопределённых данных",
    "InteractiveUndoPosting": "Интерактивная отмена проведения",
    "MainWindowModeEmbeddedWorkplace": "Режим встроенного рабочего места",
    "MainWindowModeFullscreenWorkplace": "Полноэкранное рабочее место",
    "MainWindowModeKiosk": "Режим киоска",
    "MainWindowModeNormal": "Обычный режим главного окна",
    "MainWindowModeWorkplace": "Режим рабочего места",
    "MobileClient": "Мобильный клиент",
    "Output": "Вывод",
    "Posting": "Проведение",
    "Read": "Чтение данных",
    "ReadDataHistory": "Чтение истории данных",
    "ReadDataHistoryOfMissingData": "Чтение истории отсутствующих данных",
    "SaveUserData": "Сохранение данных пользователя",
    "Set": "Установка",
    "SwitchToDataHistoryVersion": "Переход к версии истории данных",
    "TechnicalSpecialistMode": "Режим технического специалиста",
    "ThickClient": "Толстый клиент",
    "ThinClient": "Тонкий клиент",
    "TotalsControl": "Управление итогами",
    "UndoPosting": "Отмена проведения",
    "Update": "Изменение данных",
    "UpdateDataBaseConfiguration": "Обновление конфигурации базы данных",
    "UpdateDataHistory": "Изменение истории данных",
    "UpdateDataHistoryOfMissingData": "Изменение истории отсутствующих данных",
    "UpdateDataHistorySettings": "Изменение настроек истории данных",
    "UpdateDataHistoryVersionComment": "Изменение комментария истории данных",
    "Use": "Использование",
    "View": "Интерактивный просмотр",
    "ViewDataHistory": "Просмотр истории данных",
    "WebClient": "Веб-клиент",
}


class RoleAccessQueryError(ValueError):
    """Публичная ошибка параметров или точного адреса role-запроса."""


@dataclass(frozen=True, slots=True)
class _Selection:
    configuration: str
    roles: LoadedRoleAccess | None


def _bounded(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _token(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _untoken(value: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value or len(value) > MAX_CURSOR_CHARS:
        raise RoleAccessQueryError("cursor имеет неверный формат")
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        payload = json.loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise RoleAccessQueryError("cursor имеет неверный формат") from error
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise RoleAccessQueryError("cursor имеет неизвестную версию")
    return payload


def _fingerprint(*values: object) -> str:
    raw = json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _cursor(
    kind: str,
    generation: str,
    query: str,
    offset: int | None,
) -> str | None:
    if offset is None:
        return None
    return _token(
        {"v": 1, "kind": kind, "generation": generation, "query": query, "offset": offset}
    )


def _cursor_offset(
    value: str | None,
    *,
    kind: str,
    generation: str,
    query: str,
) -> int:
    if value is None:
        return 0
    payload = _untoken(value)
    if (
        payload.get("kind") != kind
        or payload.get("generation") != generation
        or payload.get("query") != query
        or isinstance(payload.get("offset"), bool)
        or not isinstance(payload.get("offset"), int)
        or payload["offset"] < 0
    ):
        raise RoleAccessQueryError("cursor относится к другому запросу или поколению")
    return int(payload["offset"])


def _reference(
    generation: str,
    role: str,
    kind: str,
    reference_id: int,
) -> str:
    return _token(
        {
            "v": 1,
            "kind": "role-text-ref",
            "generation": generation,
            "role": role,
            "text_kind": kind,
            "id": reference_id,
        }
    )


def _decode_reference(
    value: str,
    *,
    generation: str,
    role: str,
) -> tuple[str, int]:
    payload = _untoken(value)
    kind = payload.get("text_kind")
    reference_id = payload.get("id")
    if (
        payload.get("kind") != "role-text-ref"
        or payload.get("generation") != generation
        or payload.get("role") != role
        or kind not in {"restriction", "template"}
        or isinstance(reference_id, bool)
        or not isinstance(reference_id, int)
        or reference_id < 1
    ):
        raise RoleAccessQueryError(
            "restriction_ref относится к другой роли или generation"
        )
    return str(kind), reference_id


def _selection(registry: Registry, config: str | None) -> _Selection:
    context = registry.resolve(config)
    return _Selection(context.name, context.roles)


def _state_payload(selection: _Selection) -> dict[str, Any]:
    roles = selection.roles
    if roles is None:
        return {
            "api_version": API_VERSION,
            "state": "missing",
            "configuration": selection.configuration,
            "generation": None,
            "source_sha256": None,
            "declaration_scope": DECLARATION_SCOPE,
            "disclaimer": DISCLAIMER,
            "message": "Для конфигурации нет готового снимка объявленных прав ролей.",
        }
    if not roles.ready:
        message, truncated = _bounded(roles.error, MAX_COMMENT_CHARS)
        return {
            "api_version": API_VERSION,
            "state": "error",
            "configuration": selection.configuration,
            "generation": roles.generation_id,
            "source_sha256": roles.source_sha256,
            "declaration_scope": DECLARATION_SCOPE,
            "disclaimer": DISCLAIMER,
            "message": message or "Снимок объявленных прав ролей недоступен.",
            "message_truncated": truncated,
        }
    return {
        "api_version": API_VERSION,
        "state": "ready",
        "configuration": selection.configuration,
        "generation": roles.generation_id,
        "source_sha256": roles.source_sha256,
        "declaration_scope": DECLARATION_SCOPE,
        "disclaimer": DISCLAIMER,
    }


def _ready(selection: _Selection) -> tuple[dict[str, Any], LoadedRoleAccess, RoleAccessIndex] | dict[str, Any]:
    state = _state_payload(selection)
    roles = selection.roles
    if roles is None or not roles.ready or roles.index is None:
        return state
    return state, roles, roles.index


def _descriptor(role: RoleDescriptor) -> dict[str, Any]:
    comment, truncated = _bounded(role.comment, MAX_COMMENT_CHARS)
    label_ru = next(
        (
            content
            for language, content in role.synonyms
            if language.casefold().startswith("ru") and content
        ),
        role.name,
    )
    return {
        "uuid": role.uuid,
        "name": role.name,
        "label_ru": label_ru,
        "synonyms": [
            {"language": language, "content": content}
            for language, content in role.synonyms
        ],
        "comment": comment,
        "comment_truncated": truncated,
        "xml_version": role.xml_version,
        "default_flags": {
            "set_for_new_objects": role.set_for_new_objects,
            "set_for_attributes_by_default": role.set_for_attributes_by_default,
            "independent_rights_of_child_objects": (
                role.independent_rights_of_child_objects
            ),
            "resolver_effect": "evidence_only",
        },
    }


def _right_state(right: DeclaredRight) -> str:
    if not right.value:
        return "explicit_false"
    if right.conditional:
        return "conditional_true"
    return "unconditional_true"


def _right_channel(name: str) -> str:
    if name in {"Read", "Update", "Insert", "Delete", "Posting", "UndoPosting"}:
        return "programmatic"
    if name in {"View", "Edit", "InputByString"} or name.startswith("Interactive"):
        return "interactive"
    return "platform"


def _right_payload(
    right: DeclaredRight,
    *,
    roles: LoadedRoleAccess,
    role: str,
) -> dict[str, Any]:
    restrictions = [
        {
            "fields": list(item.fields),
            "chars": item.chars,
            "bytes": item.bytes,
            "ref": _reference(
                roles.generation_id,
                role,
                "restriction",
                item.id,
            ),
        }
        for item in right.restriction_refs
    ]
    payload: dict[str, Any] = {
        "target": right.target,
        "name": right.name,
        "label_ru": _RIGHT_LABELS.get(right.name, right.name),
        "channel": _right_channel(right.name),
        "value": right.value,
        "state": _right_state(right),
        "has_rls": right.conditional,
        "rls_detail_available": bool(restrictions),
        "restrictions": restrictions,
    }
    target = _target_payload(right.target)
    for key in ("child_path", "child_kind", "child_kind_ru", "child_name"):
        if key in target:
            payload[key] = target[key]
    if right.conditional:
        payload["next_action"] = (
            "Запросите условие отдельно через restriction_ref."
        )
    return payload


def _target_payload(target: str) -> dict[str, Any]:
    parts = target.split(".")
    kind = parts[0] if parts else ""
    name = parts[1] if len(parts) > 1 else ""
    canonical_kind, kind_ru = _SOURCE_KIND_PRESENTATION.get(kind, (kind, kind))
    payload: dict[str, Any] = {
        "target": target,
        "full_name": f"{canonical_kind}.{name}" if name else target,
        "kind": kind,
        "kind_ru": kind_ru,
        "name": name,
    }
    if len(parts) > 2:
        child_path = parts[2:]
        leaf_kind = child_path[-2] if len(child_path) > 1 else child_path[0]
        payload.update(
            {
                "child_path": ".".join(child_path),
                "child_kind": leaf_kind,
                "child_kind_ru": _CHILD_KIND_LABELS.get(
                    leaf_kind,
                    leaf_kind,
                ),
                "child_name": child_path[-1] if len(child_path) > 1 else "",
            }
        )
    return payload


def _operation_checks(rights: tuple[DeclaredRight, ...]) -> list[dict[str, Any]]:
    by_name = {right.name.casefold(): right for right in rights}
    checks: list[dict[str, Any]] = []
    for operation, platform_right in OPERATION_RIGHTS.items():
        label_ru, channel = OPERATION_PRESENTATION[operation]
        right = by_name.get(platform_right.casefold())
        granted = bool(right is not None and right.value)
        if granted:
            state = "conditional" if right and right.conditional else "unconditional"
            evidence = "explicit_true"
        else:
            state = "not_granted"
            evidence = "explicit_false" if right is not None else "not_declared"
        row = {
            "operation": operation,
            "label_ru": label_ru,
            "channel": channel,
            "platform_right": platform_right,
            "granted": granted,
            "state": state,
            "evidence": evidence,
            "has_rls": bool(granted and right and right.conditional),
            "rls_detail_available": bool(
                granted and right and right.restriction_refs
            ),
        }
        if row["has_rls"]:
            row["next_action"] = (
                "Откройте restriction_ref соответствующего root_right."
            )
        checks.append(row)
    return checks


def _candidate(candidate: RoleCandidate) -> dict[str, Any]:
    has_rls = bool(candidate.conditional_operations)
    payload = {
        "role": _descriptor(candidate.role),
        "complete": candidate.complete,
        "matched_operations": list(candidate.matched_operations),
        "missing_operations": list(candidate.missing_operations),
        "conditional_operations": list(candidate.conditional_operations),
        "denied_operations": list(candidate.denied_operations),
        "matched_rights": [
            {
                "target": right.target,
                "name": right.name,
                "label_ru": _RIGHT_LABELS.get(right.name, right.name),
                "channel": _right_channel(right.name),
                "value": right.value,
                "state": _right_state(right),
            }
            for right in candidate.matched_rights
        ],
        "has_rls": has_rls,
        "rls_detail_available": has_rls,
    }
    if has_rls:
        payload["next_action"] = (
            "Вызовите get_role_access для роли и объекта; затем запросите "
            "условие отдельно через restriction_ref."
        )
    return payload


def _operation_rows(operations: Iterable[str]) -> list[dict[str, str]]:
    return [
        {
            "operation": operation,
            "label_ru": OPERATION_PRESENTATION[operation][0],
            "channel": OPERATION_PRESENTATION[operation][1],
            "platform_right": OPERATION_RIGHTS[operation],
        }
        for operation in operations
    ]


def _object_summary_payload(
    index: RoleAccessIndex,
    roles: LoadedRoleAccess,
    role: str,
    target: str,
    aggregate: RoleObjectSummary | None,
    *,
    include_checks: bool,
) -> dict[str, Any]:
    if include_checks:
        root_page = index.role_access(role, target=target, limit=200)
        all_root_rights = root_page.rights
    else:
        all_root_rights = aggregate.root_rights if aggregate else ()
    root_rights = tuple(right for right in all_root_rights if right.value)
    conditional_grants = aggregate.conditional_grants if aggregate else 0
    descendants = {
        "targets_with_grants": aggregate.descendant_targets if aggregate else 0,
        "granted_rights": aggregate.descendant_grants if aggregate else 0,
        "conditional_rights": 0,
        "detail_available": bool(aggregate and aggregate.descendant_grants),
    }
    if aggregate:
        root_conditional = sum(1 for right in root_rights if right.conditional)
        descendants["conditional_rights"] = max(
            aggregate.conditional_grants - root_conditional,
            0,
        )
    payload = {
        **_target_payload(target),
        "root_rights": [
            _right_payload(right, roles=roles, role=role) for right in root_rights
        ],
        "descendants": descendants,
        "has_rls": bool(conditional_grants),
        "rls_detail_available": bool(conditional_grants),
    }
    if conditional_grants:
        if any(right.conditional for right in root_rights):
            payload["next_action"] = (
                "Откройте restriction_ref условного права; дочерние условия "
                "доступны через detail=children."
            )
        else:
            payload["next_action"] = (
                "Запросите get_role_access с detail=children, затем откройте "
                "условие через restriction_ref."
            )
    if include_checks:
        payload["operation_checks"] = _operation_checks(all_root_rights)
    return payload


def find_roles_payload(
    registry: Registry,
    full_name: str,
    operations: list[str] | tuple[str, ...],
    *,
    config: str | None = None,
    child_path: str = "",
    include_conditional: bool = False,
    cursor: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    if not isinstance(operations, (list, tuple)) or not operations or not all(
        isinstance(operation, str) for operation in operations
    ):
        raise RoleAccessQueryError("operations должен быть непустым списком строк")
    normalized = tuple(operation.casefold() for operation in operations)
    if len(normalized) > len(OPERATION_RIGHTS):
        raise RoleAccessQueryError("operations содержит слишком много значений")
    if not isinstance(include_conditional, bool):
        raise RoleAccessQueryError("include_conditional должен быть boolean")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_FIND_LIMIT:
        raise RoleAccessQueryError(f"limit должен быть от 1 до {MAX_FIND_LIMIT}")
    selection = _selection(registry, config)
    selected = _ready(selection)
    if isinstance(selected, dict):
        return selected
    state, roles, index = selected
    query = _fingerprint(
        selection.configuration,
        full_name,
        normalized,
        child_path,
        include_conditional,
    )
    offset = _cursor_offset(
        cursor,
        kind="role-candidates",
        generation=roles.generation_id,
        query=query,
    )
    try:
        resolution = index.find_roles_for_access(
            full_name,
            normalized,
            child_path=child_path,
            include_conditional=include_conditional,
            offset=offset,
            limit=limit,
        )
    except ValueError as error:
        raise RoleAccessQueryError(str(error)) from error
    minimum = None
    if resolution.minimal_role_set and resolution.minimum_proof:
        minimum = {
            "roles": list(resolution.minimal_role_set),
            "proof": resolution.minimum_proof,
        }
    return {
        **state,
        "source_target": resolution.source_target,
        "checked_rights": _operation_rows(
            operation for operation, _right in resolution.checked_rights
        ),
        "include_conditional": include_conditional,
        "conditional_candidates_excluded": (
            resolution.conditional_candidates_excluded
        ),
        "candidates_total": resolution.candidates_total,
        "candidates": [_candidate(candidate) for candidate in resolution.candidates],
        "minimal_role_set": minimum,
        "warnings": list(resolution.warnings),
        "page": {
            "offset": resolution.offset,
            "limit": limit,
            "returned": len(resolution.candidates),
            "next_cursor": _cursor(
                "role-candidates",
                roles.generation_id,
                query,
                resolution.next_offset,
            ),
        },
    }


def _text_payload(
    state: dict[str, Any],
    roles: LoadedRoleAccess,
    index: RoleAccessIndex,
    *,
    role: str,
    restriction_ref: str,
    restriction_cursor: str | None,
    max_chars: int,
) -> dict[str, Any]:
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not MIN_PAGE_CHARS <= max_chars <= MAX_PAGE_CHARS
    ):
        raise RoleAccessQueryError(
            f"max_chars должен быть от {MIN_PAGE_CHARS} до {MAX_PAGE_CHARS}"
        )
    kind, reference_id = _decode_reference(
        restriction_ref,
        generation=roles.generation_id,
        role=role,
    )
    query = _fingerprint(role, restriction_ref)
    offset = _cursor_offset(
        restriction_cursor,
        kind="role-text",
        generation=roles.generation_id,
        query=query,
    )
    try:
        page = index.read_text(
            role,
            kind,
            reference_id,
            offset=offset,
            max_chars=max_chars,
        )
    except KeyError as error:
        raise RoleAccessQueryError("Условие роли не найдено") from error
    except ValueError as error:
        raise RoleAccessQueryError(str(error)) from error
    return {
        **state,
        "mode": page.kind,
        "role": page.role,
        "restriction_ref": restriction_ref,
        "fields": list(page.fields),
        "template": page.template,
        "target": page.target,
        "right": page.right,
        "content": page.content,
        "total_chars": page.total_chars,
        "total_bytes": page.total_bytes,
        "page": {
            "offset": page.offset,
            "max_chars": max_chars,
            "returned_chars": len(page.content),
            "next_cursor": _cursor(
                "role-text",
                roles.generation_id,
                query,
                page.next_offset,
            ),
        },
    }


def get_role_access_payload(
    registry: Registry,
    role: str,
    *,
    config: str | None = None,
    full_name: str = "",
    detail: str = "summary",
    cursor: str | None = None,
    limit: int = 50,
    restriction_ref: str = "",
    restriction_cursor: str | None = None,
    max_chars: int = DEFAULT_PAGE_CHARS,
) -> dict[str, Any]:
    if not isinstance(role, str) or not role or len(role) > MAX_NAME_CHARS:
        raise RoleAccessQueryError("role должен быть непустым точным именем")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ACCESS_LIMIT:
        raise RoleAccessQueryError(f"limit должен быть от 1 до {MAX_ACCESS_LIMIT}")
    if detail not in {"summary", "children", "audit"}:
        raise RoleAccessQueryError("detail должен быть summary|children|audit")
    selection = _selection(registry, config)
    selected = _ready(selection)
    if isinstance(selected, dict):
        return selected
    state, roles, index = selected
    if restriction_ref:
        if cursor or full_name:
            raise RoleAccessQueryError(
                "restriction_ref нельзя смешивать с cursor или full_name"
            )
        return _text_payload(
            state,
            roles,
            index,
            role=role,
            restriction_ref=restriction_ref,
            restriction_cursor=restriction_cursor,
            max_chars=max_chars,
        )
    if restriction_cursor:
        raise RoleAccessQueryError(
            "restriction_cursor требует restriction_ref"
        )
    target = ""
    if full_name:
        try:
            target = source_target(full_name)
        except ValueError as error:
            raise RoleAccessQueryError(str(error)) from error
    if detail in {"children", "audit"} and not target:
        raise RoleAccessQueryError(f"detail={detail} требует full_name")
    query = _fingerprint(selection.configuration, role, target, detail)
    offset = 0
    template_offset: int | None = None
    if cursor:
        decoded = _untoken(cursor)
        if decoded.get("kind") == "role-templates":
            template_offset = _cursor_offset(
                cursor,
                kind="role-templates",
                generation=roles.generation_id,
                query=query,
            )
        else:
            cursor_kind = {
                "summary": "role-objects",
                "children": "role-children",
                "audit": "role-audit",
            }[detail]
            offset = _cursor_offset(
                cursor,
                kind=cursor_kind,
                generation=roles.generation_id,
                query=query,
            )
    try:
        descriptor = index.get_role(role)
        if template_offset is not None:
            templates = index.role_templates(
                descriptor.name,
                offset=template_offset,
                limit=min(limit, 20),
            )
            return {
                **state,
                "mode": "templates",
                "role": _descriptor(descriptor),
                "templates_total": templates.total,
                "templates": [
                    {
                        "name": item.name,
                        "chars": item.chars,
                        "bytes": item.bytes,
                        "ref": _reference(
                            roles.generation_id,
                            descriptor.name,
                            "template",
                            item.id,
                        ),
                    }
                    for item in templates.templates
                ],
                "page": {
                    "offset": templates.offset,
                    "limit": min(limit, 20),
                    "returned": len(templates.templates),
                    "next_cursor": _cursor(
                        "role-templates",
                        roles.generation_id,
                        query,
                        templates.next_offset,
                    ),
                },
            }
        if detail == "summary":
            objects_page = index.role_objects(
                descriptor.name,
                target=target,
                offset=0 if target else offset,
                limit=1 if target else limit,
            )
            templates = index.role_templates(descriptor.name, offset=0, limit=20)
        else:
            page = index.role_access(
                descriptor.name,
                target=target,
                subtree=True,
                include_root=detail == "audit",
                only_granted=detail == "children",
                offset=offset,
                limit=limit,
            )
    except KeyError as error:
        raise RoleAccessQueryError("Роль не найдена") from error
    except ValueError as error:
        raise RoleAccessQueryError(str(error)) from error

    if detail != "summary":
        cursor_kind = "role-children" if detail == "children" else "role-audit"
        return {
            **state,
            "mode": detail,
            "role": _descriptor(page.role),
            "object": _target_payload(target),
            "rights_total": page.total,
            "rights": [
                _right_payload(right, roles=roles, role=page.role.name)
                for right in page.rights
            ],
            "page": {
                "offset": page.offset,
                "limit": limit,
                "returned": len(page.rights),
                "next_cursor": _cursor(
                    cursor_kind,
                    roles.generation_id,
                    query,
                    page.next_offset,
                ),
            },
        }

    aggregates = {item.target.casefold(): item for item in objects_page.objects}
    selected_targets = [target] if target else [
        item.target for item in objects_page.objects
    ]
    objects = [
        _object_summary_payload(
            index,
            roles,
            descriptor.name,
            item,
            aggregates.get(item.casefold()),
            include_checks=bool(target),
        )
        for item in selected_targets
    ]
    objects_total = 1 if target else objects_page.total
    return {
        **state,
        "mode": "objects",
        "role": _descriptor(descriptor),
        "target": target or None,
        "objects_total": objects_total,
        "objects": objects,
        "templates_total": templates.total,
        "templates": [
            {
                "name": item.name,
                "chars": item.chars,
                "bytes": item.bytes,
                "ref": _reference(
                    roles.generation_id,
                    descriptor.name,
                    "template",
                    item.id,
                ),
            }
            for item in templates.templates
        ],
        "page": {
            "offset": 0 if target else objects_page.offset,
            "limit": 1 if target else limit,
            "returned": len(objects),
            "next_cursor": _cursor(
                "role-objects",
                roles.generation_id,
                query,
                None if target else objects_page.next_offset,
            ),
        },
        "templates_page": {
            "offset": templates.offset,
            "limit": 20,
            "returned": len(templates.templates),
            "next_cursor": _cursor(
                "role-templates",
                roles.generation_id,
                query,
                templates.next_offset,
            ),
        },
    }


def role_objects_payload(
    registry: Registry,
    role: str,
    *,
    config: str | None = None,
    kind: str = "",
    query: str = "",
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Навигационная проекция объектов роли для SPA, не меняющая MCP-схему."""
    if not isinstance(role, str) or not role or len(role) > MAX_NAME_CHARS:
        raise RoleAccessQueryError("role должен быть непустым точным именем")
    if not isinstance(kind, str) or len(kind) > MAX_NAME_CHARS:
        raise RoleAccessQueryError("kind имеет неверный формат")
    if not isinstance(query, str) or len(query) > MAX_OBJECT_QUERY_CHARS:
        raise RoleAccessQueryError(
            f"query должен содержать не более {MAX_OBJECT_QUERY_CHARS} символов"
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ACCESS_LIMIT:
        raise RoleAccessQueryError(f"limit должен быть от 1 до {MAX_ACCESS_LIMIT}")
    normalized_kind = kind.strip()
    normalized_query = " ".join(query.split())
    selection = _selection(registry, config)
    selected = _ready(selection)
    if isinstance(selected, dict):
        return selected
    state, roles, index = selected
    cursor_query = _fingerprint(
        selection.configuration,
        role,
        normalized_kind.casefold(),
        normalized_query.casefold(),
        "role-object-navigation",
    )
    offset = _cursor_offset(
        cursor,
        kind="role-object-navigation",
        generation=roles.generation_id,
        query=cursor_query,
    )
    try:
        descriptor = index.get_role(role)
        facets = index.role_object_facets(descriptor.name)
        page = index.role_objects(
            descriptor.name,
            kind=normalized_kind,
            query=normalized_query,
            offset=offset,
            limit=limit,
        )
    except KeyError as error:
        raise RoleAccessQueryError("Роль не найдена") from error
    except ValueError as error:
        raise RoleAccessQueryError(str(error)) from error
    return {
        **state,
        "mode": "objects",
        "role": _descriptor(descriptor),
        "object_filters": {
            "kind": normalized_kind,
            "query": normalized_query,
        },
        "objects_all_total": sum(total for _, total in facets),
        "objects_total": page.total,
        "object_facets": [
            {
                "kind": source_kind,
                "kind_ru": _SOURCE_KIND_PRESENTATION.get(
                    source_kind,
                    (source_kind, source_kind),
                )[1],
                "count": total,
            }
            for source_kind, total in facets
        ],
        "objects": [
            _object_summary_payload(
                index,
                roles,
                descriptor.name,
                item.target,
                item,
                include_checks=False,
            )
            for item in page.objects
        ],
        "page": {
            "offset": page.offset,
            "limit": limit,
            "returned": len(page.objects),
            "next_cursor": _cursor(
                "role-object-navigation",
                roles.generation_id,
                cursor_query,
                page.next_offset,
            ),
        },
    }


def roles_catalog_payload(
    registry: Registry,
    *,
    config: str | None = None,
    query: str = "",
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_ROLE_LIST_LIMIT
    ):
        raise RoleAccessQueryError(
            f"limit должен быть от 1 до {MAX_ROLE_LIST_LIMIT}"
        )
    if not isinstance(query, str) or len(query) > MAX_ROLE_QUERY_CHARS:
        raise RoleAccessQueryError(
            f"query должен содержать не более {MAX_ROLE_QUERY_CHARS} символов"
        )
    normalized_query = " ".join(query.split())
    names = list(registry.snapshot().configuration_names)
    if not names:
        return {
            "api_version": API_VERSION,
            "state": "missing",
            "configuration": None,
            "configuration_names": [],
            "generation": None,
            "source_sha256": None,
            "declaration_scope": DECLARATION_SCOPE,
            "disclaimer": DISCLAIMER,
            "message": "Не загружено ни одной конфигурации.",
        }
    if not config:
        if len(names) > 1:
            return {
                "api_version": API_VERSION,
                "state": "selection_required",
                "configuration": None,
                "configuration_names": names,
                "generation": None,
                "source_sha256": None,
                "declaration_scope": DECLARATION_SCOPE,
                "disclaimer": DISCLAIMER,
                "message": "Выберите конфигурацию.",
            }
        config = names[0]
    selection = _selection(registry, config)
    selected = _ready(selection)
    if isinstance(selected, dict):
        return {**selected, "configuration_names": names}
    state, roles, index = selected
    cursor_query = _fingerprint(
        selection.configuration,
        normalized_query.casefold(),
        "roles",
    )
    offset = _cursor_offset(
        cursor,
        kind="role-list",
        generation=roles.generation_id,
        query=cursor_query,
    )
    page, matched = index.search_roles(
        normalized_query,
        offset=offset,
        limit=limit,
    )
    next_offset = offset + len(page)
    if next_offset >= matched:
        next_offset = None
    return {
        **state,
        "configuration_names": names,
        "operations": _operation_rows(OPERATION_RIGHTS),
        "roles_total": index.summary.roles,
        "roles_matched": matched,
        "role_query": normalized_query,
        "roles": [_descriptor(role) for role in page],
        "page": {
            "offset": offset,
            "limit": limit,
            "returned": len(page),
            "next_cursor": _cursor(
                "role-list",
                roles.generation_id,
                cursor_query,
                next_offset,
            ),
        },
    }


def http_status(payload: dict[str, Any]) -> int:
    return 200 if payload.get("state") == "ready" else 409
