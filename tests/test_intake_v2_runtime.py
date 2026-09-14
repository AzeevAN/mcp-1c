"""RED-контракты подключения native generation к runtime Registry."""

from __future__ import annotations

import json
import re
import shutil
import zipfile

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from conftest import write_export
from mcp1c import coverage_log, registry as registry_module, tools as mcp_tools
from mcp1c.intake_v2 import LayerKind, LayerSourceProfile, LayerState
from mcp1c.intake_v2_converter import base_layer_data, convert_collection
from mcp1c.intake_v2_generation import materialize_generation
from mcp1c.intake_v2_runtime import configuration_from_base_layer
from mcp1c.model import Configuration, Field, MetadataObject
from mcp1c.registry import Registry, RegistryError
from mcp1c.render import render_http_service
from mcp1c.server import build_server
from mcp1c.tools import (
    get_callers,
    get_object,
    get_procedure,
    get_related,
    search_objects,
)
import test_intake_v2_converter as converter_fixtures
from test_intake_v2_converter import (
    _collection,
    _common_form_xml,
    _form_with_common_commands,
)


def _materialized(tmp_path, name: str, **collection_options):
    collection = _collection(
        tmp_path / f"source-{name}",
        **collection_options,
    )
    generation = materialize_generation(
        collection,
        convert_collection(collection),
        tmp_path / f"materialized-{name}",
        generation_id=f"generation-{name}",
    )
    return collection, generation


def _rewrite_manifest(target, **changes):
    with zipfile.ZipFile(target) as archive:
        members = {
            item.filename: archive.read(item.filename)
            for item in archive.infolist()
        }
    manifest = json.loads(members["manifest.json"])
    manifest.update(changes)
    members["manifest.json"] = json.dumps(
        manifest,
        ensure_ascii=False,
    ).encode("utf-8")
    with zipfile.ZipFile(target, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return target


def test_remove_native_корпуса_удаляет_единый_агрегат_конфигурации(tmp_path):
    _collection_value, generation = _materialized(
        tmp_path,
        "remove-code",
        common_forms=True,
    )
    registry = Registry(tmp_path / "data")
    previous = registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )
    registry.incoming_dir.mkdir(parents=True)
    incoming = registry.incoming_dir / "оставить.zip"
    incoming.write_bytes(b"synthetic incoming")

    registry.remove("DemoConfiguration:modules")

    current_pointer = registry.active_generation_pointer(generation.manifest.identity)
    current = registry.active_generation(generation.manifest.identity)
    assert current_pointer is None
    assert current is None
    with pytest.raises(RegistryError, match="Не загружено ни одной конфигурации"):
        registry.resolve("DemoConfiguration")
    assert not (registry.data_dir / previous.root_path).exists()
    assert incoming.read_bytes() == b"synthetic incoming"

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    with pytest.raises(RegistryError, match="Не загружено ни одной конфигурации"):
        restarted.resolve("DemoConfiguration")
    assert incoming.read_bytes() == b"synthetic incoming"


def test_native_commit_атомарно_подключает_структуру_код_и_формы(
    tmp_path,
    monkeypatch,
):
    collection, generation = _materialized(
        tmp_path,
        "001",
        common_forms=True,
    )
    registry = Registry(tmp_path / "data")

    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )

    context = registry.resolve("DemoConfiguration")
    catalog = context.configuration.config.get("Справочник.Items")
    assert catalog is not None
    assert {
        field.name: field.types
        for field in catalog.attributes
        if not field.standard
    } == {
        "Title": ["Строка"],
        "Owner": ["Справочник.Items"],
    }
    assert catalog.tabular_parts[0].attributes[0].digits == 15
    assert context.modules is not None and context.modules.готов
    assert context.modules.структура is not None
    assert context.modules.структура.complete
    assert "Справочник.Items" in context.modules.структура.objects
    assert "Справочник.Items.Title" in context.modules.структура.fields
    assert "Справочник.Items.МодульОбъекта" in context.modules.оглавление.модули
    assert "ОбщаяФорма.Workspace" in context.modules.формы.модули
    assert "procedure Demo()" in get_procedure(
        registry,
        "Справочник.Items.МодульОбъекта::Demo",
        config="DemoConfiguration",
    )
    pointer = registry.active_generation_pointer(generation.manifest.identity)
    assert pointer is not None
    shutil.rmtree(collection.root)
    shutil.rmtree(generation.root)

    restarted = Registry(registry.data_dir)

    def reject_cold_rebuild(*_args, **_kwargs):
        pytest.fail("неизменённый native runtime обязан подняться из кэша")

    monkeypatch.setattr(
        restarted,
        "_построить_индекс_кода",
        reject_cold_rebuild,
    )
    monkeypatch.setattr("mcp1c.registry.index_configuration", reject_cold_rebuild)
    monkeypatch.setattr("mcp1c.registry.index_fields", reject_cold_rebuild)
    assert restarted.restore() == []
    restored = restarted.resolve("DemoConfiguration")
    assert restored.configuration.config.get("Справочник.Items") is not None
    assert restored.modules is not None and restored.modules.готов
    assert restored.modules.структура is not None
    assert restored.modules.структура.complete
    assert "procedure Demo()" in get_procedure(
        restarted,
        "Справочник.Items.МодульОбъекта::Demo",
        config="DemoConfiguration",
    )
    assert (registry.data_dir / pointer.root_path).is_dir()


def test_native_плоская_xml_форма_доходит_до_runtime_вместе_с_событиями(
    tmp_path,
):
    _collection_value, generation = _materialized(
        tmp_path,
        "flat-form-xml",
        flat_common_forms=True,
        item_form_xml=_common_form_xml(),
    )
    registry = Registry(tmp_path / "data-flat-form-xml")
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )

    context = registry.resolve("DemoConfiguration")
    assert context.modules is not None
    managed = context.modules.формы.состав("ОбщаяФорма.FlatManaged")
    descriptor_only = context.modules.формы.состав(
        "ОбщаяФорма.FlatDescriptorOnly"
    )
    assert managed is not None
    assert managed.реквизиты == ["Filter"]
    assert managed.элементы == ["FilterField", "Pages", "Refresh"]
    assert managed.события == {
        "OnOpen": ("OnOpen",),
        "OnChange": ("OnChange",),
        "Refresh": ("Click",),
    }
    assert managed.состояние_xml == "ready"
    assert not managed.структура_частична
    assert descriptor_only is not None
    assert descriptor_only.структура_доступна
    assert descriptor_only.структура_частична

    coverage = mcp_tools.sources_snapshot(registry).code[0].coverage
    assert coverage is not None
    assert coverage.forms_total == 3
    assert coverage.form_structures_full == 2
    assert coverage.form_structures_partial == 1
    assert coverage.form_structures_unread == 0
    assert coverage.form_modules_read == 1
    assert coverage.form_modules_missing == 2


