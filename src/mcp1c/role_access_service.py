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
    OPERATION_RIGHTS,
    DeclaredRight,
    LoadedRoleAccess,
    RoleAccessIndex,
    RoleCandidate,
    RoleDescriptor,
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
MAX_CURSOR_CHARS = 2048
MAX_NAME_CHARS = 512
MAX_COMMENT_CHARS = 2048


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
    return {
        "uuid": role.uuid,
        "name": role.name,
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


def _candidate(candidate: RoleCandidate) -> dict[str, Any]:
    return {
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
                "value": right.value,
                "state": _right_state(right),
            }
            for right in candidate.matched_rights
        ],
    }


def _operation_rows(operations: Iterable[str]) -> list[dict[str, str]]:
    return [
        {"operation": operation, "platform_right": OPERATION_RIGHTS[operation]}
        for operation in operations
    ]


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
    query = _fingerprint(selection.configuration, role, target)
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
            offset = _cursor_offset(
                cursor,
                kind="role-rights",
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
        page = index.role_access(
            descriptor.name,
            target=target,
            offset=offset,
            limit=limit,
        )
        templates = index.role_templates(descriptor.name, offset=0, limit=20)
    except KeyError as error:
        raise RoleAccessQueryError("Роль не найдена") from error
    except ValueError as error:
        raise RoleAccessQueryError(str(error)) from error
    return {
        **state,
        "mode": "rights",
        "role": _descriptor(page.role),
        "target": target or None,
        "rights_total": page.total,
        "rights": [
            {
                "target": right.target,
                "name": right.name,
                "value": right.value,
                "state": _right_state(right),
                "restrictions": [
                    {
                        "fields": list(item.fields),
                        "chars": item.chars,
                        "bytes": item.bytes,
                        "ref": _reference(
                            roles.generation_id,
                            page.role.name,
                            "restriction",
                            item.id,
                        ),
                    }
                    for item in right.restriction_refs
                ],
            }
            for right in page.rights
        ],
        "templates_total": templates.total,
        "templates": [
            {
                "name": item.name,
                "chars": item.chars,
                "bytes": item.bytes,
                "ref": _reference(
                    roles.generation_id,
                    page.role.name,
                    "template",
                    item.id,
                ),
            }
            for item in templates.templates
        ],
        "page": {
            "offset": page.offset,
            "limit": limit,
            "returned": len(page.rights),
            "next_cursor": _cursor(
                "role-rights",
                roles.generation_id,
                query,
                page.next_offset,
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


def roles_catalog_payload(
    registry: Registry,
    *,
    config: str | None = None,
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
    query = _fingerprint(selection.configuration, "roles")
    offset = _cursor_offset(
        cursor,
        kind="role-list",
        generation=roles.generation_id,
        query=query,
    )
    page = index.list_roles(offset=offset, limit=limit)
    next_offset = offset + len(page)
    if next_offset >= index.summary.roles:
        next_offset = None
    return {
        **state,
        "configuration_names": names,
        "operations": _operation_rows(OPERATION_RIGHTS),
        "roles_total": index.summary.roles,
        "roles": [_descriptor(role) for role in page],
        "page": {
            "offset": offset,
            "limit": limit,
            "returned": len(page),
            "next_cursor": _cursor(
                "role-list",
                roles.generation_id,
                query,
                next_offset,
            ),
        },
    }


def http_status(payload: dict[str, Any]) -> int:
    return 200 if payload.get("state") == "ready" else 409
