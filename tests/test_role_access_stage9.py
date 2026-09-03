"""RED-контракты MCP и read-only API объявленных прав ролей.

Все поколения и XML синтетические. Обычный ответ никогда не несёт текст RLS:
условие читается отдельно ограниченными страницами и собирается без потерь.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.subscriptions import ToolsListChanged
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.applications import Starlette

from conftest import build_configuration, write_export, живой_клиент
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry
from mcp1c.role_access import RoleAccessIndex
from mcp1c.server import build_server
from test_role_access_index import (
    RIGHTS_NS,
    _descriptor,
    _generation,
    _right,
    _rights,
)


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _roles(
    condition: str = "Allowed = true",
    fields: tuple[str, ...] = (),
):
    return (
        (
            "Reader",
            _descriptor(
                "Reader",
                uuid="11111111-1111-1111-1111-111111111111",
                comment="Синтетическая роль чтения",
                synonyms=(("ru", "Чтение"),),
            ),
            _rights(
                ((
                    "Catalog.Orders",
                    (_right("Read", True), _right("Update", False)),
                ),),
                set_for_new=True,
                set_for_attributes=False,
            ),
        ),
        (
            "Editor",
            _descriptor(
                "Editor",
                uuid="22222222-2222-2222-2222-222222222222",
            ),
            _rights((("Catalog.Orders", (_right("Update", True),)),)),
        ),
        (
            "Conditional",
            _descriptor(
                "Conditional",
                uuid="33333333-3333-3333-3333-333333333333",
            ),
            _rights(
                ((
                    "Catalog.Orders",
                    (
                        _right("Read", True, condition=condition, fields=fields),
                        _right("Update", True, condition=condition, fields=fields),
                    ),
                ),),
                templates=(("SyntheticTemplate", condition),),
            ),
        ),
        (
            "Empty",
            _descriptor(
                "Empty",
                uuid="44444444-4444-4444-4444-444444444444",
            ),
            _rights(),
        ),
    )


def _summary_roles():
    return ((
        "CompactReader",
        _descriptor(
            "CompactReader",
            uuid="66666666-6666-6666-6666-666666666666",
            comment="Синтетическая компактная роль",
            synonyms=(("en", "Compact reader"), ("ru", "Компактное чтение")),
        ),
        _rights((
            (
                "Catalog.Orders",
                (
                    _right("Read", True),
                    _right("View", True),
                    _right("Update", False),
                    _right("Edit", False),
                ),
            ),
            (
                "Catalog.Orders.Attribute.Code",
                (_right("Read", True), _right("View", False)),
            ),
            (
                "Catalog.Orders.Command.Open",
                (_right("Use", True, condition="Allowed = true"),),
            ),
            (
                "Catalog.Orders.TabularSection.Items.Attribute.Price",
                (_right("Read", True),),
            ),
            (
                "Catalog.Hidden",
                (_right("Read", False), _right("View", False)),
            ),
        )),
    ),)


def _navigation_roles():
    return (
        (
            "Reader",
            _descriptor(
                "Reader",
                uuid="71111111-1111-1111-1111-111111111111",
                synonyms=(("ru", "Базовое чтение"),),
            ),
            _rights((
                ("Catalog.Orders", (_right("Read", True),)),
                ("Document.Invoice", (_right("Read", True),)),
                (
                    "Document.Invoice.Attribute.Number",
                    (_right("Read", True),),
                ),
                ("Document.Act", (_right("View", True),)),
                ("InformationRegister.Stock", (_right("Read", True),)),
            )),
        ),
        (
            "Administrator",
            _descriptor(
                "Administrator",
                uuid="72222222-2222-2222-2222-222222222222",
                synonyms=(("en", "Administration"), ("ru", "Администратор")),
            ),
            _rights((("Catalog.Orders", (_right("Update", True),)),)),
        ),
        (
            "AccountingAdministrator",
            _descriptor(
                "AccountingAdministrator",
                uuid="73333333-3333-3333-3333-333333333333",
                synonyms=(("ru", "Администратор учёта"),),
            ),
            _rights((("Document.Invoice", (_right("Update", True),)),)),
        ),
    )


def _publish(
    registry: Registry,
    root,
    *,
    configuration: str,
    generation_id: str,
    roles=None,
):
    manifest, payloads = _generation(
        root,
        roles or _roles(),
        generation_id=generation_id,
        configuration_name=configuration,
    )
    registry.publish_generation(registry.stage_generation(manifest, payloads))


def _add_missing_configuration(registry: Registry, root, name: str) -> None:
    root.mkdir(parents=True)
    registry.add_configuration(
        write_export(root, build_configuration(name=name))
    )


def _add_error_configuration(registry: Registry, root, name: str) -> None:
    malformed = (
        f'<Rights xmlns="{RIGHTS_NS}" version="2.20">'
        "<object><name>Catalog.Orders</name>"
        f"{_right('Read', True)}</object></Rights>"
    ).encode()
    _publish(
        registry,
        root,
        configuration=name,
        generation_id="generation-error",
        roles=((
            "Broken",
            _descriptor(
                "Broken",
                uuid="55555555-5555-5555-5555-555555555555",
            ),
            malformed,
        ),),
    )


def _server(registry: Registry, root):
    reference = ReferenceService.discover(root / "reference-missing")
    return build_server(registry, reference=reference)


def _tool_json(result) -> dict:
    assert result.is_error is False
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


def _client(registry: Registry, root):
    reference = ReferenceService.discover(root / "reference-missing")
    return живой_клиент(
        Starlette(
            routes=routes(
                registry,
                mode=DASHBOARD_ON,
                reference=reference,
            )
        )
    )


async def test_role_tools_отсутствуют_без_ready_и_появляются_только_с_ready(
    tmp_path,
):
    missing = Registry(tmp_path / "missing-data")
    _add_missing_configuration(missing, tmp_path / "missing-source", "NoRoles")
    missing_names = [
        tool.name for tool in await _server(missing, tmp_path / "missing").list_tools()
    ]

    failed = Registry(tmp_path / "failed-data")
    _add_error_configuration(
        failed,
        tmp_path / "failed-source",
        "BrokenRoles",
    )
    failed_names = [
        tool.name for tool in await _server(failed, tmp_path / "failed").list_tools()
    ]

    ready = Registry(tmp_path / "ready-data")
    _publish(
        ready,
        tmp_path / "ready-source",
        configuration="DemoConfiguration",
        generation_id="generation-1",
    )
    ready_tools = await _server(ready, tmp_path / "ready").list_tools()
    ready_names = [tool.name for tool in ready_tools]

    assert "find_roles_for_access" not in missing_names
    assert "get_role_access" not in missing_names
    assert "find_roles_for_access" not in failed_names
    assert "get_role_access" not in failed_names
    assert ready_names[-2:] == ["find_roles_for_access", "get_role_access"]

    find = next(tool for tool in ready_tools if tool.name == "find_roles_for_access")
    find_fields = find.input_schema["properties"]
    assert set(find_fields) == {
        "full_name",
        "operations",
        "config",
        "child_path",
        "include_conditional",
        "cursor",
        "limit",
    }
    assert find_fields["operations"]["minItems"] == 1
    assert find_fields["operations"]["maxItems"] == 16
    assert find_fields["limit"]["maximum"] == 20
    assert "объявлен" in (find.description or "").lower()
    assert "эффектив" in (find.description or "").lower()

    get = next(tool for tool in ready_tools if tool.name == "get_role_access")
    get_fields = get.input_schema["properties"]
    assert set(get_fields) == {
        "role",
        "config",
        "full_name",
        "detail",
        "cursor",
        "limit",
        "restriction_ref",
        "restriction_cursor",
        "max_chars",
    }
    assert get_fields["limit"]["maximum"] == 100
    assert get_fields["max_chars"]["maximum"] == 8000
    assert "rls" in (get.description or "").lower()
    role_schema_bytes = len(
        json.dumps(
            [
                tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                for tool in ready_tools[-2:]
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert role_schema_bytes <= 7_500


async def test_role_catalog_обновляется_без_restart_и_посылает_list_changed(
    tmp_path,
):
    registry = Registry(tmp_path / "data")
    _add_missing_configuration(
        registry,
        tmp_path / "legacy-source",
        "DemoConfiguration",
    )
    server = _server(registry, tmp_path)
    events = []
    unsubscribe = server._role_subscriptions.subscribe(events.append)
    try:
        before = {tool.name for tool in await server.list_tools()}
        assert not before & {"find_roles_for_access", "get_role_access"}

        _publish(
            registry,
            tmp_path / "generation-source",
            configuration="DemoConfiguration",
            generation_id="generation-1",
        )
        assert await server.refresh_role_tools() is True
        ready = {tool.name for tool in await server.list_tools()}
        assert ready & {"find_roles_for_access", "get_role_access"} == {
            "find_roles_for_access",
            "get_role_access",
        }
        assert events == [ToolsListChanged()]

        _add_error_configuration(
            registry,
            tmp_path / "error-generation-source",
            "DemoConfiguration",
        )
        assert await server.refresh_role_tools() is True
        after = {tool.name for tool in await server.list_tools()}
        assert not after & {"find_roles_for_access", "get_role_access"}
        assert events == [ToolsListChanged(), ToolsListChanged()]
        assert await server.refresh_role_tools() is False
        assert events == [ToolsListChanged(), ToolsListChanged()]
    finally:
        unsubscribe()

    capabilities = server._lowlevel_server.get_capabilities(
        protocol_version=LATEST_PROTOCOL_VERSION
    )
    assert capabilities.tools is not None
    assert capabilities.tools.list_changed is True


async def test_find_mcp_и_api_дают_один_resolver_и_страницу_кандидатов(
    tmp_path,
):
    registry = Registry(tmp_path / "data")
    _publish(
        registry,
        tmp_path / "source",
        configuration="DemoConfiguration",
        generation_id="generation-1",
    )
    server = _server(registry, tmp_path)
    arguments = {
        "config": "DemoConfiguration",
        "full_name": "Справочник.Orders",
        "operations": ["read", "update"],
        "limit": 1,
    }

    mcp = _tool_json(await server.call_tool("find_roles_for_access", arguments))
    api_response = _client(registry, tmp_path).get(
        "/api/v1/roles/find",
        params=(
            ("config", "DemoConfiguration"),
            ("full_name", "Справочник.Orders"),
            ("operation", "read"),
            ("operation", "update"),
            ("limit", "1"),
        ),
    )

    assert api_response.status_code == 200
    assert api_response.json() == mcp
    assert mcp["state"] == "ready"
    assert mcp["declaration_scope"] == "declared_role_rights"
    assert "эффектив" in mcp["disclaimer"].lower()
    assert mcp["generation"] == "generation-1"
    assert mcp["source_sha256"] == "a" * 64
    assert mcp["source_target"] == "Catalog.Orders"
    assert mcp["checked_rights"] == [
        {
            "operation": "read",
            "label_ru": "Чтение данных",
            "channel": "programmatic",
            "platform_right": "Read",
        },
        {
            "operation": "update",
            "label_ru": "Изменение данных",
            "channel": "programmatic",
            "platform_right": "Update",
        },
    ]
    assert mcp["candidates_total"] == 2
    assert len(mcp["candidates"]) == 1
    assert mcp["page"]["next_cursor"]
    assert mcp["minimal_role_set"] == {
        "roles": ["Editor", "Reader"],
        "proof": "explicit_unconditional",
    }
    candidate = mcp["candidates"][0]
    assert candidate["matched_rights"][0]["state"] == "unconditional_true"
    assert candidate["denied_operations"] in ([], ["update"])

    second_arguments = {**arguments, "cursor": mcp["page"]["next_cursor"]}
    second = _tool_json(
        await server.call_tool("find_roles_for_access", second_arguments)
    )
    assert second["page"]["offset"] == 1
    assert second["page"]["next_cursor"] is None
    assert second["minimal_role_set"] == mcp["minimal_role_set"]


async def test_conditional_opt_in_и_explicit_false_различаются(tmp_path):
    registry = Registry(tmp_path / "data")
    _publish(
        registry,
        tmp_path / "source",
        configuration="DemoConfiguration",
        generation_id="generation-1",
    )
    server = _server(registry, tmp_path)
    base = {
        "config": "DemoConfiguration",
        "full_name": "Справочник.Orders",
        "operations": ["read", "update"],
        "limit": 20,
    }

    unconditional = _tool_json(
        await server.call_tool("find_roles_for_access", base)
    )
    conditional = _tool_json(
        await server.call_tool(
            "find_roles_for_access",
            {**base, "include_conditional": True},
        )
    )

    by_name = {row["role"]["name"]: row for row in unconditional["candidates"]}
    assert by_name["Reader"]["denied_operations"] == ["update"]
    assert "Conditional" not in by_name
    assert unconditional["conditional_candidates_excluded"] == 1

    conditional_by_name = {
        row["role"]["name"]: row for row in conditional["candidates"]
    }
    assert conditional_by_name["Conditional"]["conditional_operations"] == [
        "read",
        "update",
    ]
    assert conditional_by_name["Conditional"]["has_rls"] is True
    assert conditional_by_name["Conditional"]["rls_detail_available"] is True
    assert "get_role_access" in conditional_by_name["Conditional"]["next_action"]
    assert {
        right["state"]
        for right in conditional_by_name["Conditional"]["matched_rights"]
    } == {"conditional_true"}
    assert conditional["minimal_role_set"] == {
        "roles": ["Conditional"],
        "proof": "explicit_with_conditions",
    }


async def test_get_role_access_сводит_объекты_и_отдаёт_детали_только_явно(
    tmp_path,
    monkeypatch,
):
    registry = Registry(tmp_path / "data")
    _publish(
        registry,
        tmp_path / "source",
        configuration="DemoConfiguration",
        generation_id="generation-1",
        roles=_summary_roles(),
    )
    server = _server(registry, tmp_path)
    base = {"config": "DemoConfiguration", "role": "CompactReader"}
    role_access_calls = []
    original_role_access = RoleAccessIndex.role_access

    def tracked_role_access(self, *args, **kwargs):
        role_access_calls.append((args, kwargs))
        return original_role_access(self, *args, **kwargs)

    monkeypatch.setattr(RoleAccessIndex, "role_access", tracked_role_access)

    compact = _tool_json(await server.call_tool("get_role_access", base))

    assert role_access_calls == []
    assert compact["mode"] == "objects"
    assert compact["role"]["label_ru"] == "Компактное чтение"
    assert compact["objects_total"] == 1
    assert len(compact["objects"]) == 1
    summary = compact["objects"][0]
    assert summary["target"] == "Catalog.Orders"
    assert summary["full_name"] == "Справочник.Orders"
    assert summary["kind_ru"] == "Справочник"
    assert {right["name"] for right in summary["root_rights"]} == {
        "Read",
        "View",
    }
    assert summary["has_rls"] is True
    assert summary["rls_detail_available"] is True
    assert "detail=children" in summary["next_action"]
    assert summary["descendants"] == {
        "targets_with_grants": 3,
        "granted_rights": 3,
        "conditional_rights": 1,
        "detail_available": True,
    }
    assert "explicit_false" not in json.dumps(compact, ensure_ascii=False)
    assert "Catalog.Hidden" not in json.dumps(compact, ensure_ascii=False)

    exact = _tool_json(
        await server.call_tool(
            "get_role_access",
            {**base, "full_name": "Справочник.Orders"},
        )
    )
    assert len(role_access_calls) == 1
    checks = {
        row["operation"]: row for row in exact["objects"][0]["operation_checks"]
    }
    assert checks["read"]["granted"] is True
    assert checks["view"]["granted"] is True
    assert checks["update"] == {
        "operation": "update",
        "label_ru": "Изменение данных",
        "channel": "programmatic",
        "platform_right": "Update",
        "granted": False,
        "state": "not_granted",
        "evidence": "explicit_false",
        "has_rls": False,
        "rls_detail_available": False,
    }
    assert checks["delete"]["granted"] is False
    assert checks["delete"]["evidence"] == "not_declared"

    interactive = _tool_json(
        await server.call_tool(
            "find_roles_for_access",
            {
                "config": "DemoConfiguration",
                "full_name": "Справочник.Orders",
                "operations": ["view", "edit"],
            },
        )
    )
    assert interactive["checked_rights"] == [
        {
            "operation": "view",
            "label_ru": "Интерактивный просмотр",
            "channel": "interactive",
            "platform_right": "View",
        },
        {
            "operation": "edit",
            "label_ru": "Интерактивное редактирование",
            "channel": "interactive",
            "platform_right": "Edit",
        },
    ]
    assert interactive["candidates"][0]["matched_operations"] == ["view"]
    assert interactive["candidates"][0]["denied_operations"] == ["edit"]

    children = _tool_json(
        await server.call_tool(
            "get_role_access",
            {
                **base,
                "full_name": "Справочник.Orders",
                "detail": "children",
            },
        )
    )
    assert children["mode"] == "children"
    assert children["rights_total"] == 3
    assert {right["target"] for right in children["rights"]} == {
        "Catalog.Orders.Attribute.Code",
        "Catalog.Orders.Command.Open",
        "Catalog.Orders.TabularSection.Items.Attribute.Price",
    }
    nested = next(right for right in children["rights"] if right["child_name"] == "Price")
    assert nested["child_kind_ru"] == "Реквизит"
    assert nested["child_path"] == "TabularSection.Items.Attribute.Price"
    conditional = next(right for right in children["rights"] if right["name"] == "Use")
    assert conditional["has_rls"] is True
    assert conditional["rls_detail_available"] is True
    assert "restriction_ref" in conditional["next_action"]
    assert conditional["restrictions"][0]["ref"]
    assert "explicit_false" not in json.dumps(children, ensure_ascii=False)

    audit = _tool_json(
        await server.call_tool(
            "get_role_access",
            {
                **base,
                "full_name": "Справочник.Orders",
                "detail": "audit",
            },
        )
    )
    assert audit["mode"] == "audit"
    assert {right["state"] for right in audit["rights"]} >= {
        "unconditional_true",
        "conditional_true",
        "explicit_false",
    }


async def test_get_role_access_пагинирует_и_rls_читается_без_потерь(
    tmp_path,
):
    condition = "SyntheticAllowed(" + "x" * 5000 + ")"
    registry = Registry(tmp_path / "data")
    _publish(
        registry,
        tmp_path / "source",
        configuration="DemoConfiguration",
        generation_id="generation-1",
        roles=_roles(
            condition,
            fields=(
                "Catalog.Orders.Attribute.Code",
                "Catalog.Orders.Attribute.Number",
            ),
        ),
    )
    server = _server(registry, tmp_path)
    client = _client(registry, tmp_path)
    arguments = {
        "config": "DemoConfiguration",
        "role": "Conditional",
        "full_name": "Справочник.Orders",
    }

    mcp = _tool_json(await server.call_tool("get_role_access", arguments))
    api = client.get(
        "/api/v1/roles/access",
        params={
            "config": "DemoConfiguration",
            "role": "Conditional",
            "full_name": "Справочник.Orders",
        },
    )

    assert api.status_code == 200
    assert api.json() == mcp
    assert len(json.dumps(mcp, ensure_ascii=False)) < 12_000
    assert condition not in json.dumps(mcp, ensure_ascii=False)
    assert mcp["generation"] == "generation-1"
    assert mcp["source_sha256"] == "a" * 64
    assert mcp["role"]["uuid"] == "33333333-3333-3333-3333-333333333333"
    assert mcp["mode"] == "objects"
    assert mcp["objects_total"] == 1
    assert len(mcp["objects"]) == 1
    assert len(mcp["objects"][0]["root_rights"]) == 2
    right = mcp["objects"][0]["root_rights"][0]
    assert right["state"] == "conditional_true"
    assert right["restrictions"][0]["chars"] == len(condition)
    assert right["restrictions"][0]["fields"] == [
        "Catalog.Orders.Attribute.Code",
        "Catalog.Orders.Attribute.Number",
    ]
    assert "field" not in right["restrictions"][0]
    assert "condition" not in right["restrictions"][0]
    restriction_ref = right["restrictions"][0]["ref"]

    chunks = []
    cursor = None
    while True:
        restriction_arguments = {
            "config": "DemoConfiguration",
            "role": "Conditional",
            "restriction_ref": restriction_ref,
            "max_chars": 256,
        }
        if cursor is not None:
            restriction_arguments["restriction_cursor"] = cursor
        page = _tool_json(
            await server.call_tool("get_role_access", restriction_arguments)
        )
        assert page["mode"] == "restriction"
        assert page["fields"] == [
            "Catalog.Orders.Attribute.Code",
            "Catalog.Orders.Attribute.Number",
        ]
        assert "field" not in page
        assert len(page["content"]) <= 256
        chunks.append(page["content"])
        cursor = page["page"]["next_cursor"]
        if cursor is None:
            break
    assert "".join(chunks) == condition

    api_page = client.get(
        "/api/v1/roles/restriction",
        params={
            "config": "DemoConfiguration",
            "role": "Conditional",
            "restriction_ref": restriction_ref,
            "max_chars": 256,
        },
    )
    assert api_page.status_code == 200
    assert api_page.json()["content"] == condition[:256]


async def test_descriptor_only_роль_возвращает_null_флаги_и_пустые_права(
    tmp_path,
):
    registry = Registry(tmp_path / "data")
    _publish(
        registry,
        tmp_path / "source",
        configuration="DemoConfiguration",
        generation_id="generation-1",
        roles=((
            "DescriptorOnly",
            _descriptor(
                "DescriptorOnly",
                uuid="88888888-8888-8888-8888-888888888888",
            ),
            None,
        ),),
    )

    payload = _tool_json(
        await _server(registry, tmp_path).call_tool(
            "get_role_access",
            {"config": "DemoConfiguration", "role": "DescriptorOnly"},
        )
    )

    assert payload["state"] == "ready"
    assert payload["objects"] == []
    assert payload["objects_total"] == 0
    assert payload["role"]["default_flags"] == {
        "set_for_new_objects": None,
        "set_for_attributes_by_default": None,
        "independent_rights_of_child_objects": None,
        "resolver_effect": "evidence_only",
    }


async def test_missing_и_error_ограничены_если_каталог_включён_другой_конфигурацией(
    tmp_path,
):
    registry = Registry(tmp_path / "data")
    _publish(
        registry,
        tmp_path / "ready-source",
        configuration="ReadyConfiguration",
        generation_id="generation-1",
    )
    _add_missing_configuration(
        registry,
        tmp_path / "missing-source",
        "MissingConfiguration",
    )
    _add_error_configuration(
        registry,
        tmp_path / "error-source",
        "ErrorConfiguration",
    )
    server = _server(registry, tmp_path)

    missing = _tool_json(
        await server.call_tool(
            "get_role_access",
            {"config": "MissingConfiguration", "role": "Any"},
        )
    )
    error = _tool_json(
        await server.call_tool(
            "get_role_access",
            {"config": "ErrorConfiguration", "role": "Any"},
        )
    )

    assert missing["state"] == "missing"
    assert missing["generation"] is None
    assert missing["source_sha256"] is None
    assert len(json.dumps(missing, ensure_ascii=False)) < 2_000
    assert error["state"] == "error"
    assert error["generation"] == "generation-error"
    assert error["source_sha256"] == "b" * 64
    assert len(json.dumps(error, ensure_ascii=False)) < 2_000
    assert str(tmp_path) not in json.dumps(error, ensure_ascii=False)


def test_roles_api_объясняет_setup_missing_error_и_требует_read_token(
    tmp_path,
    monkeypatch,
):
    registry = Registry(tmp_path / "data")
    _publish(
        registry,
        tmp_path / "ready-source",
        configuration="ReadyConfiguration",
        generation_id="generation-1",
    )
    _add_missing_configuration(
        registry,
        tmp_path / "missing-source",
        "MissingConfiguration",
    )
    _add_error_configuration(
        registry,
        tmp_path / "error-source",
        "ErrorConfiguration",
    )
    client = _client(registry, tmp_path)

    setup = client.get(
        "/api/v1/roles",
        params={"config": "ReadyConfiguration", "limit": 2},
    )
    missing = client.get(
        "/api/v1/roles",
        params={"config": "MissingConfiguration"},
    )
    error = client.get(
        "/api/v1/roles",
        params={"config": "ErrorConfiguration"},
    )

    assert setup.status_code == 200
    assert setup.json()["state"] == "ready"
    assert setup.json()["configuration_names"] == [
        "ErrorConfiguration",
        "MissingConfiguration",
        "ReadyConfiguration",
    ]
    assert setup.json()["roles_total"] == 4
    assert len(setup.json()["roles"]) == 2
    assert setup.json()["page"]["next_cursor"]
    operations = setup.json()["operations"]
    assert len(operations) == 16
    assert operations[:4] == [
        {
            "operation": "read",
            "label_ru": "Чтение данных",
            "channel": "programmatic",
            "platform_right": "Read",
        },
        {
            "operation": "view",
            "label_ru": "Интерактивный просмотр",
            "channel": "interactive",
            "platform_right": "View",
        },
        {
            "operation": "update",
            "label_ru": "Изменение данных",
            "channel": "programmatic",
            "platform_right": "Update",
        },
        {
            "operation": "edit",
            "label_ru": "Интерактивное редактирование",
            "channel": "interactive",
            "platform_right": "Edit",
        },
    ]
    assert missing.status_code == 409
    assert missing.json()["state"] == "missing"
    assert error.status_code == 409
    assert error.json()["state"] == "error"

    monkeypatch.setenv("API_TOKEN", "read-token")
    denied = _client(registry, tmp_path).get(
        "/api/v1/roles",
        params={"config": "ReadyConfiguration"},
    )
    assert denied.status_code == 401
    denied_objects = _client(registry, tmp_path).get(
        "/api/v1/roles/objects",
        params={"config": "ReadyConfiguration", "role": "Reader"},
    )
    assert denied_objects.status_code == 401


def test_roles_api_ищет_роль_по_русскому_синониму_и_имени(tmp_path):
    registry = Registry(tmp_path / "data")
    _publish(
        registry,
        tmp_path / "source",
        configuration="DemoConfiguration",
        generation_id="generation-1",
        roles=_navigation_roles(),
    )
    client = _client(registry, tmp_path)

    first = client.get(
        "/api/v1/roles",
        params={
            "config": "DemoConfiguration",
            "query": "администратор",
            "limit": 1,
        },
    )

    assert first.status_code == 200
    payload = first.json()
    assert payload["roles_total"] == 3
    assert payload["roles_matched"] == 2
    assert [role["label_ru"] for role in payload["roles"]] == [
        "Администратор",
    ]
    assert payload["page"]["next_cursor"]

    second = client.get(
        "/api/v1/roles",
        params={
            "config": "DemoConfiguration",
            "query": "администратор",
            "limit": 1,
            "cursor": payload["page"]["next_cursor"],
        },
    )
    assert second.status_code == 200
    assert [role["label_ru"] for role in second.json()["roles"]] == [
        "Администратор учёта",
    ]
    assert second.json()["page"]["next_cursor"] is None

    by_name = client.get(
        "/api/v1/roles",
        params={
            "config": "DemoConfiguration",
            "query": "accountingadmin",
            "limit": 20,
        },
    )
    assert by_name.status_code == 200
    assert [role["name"] for role in by_name.json()["roles"]] == [
        "AccountingAdministrator",
    ]


def test_roles_api_фильтрует_объекты_роли_и_возвращает_фасеты(tmp_path):
    registry = Registry(tmp_path / "data")
    _publish(
        registry,
        tmp_path / "source",
        configuration="DemoConfiguration",
        generation_id="generation-1",
        roles=_navigation_roles(),
    )
    client = _client(registry, tmp_path)

    response = client.get(
        "/api/v1/roles/objects",
        params={
            "config": "DemoConfiguration",
            "role": "Reader",
            "kind": "Document",
            "query": "invoice",
            "limit": 50,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "objects"
    assert payload["object_filters"] == {
        "kind": "Document",
        "query": "invoice",
    }
    assert payload["objects_all_total"] == 4
    assert payload["objects_total"] == 1
    assert [item["full_name"] for item in payload["objects"]] == [
        "Документ.Invoice",
    ]
    assert payload["object_facets"] == [
        {"kind": "Catalog", "kind_ru": "Справочник", "count": 1},
        {"kind": "Document", "kind_ru": "Документ", "count": 2},
        {
            "kind": "InformationRegister",
            "kind_ru": "Регистр сведений",
            "count": 1,
        },
    ]