def test_native_common_command_карточка_граф_замена_и_restart(
    tmp_path,
    monkeypatch,
):
    _baseline_collection, baseline = _materialized(
        tmp_path,
        "common-command-baseline",
        common_commands=True,
        item_form_xml=_form_with_common_commands(),
    )
    _changed_collection, changed = _materialized(
        tmp_path,
        "common-command-changed",
        common_commands=True,
        item_form_xml=_common_form_xml(),
    )
    _removed_collection, removed = _materialized(
        tmp_path,
        "common-command-removed",
        item_form_xml=_common_form_xml(),
    )
    registry = Registry(tmp_path / "data-common-command")

    registry.publish_generation(
        registry.stage_generation(baseline.manifest, baseline.payloads)
    )
    assert "`ОбщаяКоманда.Run`" in search_objects(
        registry,
        "выполнить",
        config="DemoConfiguration",
        kind="ОбщаяКоманда",
    )
    card = get_object(
        registry,
        "ОбщаяКоманда.Run",
        config="DemoConfiguration",
        detail="full",
    )
    assert "Синтетическая общая команда" in card
    assert "procedure Execute" in get_procedure(
        registry,
        "ОбщаяКоманда.Run::Execute",
        config="DemoConfiguration",
    )
    outgoing = get_related(
        registry,
        "Справочник.Items",
        config="DemoConfiguration",
    )
    incoming = get_related(
        registry,
        "ОбщаяКоманда.Run",
        config="DemoConfiguration",
    )
    assert "ОбщаяКоманда.Run" in outgoing
    assert "общая команда формы" in outgoing
    assert "Справочник.Items" in incoming
    assert "form=Справочник.Items.Форма.Card" in incoming

    registry.publish_generation(
        registry.stage_generation(changed.manifest, changed.payloads)
    )
    assert "ОбщаяКоманда.Run" not in get_related(
        registry,
        "Справочник.Items",
        config="DemoConfiguration",
    )

    restarted = Registry(registry.data_dir)

    def reject_cold_rebuild(*_args, **_kwargs):
        pytest.fail("неизменённая общая команда обязана подняться из кэша")

    monkeypatch.setattr("mcp1c.registry.index_configuration", reject_cold_rebuild)
    monkeypatch.setattr("mcp1c.registry.index_fields", reject_cold_rebuild)
    assert restarted.restore() == []
    assert "`ОбщаяКоманда.Run`" in search_objects(
        restarted,
        "выполнить",
        config="DemoConfiguration",
        kind="ОбщаяКоманда",
    )

    monkeypatch.undo()
    restarted.publish_generation(
        restarted.stage_generation(removed.manifest, removed.payloads)
    )
    assert "нет объекта `ОбщаяКоманда.Run`" in get_object(
        restarted,
        "ОбщаяКоманда.Run",
        config="DemoConfiguration",
    )


def test_native_extended_objects_доступны_через_mcp_после_restart(
    tmp_path,
    monkeypatch,
):
    collection, generation = _materialized(
        tmp_path,
        "extended-runtime",
        journal=True,
        bindings=True,
        common_forms=True,
        bots=True,
        filter_criteria=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )
    expected = (
        ("ОбщийРеквизит.Tenant", "ОбщийРеквизит"),
        ("ПараметрСеанса.Tenant", "ПараметрСеанса"),
        ("ЖурналДокументов.Ledger", "ЖурналДокументов"),
        ("ПланОбмена.Nodes", "ПланОбмена"),
        ("ПодпискаНаСобытие.Resolved", "ПодпискаНаСобытие"),
        ("РегламентноеЗадание.Refresh", "РегламентноеЗадание"),
        ("ОбщаяФорма.Workspace", "ОбщаяФорма"),
        ("Бот.Assistant", "Бот"),
        ("КритерийОтбора.ByItem", "КритерийОтбора"),
        ("КритерийОтбора.DescriptorOnly", "КритерийОтбора"),
    )

    shutil.rmtree(collection.root)
    shutil.rmtree(generation.root)
    for full_name, kind in expected:
        found = search_objects(
            registry,
            full_name,
            config="DemoConfiguration",
            kind=kind,
        )
        assert f"`{full_name}`" in found
        card = get_object(
            registry,
            full_name,
            config="DemoConfiguration",
            detail="brief",
        )
        assert f"нет объекта `{full_name}`" not in card
    common_attribute = registry.resolve(
        "DemoConfiguration"
    ).configuration.config.get("ОбщийРеквизит.Tenant")
    assert common_attribute is not None
    assert common_attribute.manager_path == "ОбщиеРеквизиты.Tenant"
    assert common_attribute.value_type is not None
    assert common_attribute.value_type.type_spec() == "Строка(40)"
    common_attribute_card = get_object(
        registry,
        "ОбщийРеквизит.Tenant",
        config="DemoConfiguration",
        detail="full",
    )
    assert "Допустимая длина строки: `Fixed`" in common_attribute_card
    catalog = registry.resolve(
        "DemoConfiguration"
    ).configuration.config.get("Справочник.Items")
    assert catalog is not None
    catalog_fields = {item.name: item for item in catalog.attributes}
    assert catalog_fields["Ссылка"].types == ["Справочник.Items"]
    assert catalog_fields["Код"].type_spec() == "Строка(9, перем.)"
    assert catalog_fields["Наименование"].type_spec() == "Строка(120, перем.)"
    assert catalog_fields["Родитель"].types == ["Справочник.Items"]
    assert catalog_fields["ЭтоГруппа"].types == ["Булево"]
    assert catalog_fields["ПометкаУдаления"].types == ["Булево"]
    assert catalog_fields["Предопределенный"].types == ["Булево"]
    assert catalog_fields["ИмяПредопределенныхДанных"].is_unlimited_string is False
    document = registry.resolve(
        "DemoConfiguration"
    ).configuration.config.get("Документ.Invoice")
    assert document is not None
    document_fields = {item.name: item for item in document.attributes}
    assert document_fields["Ссылка"].types == ["Документ.Invoice"]
    assert document_fields["Номер"].type_spec() == "Число(3,0)"
    assert document_fields["Дата"].types == ["Дата"]
    assert document_fields["Проведен"].types == ["Булево"]
    assert document_fields["ПометкаУдаления"].types == ["Булево"]
    journal = registry.resolve(
        "DemoConfiguration"
    ).configuration.config.get("ЖурналДокументов.Ledger")
    assert journal is not None
    journal_fields = {item.name: item for item in journal.attributes}
    assert journal_fields["Тип"].types == ["Строка"]
    assert journal_fields["Ссылка"].types == [
        "Документ.Invoice",
        "Документ.Missing",
    ]
    assert journal_fields["Дата"].types == ["Дата"]
    assert journal_fields["Проведен"].types == ["Булево"]
    assert journal_fields["ПометкаУдаления"].types == ["Булево"]
    assert journal_fields["Amount"].types == ["Число"]
    assert journal_fields["Missing"].types == []
    assert not {
        "Type",
        "Ref",
        "Number",
        "Date",
        "Posted",
        "DeletionMark",
    } & set(journal_fields)
    relations = get_related(
        registry,
        "ЖурналДокументов.Ledger",
        config="DemoConfiguration",
    )
    assert "Документ.Invoice" in relations
    assert "регистрирует документ" in relations
    context = registry.resolve("DemoConfiguration")
    exchange_plan = context.configuration.config.get("ПланОбмена.Nodes")
    subscription = context.configuration.config.get(
        "ПодпискаНаСобытие.Resolved"
    )
    scheduled_job = context.configuration.config.get(
        "РегламентноеЗадание.Refresh"
    )
    assert exchange_plan is not None
    assert exchange_plan.extended["content"][0]["target"] == "Справочник.Items"
    assert subscription is not None
    assert subscription.props["handler"] == "CommonModule.Handlers.OnWrite"
    subscription_sources = [
        edge
        for edge in context.configuration.graph.outgoing(subscription.full_name)
        if edge.target == "Справочник.Items"
    ]
    assert [(edge.kind, edge.title) for edge in subscription_sources] == [
        ("source", "источник подписки")
    ]
    assert scheduled_job is not None
    assert scheduled_job.props["method"] == "CommonModule.Handlers.RunJob"
    common_form_card = get_object(
        registry,
        "ОбщаяФорма.Workspace",
        config="DemoConfiguration",
        detail="full",
    )
    assert "Использовать стандартные команды: `Нет`" in common_form_card
    criterion_relations = get_related(
        registry,
        "КритерийОтбора.ByItem",
        config="DemoConfiguration",
    )
    assert "Справочник.Items" in criterion_relations
    assert "применяется к" in criterion_relations

    restarted = Registry(registry.data_dir)

    def reject_cold_rebuild(*_args, **_kwargs):
        pytest.fail("неизменённый единый A/B view обязан подняться из кэша")

    monkeypatch.setattr("mcp1c.registry.index_configuration", reject_cold_rebuild)
    monkeypatch.setattr("mcp1c.registry.index_fields", reject_cold_rebuild)
    assert restarted.restore() == []

    for full_name, kind in expected:
        found = search_objects(
            restarted,
            full_name,
            config="DemoConfiguration",
            kind=kind,
        )
        assert f"`{full_name}`" in found
        card = get_object(
            restarted,
            full_name,
            config="DemoConfiguration",
            detail="brief",
        )
        assert f"нет объекта `{full_name}`" not in card


def test_native_filter_criterion_изменение_и_удаление_переживают_restart(
    tmp_path,
):
    _collection_value, baseline = _materialized(
        tmp_path,
        "filter-baseline",
        filter_criteria=True,
    )
    _collection_value, changed = _materialized(
        tmp_path,
        "filter-changed",
        filter_criteria=True,
        filter_criterion_comment="Обновлённый синтетический критерий",
    )
    _collection_value, removed = _materialized(tmp_path, "filter-removed")
    registry = Registry(tmp_path / "data-filter-criterion")

    first = registry.publish_generation(
        registry.stage_generation(baseline.manifest, baseline.payloads)
    )
    original = registry.resolve("DemoConfiguration").configuration.config.get(
        "КритерийОтбора.ByItem"
    )
    assert original is not None
    assert original.comment == "Синтетический критерий отбора"

    second = registry.publish_generation(
        registry.stage_generation(changed.manifest, changed.payloads)
    )
    updated = registry.resolve("DemoConfiguration").configuration.config.get(
        "КритерийОтбора.ByItem"
    )
    assert second.generation_id != first.generation_id
    assert updated is not None
    assert updated.comment == "Обновлённый синтетический критерий"

    third = registry.publish_generation(
        registry.stage_generation(removed.manifest, removed.payloads)
    )
    assert third.generation_id != second.generation_id
    assert (
        registry.resolve("DemoConfiguration").configuration.config.get(
            "КритерийОтбора.ByItem"
        )
        is None
    )

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert (
        restarted.resolve("DemoConfiguration").configuration.config.get(
            "КритерийОтбора.ByItem"
        )
        is None
    )


def test_native_http_service_endpoint_доступен_через_mcp_после_restart(
    tmp_path,
):
    _collection_value, generation = _materialized(
        tmp_path,
        "http-service",
        http_services=True,
    )
    registry = Registry(tmp_path / "data-http-service")
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )

    def assert_http_contract(current: Registry) -> None:
        found = search_objects(
            current,
            "HTTPСервис.Api",
            config="DemoConfiguration",
            kind="HTTPСервис",
        )
        assert "`HTTPСервис.Api`" in found
        card = get_object(
            current,
            "HTTPСервис.Api",
            config="DemoConfiguration",
            detail="fields",
        )
        assert "Корневой путь после публикации: `/hs/api`" in card
        assert "Повторное использование сеансов: `Use`" in card
        assert "Время жизни сеанса: `900` секунд" in card
        assert "`/hs/api/items/{id}`" in card
        assert "`Any` · `GetItem`" in card
        assert "`HTTPСервис.Api::Handle` · привязка разрешена" in card
        procedure = get_procedure(
            current,
            "HTTPСервис.Api::Handle",
            config="DemoConfiguration",
        )
        assert "Функция Handle(Request)" in procedure
        callers = get_callers(
            current,
            "HTTPСервис.Api::Handle",
            config="DemoConfiguration",
        )
        assert "HTTP-метод `Any`" in callers
        assert "`/hs/api/items/{id}`" in callers

    assert_http_contract(registry)
    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert_http_contract(restarted)


def test_native_http_service_проходит_полную_mcp_сессию(tmp_path):
    """Агент видит HTTP-сервис через настоящий протокольный контракт SDK."""
    _collection_value, generation = _materialized(
        tmp_path,
        "http-service-mcp-session",
        http_services=True,
    )
    registry = Registry(tmp_path / "data-http-service-mcp-session")
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )
    server = build_server(registry)

    async def run_session():
        async with create_client_server_memory_streams() as (
            client_streams,
            server_streams,
        ):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(
                    server._lowlevel_server.run,
                    *server_streams,
                    server._lowlevel_server.create_initialization_options(),
                )
                try:
                    async with ClientSession(*client_streams) as session:
                        initialized = await session.initialize()
                        catalog = await session.list_tools()
                        listed = await session.call_tool("list_configurations", {})
                        found = await session.call_tool(
                            "search_objects",
                            {
                                "query": "HTTPСервис.Api",
                                "config": "DemoConfiguration",
                                "kind": "HTTPСервис",
                            },
                        )
                        card = await session.call_tool(
                            "get_object",
                            {
                                "full_name": "HTTPСервис.Api",
                                "config": "DemoConfiguration",
                                "detail": "fields",
                            },
                        )
                        procedure = await session.call_tool(
                            "get_procedure",
                            {
                                "address": "HTTPСервис.Api::Handle",
                                "config": "DemoConfiguration",
                            },
                        )
                        callers = await session.call_tool(
                            "get_callers",
                            {
                                "address": "HTTPСервис.Api::Handle",
                                "config": "DemoConfiguration",
                            },
                        )
                finally:
                    tasks.cancel_scope.cancel()
        return initialized, catalog, listed, found, card, procedure, callers

    initialized, catalog, listed, found, card, procedure, callers = anyio.run(
        run_session,
        backend="asyncio",
    )

    assert initialized.server_info.name == "mcp1c"
    assert {
        "list_configurations",
        "search_objects",
        "get_object",
        "get_procedure",
        "get_callers",
    } <= {tool.name for tool in catalog.tools}
    assert all(
        not result.is_error
        for result in (listed, found, card, procedure, callers)
    )
    assert "DemoConfiguration" in listed.content[0].text
    assert "`HTTPСервис.Api`" in found.content[0].text
    assert "`/hs/api/items/{id}`" in card.content[0].text
    assert "`HTTPСервис.Api::Handle` · привязка разрешена" in card.content[0].text
    assert "Функция Handle(Request)" in procedure.content[0].text
    assert "HTTP-метод `Any`" in callers.content[0].text
    assert "`/hs/api/items/{id}`" in callers.content[0].text


def test_native_xdto_проходит_mcp_сессию_после_restart(tmp_path):
    """Агент получает XDTO через прежние инструменты, а не Python-вызов."""
    _collection_value, generation = _materialized(
        tmp_path,
        "xdto-mcp-session",
        xdto_packages=True,
    )
    registry = Registry(tmp_path / "data-xdto-mcp-session")
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )
    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    server = build_server(restarted)

    async def run_session():
        async with create_client_server_memory_streams() as (
            client_streams,
            server_streams,
        ):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(
                    server._lowlevel_server.run,
                    *server_streams,
                    server._lowlevel_server.create_initialization_options(),
                )
                try:
                    async with ClientSession(*client_streams) as session:
                        initialized = await session.initialize()
                        catalog = await session.list_tools()
                        found = await session.call_tool(
                            "search_objects",
                            {
                                "query": "Item",
                                "config": "DemoConfiguration",
                            },
                        )
                        card = await session.call_tool(
                            "get_object",
                            {
                                "full_name": "ПакетXDTO.Main.ТипОбъекта.Item",
                                "config": "DemoConfiguration",
                                "detail": "fields",
                            },
                        )
                finally:
                    tasks.cancel_scope.cancel()
        return initialized, catalog, found, card

    initialized, catalog, found, card = anyio.run(
        run_session,
        backend="asyncio",
    )

    assert initialized.server_info.name == "mcp1c"
    assert {"search_objects", "get_object"} <= {
        tool.name for tool in catalog.tools
    }
    assert not found.is_error and not card.is_error
    assert "`ПакетXDTO.Main.ТипОбъекта.Item`" in found.content[0].text
    assert "Свойства (2)" in card.content[0].text
    assert "межпакетная" in card.content[0].text


def test_native_http_service_change_и_удаление_endpoint_переживают_restart(
    tmp_path,
):
    _baseline_collection, baseline = _materialized(
        tmp_path,
        "http-baseline",
        http_services=True,
    )
    _changed_collection, changed = _materialized(
        tmp_path,
        "http-changed",
        http_services=True,
        http_template="/items/{code}",
        http_method="HEAD",
        http_handler="Other",
        http_module=b"function Other(Request) endfunction",
    )
    _removed_collection, removed = _materialized(
        tmp_path,
        "http-removed",
        http_services=True,
        http_include_template=False,
    )
    registry = Registry(tmp_path / "data-http-change")

    first = registry.publish_generation(
        registry.stage_generation(baseline.manifest, baseline.payloads)
    )
    first_card = get_object(
        registry,
        "HTTPСервис.Api",
        config="DemoConfiguration",
        detail="fields",
    )
    assert "`/hs/api/items/{id}`" in first_card

    second = registry.publish_generation(
        registry.stage_generation(changed.manifest, changed.payloads)
    )
    second_card = get_object(
        registry,
        "HTTPСервис.Api",
        config="DemoConfiguration",
        detail="fields",
    )
    assert second.generation_id != first.generation_id
    assert "`/hs/api/items/{id}`" not in second_card
    assert "`/hs/api/items/{code}`" in second_card
    assert "`HEAD` · `GetItem`" in second_card
    assert "`HTTPСервис.Api::Other` · привязка разрешена" in second_card

    third = registry.publish_generation(
        registry.stage_generation(removed.manifest, removed.payloads)
    )
    assert third.generation_id != second.generation_id
    removed_card = get_object(
        registry,
        "HTTPСервис.Api",
        config="DemoConfiguration",
        detail="fields",
    )
    assert "URL-шаблоны и методы (0 / 0)" in removed_card
    assert "`/hs/api/items/{code}`" not in removed_card

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    restarted_card = get_object(
        restarted,
        "HTTPСервис.Api",
        config="DemoConfiguration",
        detail="fields",
    )
    assert "URL-шаблоны и методы (0 / 0)" in restarted_card
    assert "`/hs/api/items/{id}`" not in restarted_card
    assert "`/hs/api/items/{code}`" not in restarted_card


def test_http_service_дочитывается_без_пропусков_и_не_переносит_cursor(
    tmp_path, monkeypatch
):
    original = converter_fixtures._http_service
    descriptor = original().decode()
    start = descriptor.index('<URLTemplate ')
    end = descriptor.index('</URLTemplate>', start) + len('</URLTemplate>')
    template = descriptor[start:end]
    method_start = template.index('<Method ')
    method_end = template.index('</Method>', method_start) + len('</Method>')
    method = template[method_start:method_end]
    templates = []
    for number in range(41):
        current = (
            template.replace('uuid-template', f'uuid-template-{number}')
            .replace('<Name>Item</Name>', f'<Name>Route{number:02}</Name>')
            .replace('/items/{id}', f'/route{number:02}')
            .replace('uuid-method', f'uuid-method-{number}-a')
            .replace('<Name>GetItem</Name>', f'<Name>Method{number:02}A</Name>')
        )
        if number < 40:
            second = (
                method.replace('uuid-method', f'uuid-method-{number}-b')
                .replace('<Name>GetItem</Name>', f'<Name>Method{number:02}B</Name>')
            )
            current = current.replace('</ChildObjects>', second + '</ChildObjects>')
        templates.append(current)
    expanded = (descriptor[:start] + ''.join(templates) + descriptor[end:]).encode()

    monkeypatch.setattr(converter_fixtures, '_http_service', lambda **_kwargs: expanded)
    _collection_value, generation = _materialized(
        tmp_path,
        'http-pagination',
        http_services=True,
    )
    registry = Registry(tmp_path / 'data-http-pagination')
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )

    first_pages = []
    for detail in ('fields', 'full'):
        pages = []
        cursor = None
        while True:
            page = get_object(
                registry,
                'HTTPСервис.Api',
                config='DemoConfiguration',
                detail=detail,
                cursor=cursor,
            )
            pages.append(page)
            continuation = re.search(r'`get_object\((\{.*\})\)`', page)
            if continuation is None:
                break
            cursor = json.loads(continuation.group(1))['cursor']

        combined = '\n'.join(pages)
        for number in range(41):
            assert combined.count(f'`/hs/api/route{number:02}`') == 1
            assert combined.count(f'`Method{number:02}A`') == 1
            if number < 40:
                assert combined.count(f'`Method{number:02}B`') == 1
        assert len(pages) == 2
        first_pages.append(pages[0])

    old_cursor = json.loads(
        re.search(r'`get_object\((\{.*\})\)`', first_pages[0]).group(1)
    )['cursor']
    monkeypatch.setattr(registry_module, '_now', lambda: '2099-01-01T00:00:00Z')
    restarted = Registry(tmp_path / 'data-http-pagination')
    assert restarted.restore() == []
    restarted_page = get_object(
        restarted,
        'HTTPСервис.Api',
        config='DemoConfiguration',
        detail='fields',
        cursor=old_cursor,
    )
    assert '`/hs/api/route40`' in restarted_page
    assert '`Method40A`' in restarted_page
    assert '`get_object({' not in restarted_page

    other_dir = tmp_path / 'other-configuration'
    other_dir.mkdir()
    registry.add_configuration(
        write_export(
            other_dir,
            Configuration(
                name='OtherConfiguration',
                version='1.0',
                platform='8.3.23.1997',
            ),
        )
    )
    with pytest.raises(RegistryError, match='Курсор.*другому объекту'):
        get_object(
            registry,
            'HTTPСервис.Api',
            config='DemoConfiguration',
            detail='full',
            cursor=old_cursor,
        )
    with pytest.raises(RegistryError, match='Курсор.*detail'):
        get_object(
            registry,
            'HTTPСервис.Api',
            config='DemoConfiguration',
            detail='field',
            cursor=old_cursor,
        )
    with pytest.raises(RegistryError, match='Курсор.*другому объекту'):
        get_object(
            registry,
            'HTTPСервис.Missing',
            config='DemoConfiguration',
            detail='fields',
            cursor=old_cursor,
        )
    with pytest.raises(RegistryError, match='Курсор.*конфигурации'):
        get_object(
            registry,
            'HTTPСервис.Api',
            config='OtherConfiguration',
            detail='fields',
            cursor=old_cursor,
        )
    with pytest.raises(RegistryError, match='Некорректный курсор'):
        get_object(
            registry,
            'HTTPСервис.Api',
            config='DemoConfiguration',
            detail='fields',
            cursor='!',
        )
    monkeypatch.setattr(converter_fixtures, '_http_service', original)
    _changed_collection, changed = _materialized(
        tmp_path,
        'http-pagination-changed',
        http_services=True,
        http_template='/changed',
    )
    registry.publish_generation(
        registry.stage_generation(changed.manifest, changed.payloads)
    )

    with pytest.raises(RegistryError, match='Курсор.*изменивш'):
        get_object(
            registry,
            'HTTPСервис.Api',
            config='DemoConfiguration',
            detail='fields',
            cursor=old_cursor,
        )


def test_http_service_дочитывает_81_метод_одного_шаблона_без_дубля_пути():
    methods = [
        {
            'name': f'Method{number:02}',
            'http_method': 'GET',
            'handler': f'Handle{number:02}',
        }
        for number in range(81)
    ]
    obj = MetadataObject(
        full_name='HTTPСервис.Api',
        kind='HTTPСервис',
        name='Api',
        extended={
            'root_url': 'api',
            'url_templates': [
                {'name': 'All', 'template': '/all', 'methods': methods}
            ],
        },
    )

    first = render_http_service(obj, 'fields', {})
    assert (first.next_template, first.next_method) == (0, 80)
    second = render_http_service(
        obj,
        'fields',
        {},
        template_offset=first.next_template,
        method_offset=first.next_method,
    )
    combined = first.text + second.text

    assert second.next_template is None
    assert combined.count('`/hs/api/all`') == 1
    for number in range(81):
        assert combined.count(f'`Method{number:02}`') == 1


@pytest.mark.parametrize("explicit_null", [False, True])
def test_source_a_missing_register_properties_остаются_unknown_после_restart(
    tmp_path, explicit_null
):
    source_dir = tmp_path / "source-a-unknown"
    source_dir.mkdir()
    configuration = Configuration(
        name="UnknownA",
        objects={
            full_name: MetadataObject(
                full_name=full_name,
                kind=kind,
                name="Unknown",
                props=(
                    {
                        "correspondence": None,
                        "period_adjustment_length": None,
                    }
                    if explicit_null and kind == "РегистрБухгалтерии"
                    else (
                        {"action_period": None, "base_period": None}
                        if explicit_null
                        else {}
                    )
                ),
            )
            for kind, full_name in (
                ("РегистрБухгалтерии", "РегистрБухгалтерии.Unknown"),
                ("РегистрРасчета", "РегистрРасчета.Unknown"),
            )
        },
    )
    registry = Registry(tmp_path / "data-a-unknown")
    registry.add_configuration(write_export(source_dir, configuration))
    registry.save()

    for current in (registry, Registry(registry.data_dir)):
        if current is not registry:
            assert current.restore() == []
        accounting = get_object(
            current,
            "РегистрБухгалтерии.Unknown",
            config="UnknownA",
            detail="fields",
        )
        calculation = get_object(
            current,
            "РегистрРасчета.Unknown",
            config="UnknownA",
            detail="fields",
        )
        assert "по свойствам unknown" in accounting
        assert "длина уточнения периода не доказана источником" in accounting
        assert "длина уточнения периода равна нулю" not in accounting
        assert "ВидДвижения — ВидДвиженияБухгалтерии" not in accounting
        assert "Счет —" not in accounting
        assert "по свойствам unknown" in calculation
        assert "период действия выключен" not in calculation
        assert "базовый период выключен" not in calculation


def test_source_b_missing_register_properties_остаются_unknown_после_restart(
    tmp_path, monkeypatch
):
    descriptor = converter_fixtures._accounting_register()
    for element in (
        b"<Correspondence>true</Correspondence>",
        b"<ChartOfAccounts>ChartOfAccounts.Main</ChartOfAccounts>",
        b"<PeriodAdjustmentLength>0</PeriodAdjustmentLength>",
    ):
        descriptor = descriptor.replace(element, b"")
    monkeypatch.setattr(
        converter_fixtures,
        "_accounting_register",
        lambda: descriptor,
    )
    _collection_value, generation = _materialized(
        tmp_path,
        "unknown-register-properties",
        accounting_register=True,
    )
    registry = Registry(tmp_path / "data-b-unknown")
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    card = get_object(
        restarted,
        "РегистрБухгалтерии.Ledger",
        config="DemoConfiguration",
        detail="fields",
    )

    assert "по свойствам unknown" in card
    assert "значение корреспонденции не доказано источником" in card
    assert "длина уточнения периода не доказана источником" in card
    assert "длина уточнения периода равна нулю" not in card
    assert "ВидДвижения — ВидДвиженияБухгалтерии" not in card
    assert "Счет —" not in card


def test_legacy_schema_v1_получает_ту_же_проекцию_после_restart(tmp_path):
    source_dir = tmp_path / "source-a"
    source_dir.mkdir()
    configuration = Configuration(
        name="Legacy",
        objects={
            "Справочник.Items": MetadataObject(
                full_name="Справочник.Items",
                kind="Справочник",
                name="Items",
                props={
                    "hierarchical": False,
                    "code_length": 9,
                    "code_type": "Строка",
                    "code_allowed_length": "Variable",
                    "description_length": 80,
                },
                attributes=[
                    Field(
                        "Custom",
                        types=["Строка"],
                        string_length=20,
                        string_allowed_length="Fixed",
                    )
                ],
            ),
            "Документ.Invoice": MetadataObject(
                full_name="Документ.Invoice",
                kind="Документ",
                name="Invoice",
                props={
                    "number_length": 11,
                    "number_type": "Строка",
                    "number_allowed_length": "Fixed",
                    "numerator": "Нумератор.Shared",
                    "number_rules_resolved": True,
                },
            ),
        },
    )
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(source_dir, configuration))
    registry.save()

    current = registry.resolve("Legacy").configuration.config
    assert current.get("Справочник.Items").attributes[0].name == "Ссылка"
    assert current.get("Справочник.Items").attributes[1].type_spec() == (
        "Строка(9, перем.)"
    )
    assert current.get("Документ.Invoice").attributes[1].type_spec() == (
        "Строка(11, фикс.)"
    )

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    restored = restarted.resolve("Legacy").configuration.config
    assert restored.get("Справочник.Items").attributes[0].name == "Ссылка"
    assert restored.get("Справочник.Items").attributes[1].type_spec() == (
        "Строка(9, перем.)"
    )
    assert restored.get("Документ.Invoice").attributes[1].type_spec() == (
        "Строка(11, фикс.)"
    )

    xml_source = tmp_path / "legacy-xml.zip"
    manifest = (
        '<manifest schema_version="1" format="xml" name="LegacyXml" '
        'objects_total="1" truncated="false">'
        '<files><item path="objects/document.001.xml" type="Документ" '
        'chunk="1" count="1"/></files></manifest>'
    )
    chunk = (
        '<objects schema_version="1" type="Документ" chunk="1" count="1">'
        '<object full_name="Документ.Invoice" type="Документ" name="Invoice" '
        'number_length="11" number_type="Строка" '
        'number_allowed_length="Фиксированная" numerator="Нумератор.Shared" '
        'number_rules_resolved="true"/></objects>'
    )
    with zipfile.ZipFile(xml_source, "w") as archive:
        archive.writestr("manifest.xml", manifest)
        archive.writestr("objects/document.001.xml", chunk)
    xml_registry = Registry(tmp_path / "xml-data")
    xml_registry.add_configuration(xml_source, keep_source=False)
    xml_document = xml_registry.resolve("LegacyXml").configuration.config.get(
        "Документ.Invoice"
    )
    assert xml_document is not None
    assert xml_document.props["number_rules_resolved"] is True
    assert xml_document.attributes[1].type_spec() == "Строка(11, фикс.)"

    old_semantic = base_layer_data(configuration)
    for raw_object in old_semantic["objects"]:
        for raw_field in raw_object["attributes"]:
            raw_field.pop("string_allowed_length")
    restored_old = configuration_from_base_layer(old_semantic)
    old_field = restored_old.get("Справочник.Items").attributes[0]
    assert old_field.name == "Custom"
    assert old_field.string_allowed_length == ""


def test_смена_только_extended_не_поднимает_старый_object_cache(tmp_path):
    _first_collection, first = _materialized(tmp_path, "extended-cache-001")
    _second_collection, second = _materialized(
        tmp_path,
        "extended-cache-002",
        common_forms=True,
    )
    first_layers = {layer.kind: layer for layer in first.manifest.layers}
    second_layers = {layer.kind: layer for layer in second.manifest.layers}
    assert (
        first_layers[LayerKind.BASE_STRUCTURE].content_sha256
        == second_layers[LayerKind.BASE_STRUCTURE].content_sha256
    )
    assert (
        first_layers[LayerKind.EXTENDED_STRUCTURE].content_sha256
        != second_layers[LayerKind.EXTENDED_STRUCTURE].content_sha256
    )
    registry = Registry(tmp_path / "data")
    previous = registry.publish_generation(
        registry.stage_generation(first.manifest, first.payloads)
    )
    assert registry.resolve("DemoConfiguration").configuration.config.get(
        "ОбщаяФорма.Workspace"
    ) is None

    registry.publish_generation(
        registry.stage_generation(second.manifest, second.payloads),
        expected_previous=previous,
    )

    found = search_objects(
        registry,
        "ОбщаяФорма.Workspace",
        config="DemoConfiguration",
        kind="ОбщаяФорма",
    )
    assert "`ОбщаяФорма.Workspace`" in found
    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert "`ОбщаяФорма.Workspace`" in search_objects(
        restarted,
        "ОбщаяФорма.Workspace",
        config="DemoConfiguration",
        kind="ОбщаяФорма",
    )


def test_native_журнал_покрытия_публикуется_и_восстанавливается_без_zip(
    tmp_path,
):
    collection, generation = _materialized(
        tmp_path,
        "coverage-journal",
        common_forms=True,
    )
    registry = Registry(tmp_path / "data")

    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )

    published = registry.modules["DemoConfiguration:modules"]
    journal_path = coverage_log.log_path(registry.data_dir, published.source.id)
    assert coverage_log.load_current(
        registry.data_dir,
        published.source,
        expected=coverage_log.build_payload(published),
    ) is not None

    shutil.rmtree(collection.root)
    shutil.rmtree(generation.root)
    journal_path.unlink()
    restarted = Registry(registry.data_dir)

    assert restarted.restore() == []
    restored = restarted.modules["DemoConfiguration:modules"]
    assert coverage_log.load_current(
        restarted.data_dir,
        restored.source,
        expected=coverage_log.build_payload(restored),
    ) is not None


@pytest.mark.parametrize(
    ("manifest_changes", "allow_truncated"),
    [
        ({"truncated": True}, True),
        ({"predefined_available": False}, False),
    ],
    ids=("truncated", "predefined-unavailable"),
)
def test_native_base_не_принимает_неполную_schema_v1(
    tmp_path,
    manifest_changes,
    allow_truncated,
):
    _collection_value, generation = _materialized(tmp_path, "complete-base")
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )
    before = registry.active_generation_pointer(generation.manifest.identity)
    source = tmp_path / "source-a-incomplete"
    source.mkdir()
    target = _rewrite_manifest(
        write_export(
            source,
            Configuration(
                name="DemoConfiguration",
                objects={
                    "Справочник.Replacement": MetadataObject(
                        full_name="Справочник.Replacement",
                        kind="Справочник",
                        name="Replacement",
                    )
                },
            ),
        ),
        **manifest_changes,
    )

    with pytest.raises(RegistryError, match="неполную schema v1"):
        registry.add_configuration(
            target,
            allow_truncated=allow_truncated,
        )

    assert registry.active_generation_pointer(generation.manifest.identity) == before
    assert registry.resolve("DemoConfiguration").configuration.config.get(
        "Справочник.Items"
    ) is not None


def test_native_compiled_модуль_поднимается_из_warm_кэша(
    tmp_path,
    monkeypatch,
):
    _collection_value, generation = _materialized(
        tmp_path,
        "compiled-cache",
        compiled_module=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )
    published = registry.modules["DemoConfiguration:modules"]
    assert published.каталог.entries["ОбщийМодуль.Sealed"].compiled is True

    restarted = Registry(registry.data_dir)

    def reject_cold_rebuild(*_args, **_kwargs):
        pytest.fail("compiled member native generation обязан подняться из кэша")

    monkeypatch.setattr(
        restarted,
        "_построить_индекс_кода",
        reject_cold_rebuild,
    )

    assert restarted.restore() == []
    restored = restarted.modules["DemoConfiguration:modules"]
    assert restored.каталог.entries["ОбщийМодуль.Sealed"].compiled is True


def test_native_opaque_модуль_поднимается_из_warm_кэша(
    tmp_path,
    monkeypatch,
):
    _collection_value, generation = _materialized(
        tmp_path,
        "opaque-cache",
        opaque_common_module=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )
    published = registry.modules["DemoConfiguration:modules"]
    assert published.оглавление.скомпилирован("ОбщийМодуль.Sealed") is True

    restarted = Registry(registry.data_dir)

    def reject_cold_rebuild(*_args, **_kwargs):
        pytest.fail("opaque member native generation обязан подняться из кэша")

    monkeypatch.setattr(
        restarted,
        "_построить_индекс_кода",
        reject_cold_rebuild,
    )

    assert restarted.restore() == []
    restored = restarted.modules["DemoConfiguration:modules"]
    assert restored.оглавление.скомпилирован("ОбщийМодуль.Sealed") is True
    assert restored.каталог.entries["ОбщийМодуль.Sealed"].opaque is True


def test_schema_v1_после_native_заменяет_только_base_и_переживает_restart(
    tmp_path,
):
    collection, generation = _materialized(
        tmp_path,
        "source-a-update",
        common_forms=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )
    previous_pointer = registry.active_generation_pointer(
        generation.manifest.identity
    )
    previous_layers = {
        layer.kind: layer for layer in generation.manifest.layers
    }
    incoming = tmp_path / "source-a"
    incoming.mkdir()
    source_a = convert_collection(collection).base
    source_a.version = "2.0"
    source_a.platform = "8.3.27.1000"
    source_a.source_format = "json"

    registry.add_configuration(write_export(incoming, source_a))

    current_pointer = registry.active_generation_pointer(
        generation.manifest.identity
    )
    assert current_pointer is not None and current_pointer != previous_pointer
    current = registry.active_generation(generation.manifest.identity)
    current_layers = {layer.kind: layer for layer in current.layers}
    assert current_layers[LayerKind.BASE_STRUCTURE].provenance.profile is (
        LayerSourceProfile.SCHEMA_V1
    )
    for kind in (
        LayerKind.EXTENDED_STRUCTURE,
        LayerKind.CODE,
        LayerKind.FORMS,
        LayerKind.ROLES,
    ):
        assert current_layers[kind] == previous_layers[kind]
    context = registry.resolve("DemoConfiguration")
    assert context.configuration.config.version == "2.0"
    assert context.configuration.config.platform == "8.3.27.1000"
    assert context.configuration.config.exporter_version == "test"
    assert context.configuration.config.get("ОбщаяФорма.Workspace") is not None
    assert context.modules is not None and context.modules.готов
    assert context.roles is not None and context.roles.ready

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    restored = restarted.resolve("DemoConfiguration")
    assert restored.configuration.config.version == "2.0"
    assert restored.configuration.config.platform == "8.3.27.1000"
    assert restored.configuration.config.exported_at == "2026-08-15T00:00:00"
    assert restored.configuration.config.get("ОбщаяФорма.Workspace") is not None
    assert restored.modules is not None and restored.modules.готов
    assert restored.roles is not None and restored.roles.ready


def test_повторная_идентичная_schema_v1_поверх_native_не_меняет_generation(
    tmp_path,
):
    collection, generation = _materialized(
        tmp_path,
        "source-a-no-op",
        common_forms=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )
    incoming = tmp_path / "source-a"
    incoming.mkdir()
    source_a = convert_collection(collection).base
    source_a_path = write_export(incoming, source_a)

    first_source = registry.add_configuration(source_a_path)
    first_pointer = registry.active_generation_pointer(generation.manifest.identity)
    first_manifest = registry.active_generation(generation.manifest.identity)

    repeated_source = registry.add_configuration(source_a_path)

    assert registry.active_generation_pointer(generation.manifest.identity) == (
        first_pointer
    )
    assert registry.active_generation(generation.manifest.identity) == first_manifest
    assert repeated_source == first_source


def test_schema_v1_не_публикуется_поверх_несовместимого_native(tmp_path):
    _collection_value, generation = _materialized(
        tmp_path,
        "source-a-mismatch",
        common_forms=True,
    )
    registry = Registry(tmp_path / "data")
    previous = registry.publish_generation(
        registry.stage_generation(generation.manifest, generation.payloads)
    )
    incoming = tmp_path / "source-a"
    incoming.mkdir()
    incompatible = Configuration(
        name="DemoConfiguration",
        version="2.0",
        source_format="json",
        objects={
            "Справочник.Replacement": MetadataObject(
                full_name="Справочник.Replacement",
                kind="Справочник",
                name="Replacement",
                attributes=[Field("Code", types=["Строка"])],
            )
        },
    )

    with pytest.raises(RegistryError, match="несинхронная пара Source A/Source B"):
        registry.add_configuration(write_export(incoming, incompatible))

    assert registry.active_generation_pointer(generation.manifest.identity) == previous
    context = registry.resolve("DemoConfiguration")
    assert context.configuration.config.get("Справочник.Items") is not None
    assert context.configuration.config.get("Справочник.Replacement") is None


def test_runtime_failure_до_commit_сохраняет_прежний_pointer_и_runtime(
    tmp_path,
    monkeypatch,
):
    _first_collection, first = _materialized(tmp_path, "001")
    _second_collection, second = _materialized(
        tmp_path,
        "002",
        common_forms=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(first.manifest, first.payloads)
    )
    old_pointer = registry.active_generation_pointer(first.manifest.identity)
    old_configuration = registry.resolve("DemoConfiguration").configuration
    staged = registry.stage_generation(second.manifest, second.payloads)

    def fail_runtime(*_args, **_kwargs):
        raise RegistryError("синтетический отказ runtime")

    monkeypatch.setattr(registry, "_build_native_generation_runtime", fail_runtime)
    with pytest.raises(RegistryError, match="отказ runtime"):
        registry.publish_generation(staged, expected_previous=old_pointer)

    assert registry.active_generation_pointer(first.manifest.identity) == old_pointer
    assert registry.resolve("DemoConfiguration").configuration is old_configuration
    assert not staged.root.exists()


def test_авария_сборки_runtime_оставляет_wal_и_откатывается_после_restart(
    tmp_path,
    monkeypatch,
):
    _first_collection, first = _materialized(tmp_path, "001")
    _second_collection, second = _materialized(
        tmp_path,
        "002",
        common_forms=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(first.manifest, first.payloads)
    )
    old_pointer = registry.active_generation_pointer(first.manifest.identity)
    staged = registry.stage_generation(second.manifest, second.payloads)

    def crash_runtime(*_args, **_kwargs):
        raise SystemExit("синтетическая авария runtime")

    monkeypatch.setattr(registry, "_build_native_generation_runtime", crash_runtime)
    with pytest.raises(SystemExit, match="авария runtime"):
        registry.publish_generation(staged, expected_previous=old_pointer)

    assert registry.generation_recovery_path.is_file()
    restarted = Registry(registry.data_dir)
    assert restarted.recover_generation_publish() == [
        "generation generation-002: staging откачен"
    ]
    assert restarted.restore() == []
    assert restarted.active_generation_pointer(first.manifest.identity) == old_pointer
    assert restarted.resolve("DemoConfiguration").configuration.config.name == (
        "DemoConfiguration"
    )
    assert not staged.root.exists()


def test_repacked_generation_сохраняет_identity_кэша_структуры_и_кода(
    tmp_path,
    monkeypatch,
):
    _collection_value, first = _materialized(tmp_path, "001", common_forms=True)
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(first.manifest, first.payloads)
    )
    first_context = registry.resolve("DemoConfiguration")

    second_manifest = type(first.manifest)(
        format_version=first.manifest.format_version,
        generation_id="generation-roles",
        identity=first.manifest.identity,
        parser_version=first.manifest.parser_version,
        selection_version=first.manifest.selection_version,
        source_transport=first.manifest.source_transport,
        origin_name=first.manifest.origin_name,
        raw_sha256="e" * 64,
        layers=first.manifest.layers,
    )
    pointer = registry.active_generation_pointer(first.manifest.identity)
    assert pointer is not None
    payloads = registry.generation_payload_sources(pointer)

    def reject_rebuild(*_args, **_kwargs):
        pytest.fail("перепаковка без semantic change не должна строить код заново")

    monkeypatch.setattr(registry, "_построить_индекс_кода", reject_rebuild)

    registry.publish_generation(
        registry.stage_generation(second_manifest, payloads),
        expected_previous=pointer,
    )
    second_context = registry.resolve("DemoConfiguration")

    assert (
        second_context.configuration.source.sha256
        == first_context.configuration.source.sha256
    )
    assert second_context.modules is not None and first_context.modules is not None
    assert second_context.modules.source.sha256 == first_context.modules.source.sha256
    assert (
        second_context.modules.source.locator_generation
        == first_context.modules.source.locator_generation
    )
