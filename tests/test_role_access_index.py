"""RED-контракты расходного индекса объявленных прав ролей.

Fixtures полностью синтетические. Индекс строится только из сохранённого
generation-слоя: исходный ZIP и рабочий ``data/`` этим тестам не нужны.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib
import shutil
from pathlib import Path

import pytest

from mcp1c.intake_v2 import (
    CandidateTransport,
    ExportIdentity,
    GenerationManifest,
    LayerKind,
    LayerManifest,
    LayerState,
)
from mcp1c.intake_v2_registry import (
    LayerMember,
    LayerMemberSource,
    LayerPayload,
    LayerPayloadSource,
    hash_layer_payload,
    hash_layer_semantic,
    load_layer_payload,
)
from mcp1c.registry import Registry


SUBJECT = "mcp1c.role_access"
NS = "http://v8.1c.ru/8.3/MDClasses"
CORE_NS = "http://v8.1c.ru/8.1/data/core"
RIGHTS_NS = "http://v8.1c.ru/8.2/roles"


def _symbol(name: str):
    try:
        module = importlib.import_module(SUBJECT)
    except ModuleNotFoundError as error:
        if error.name != SUBJECT:
            raise
        pytest.fail(f"RED: отсутствует модуль {SUBJECT} для контракта {name}")
    if not hasattr(module, name):
        pytest.fail(f"RED: в {SUBJECT} отсутствует контракт {name}")
    return getattr(module, name)


def _descriptor(
    name: str,
    *,
    uuid: str,
    comment: str = "",
    synonyms: tuple[tuple[str, str], ...] = (("ru", "Синтетическая роль"),),
) -> bytes:
    synonym = "".join(
        f"<v8:item><v8:lang>{language}</v8:lang>"
        f"<v8:content>{content}</v8:content></v8:item>"
        for language, content in synonyms
    )
    return (
        f'<MetaDataObject xmlns="{NS}" xmlns:v8="{CORE_NS}" version="2.20">'
        f'<Role uuid="{uuid}">'
        f"<Properties><Name>{name}</Name><Synonym>{synonym}</Synonym>"
        f"<Comment>{comment}</Comment></Properties></Role></MetaDataObject>"
    ).encode()


def _restriction(condition: str, *, field: str = "") -> str:
    field_xml = f"<field>{field}</field>" if field else ""
    return (
        "<restrictionByCondition>"
        f"{field_xml}<condition>{condition}</condition>"
        "</restrictionByCondition>"
    )


def _right(name: str, value: bool, *, condition: str = "", field: str = "") -> str:
    restriction = _restriction(condition, field=field) if condition else ""
    return (
        f"<right><name>{name}</name><value>{str(value).lower()}</value>"
        f"{restriction}</right>"
    )


def _rights(
    objects: tuple[tuple[str, tuple[str, ...]], ...] = (),
    *,
    set_for_new: bool = False,
    set_for_attributes: bool = True,
    independent_children: bool = False,
    templates: tuple[tuple[str, str], ...] = (),
) -> bytes:
    object_xml = "".join(
        f"<object><name>{target}</name>{''.join(rights)}</object>"
        for target, rights in objects
    )
    template_xml = "".join(
        f"<restrictionTemplate><name>{name}</name><condition>{condition}</condition>"
        "</restrictionTemplate>"
        for name, condition in templates
    )
    return (
        f'<Rights xmlns="{RIGHTS_NS}" version="2.20">'
        f"<setForNewObjects>{str(set_for_new).lower()}</setForNewObjects>"
        f"<setForAttributesByDefault>{str(set_for_attributes).lower()}"
        "</setForAttributesByDefault>"
        f"<independentRightsOfChildObjects>{str(independent_children).lower()}"
        "</independentRightsOfChildObjects>"
        f"{object_xml}{template_xml}</Rights>"
    ).encode()


def _role_layer(
    root: Path,
    roles: tuple[tuple[str, bytes, bytes], ...],
) -> tuple[LayerManifest, dict[str, bytes]]:
    root.mkdir(parents=True)
    semantic_artifacts = []
    members = []
    saved: dict[str, bytes] = {}
    member_ordinal = 0
    for name, descriptor, rights in roles:
        for source_path, raw in (
            (f"Roles/{name}.xml", descriptor),
            (f"Roles/{name}/Ext/Rights.xml", rights),
        ):
            compressed = gzip.compress(raw, compresslevel=1, mtime=0)
            relative_path = f"payload/roles/{member_ordinal:08d}.xml.gz"
            member_ordinal += 1
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(compressed)
            semantic_artifacts.append(
                {
                    "source_path": source_path,
                    "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
            members.append(
                LayerMember(
                    key=source_path,
                    relative_path=relative_path,
                    size=len(compressed),
                    sha256=hashlib.sha256(compressed).hexdigest(),
                )
            )
            saved[relative_path] = compressed
    semantic = {
        "roles_total": len(roles),
        "artifacts": sorted(semantic_artifacts, key=lambda item: item["source_path"]),
    }
    payload = LayerPayload(LayerKind.ROLES, semantic, tuple(members))
    layer_path = root / "layers/roles.json"
    layer_path.parent.mkdir(parents=True)
    layer_path.write_bytes(payload.to_json_bytes())
    return (
        LayerManifest(
            kind=LayerKind.ROLES,
            state=LayerState.READY,
            content_sha256=hash_layer_semantic(LayerKind.ROLES, semantic),
            payload_sha256=hash_layer_payload(LayerKind.ROLES, layer_path),
            relative_path="layers/roles.json",
            items_total=len(roles),
        ),
        saved,
    )


def _open(root: Path, layer: LayerManifest, cache: Path):
    RoleAccessIndex = _symbol("RoleAccessIndex")
    return RoleAccessIndex.open_or_build(root, layer, cache)


def _generation(
    root: Path,
    roles: tuple[tuple[str, bytes, bytes], ...],
    *,
    generation_id: str,
    configuration_name: str = "DemoConfiguration",
):
    roles_layer, _saved = _role_layer(root, roles)
    base_semantic = {
        "name": configuration_name,
        "synonym": "Синтетическая конфигурация",
        "version": "1.0",
        "vendor": "Example",
        "schema_version": "1",
        "objects": [],
    }
    base_payload = LayerPayload(LayerKind.BASE_STRUCTURE, base_semantic)
    base_path = root / "base-source.json"
    base_path.write_bytes(base_payload.to_json_bytes())
    base_layer = LayerManifest(
        kind=LayerKind.BASE_STRUCTURE,
        state=LayerState.READY,
        content_sha256=hash_layer_semantic(LayerKind.BASE_STRUCTURE, base_semantic),
        payload_sha256=hash_layer_payload(LayerKind.BASE_STRUCTURE, base_path),
        relative_path="layers/base-structure.json",
        items_total=0,
    )
    roles_payload = load_layer_payload(root / roles_layer.relative_path)
    roles_source = LayerPayloadSource(
        root / roles_layer.relative_path,
        tuple(
            LayerMemberSource(member, root / member.relative_path)
            for member in roles_payload.members
        ),
    )
    manifest = GenerationManifest(
        format_version=1,
        generation_id=generation_id,
        identity=ExportIdentity.configuration(configuration_name),
        parser_version=1,
        selection_version=1,
        source_transport=CandidateTransport.INCOMING,
        origin_name="synthetic-export.zip",
        raw_sha256=("a" if generation_id.endswith("1") else "b") * 64,
        layers=(
            base_layer,
            LayerManifest(LayerKind.EXTENDED_STRUCTURE, LayerState.UNAVAILABLE),
            LayerManifest(LayerKind.FORMS, LayerState.UNAVAILABLE),
            LayerManifest(LayerKind.CODE, LayerState.UNAVAILABLE),
            roles_layer,
        ),
    )
    return manifest, {
        LayerKind.BASE_STRUCTURE: LayerPayloadSource(base_path),
        LayerKind.ROLES: roles_source,
    }


def test_index_сохраняет_descriptor_права_rls_шаблоны_и_пустую_роль(tmp_path):
    analyst_rights = _rights(
        (
            (
                "Catalog.Orders.Attribute.Code",
                (
                    _right(
                        "Read",
                        True,
                        condition="Allowed = true",
                        field="Catalog.Orders.Attribute.Code",
                    ),
                    _right("Update", False),
                ),
            ),
        ),
        set_for_new=True,
        set_for_attributes=False,
        independent_children=True,
        templates=(("RowFilter", "Tenant = &amp;CurrentTenant"),),
    )
    layer, _saved = _role_layer(
        tmp_path / "generation",
        (
            (
                "Analyst",
                _descriptor(
                    "Analyst",
                    uuid="11111111-1111-1111-1111-111111111111",
                    comment="Только синтетический пример",
                    synonyms=(("ru", "Аналитик"), ("en", "Analyst")),
                ),
                analyst_rights,
            ),
            (
                "Empty",
                _descriptor(
                    "Empty",
                    uuid="22222222-2222-2222-2222-222222222222",
                    synonyms=(("ru", "Пустая роль"),),
                ),
                _rights(),
            ),
        ),
    )

    index = _open(tmp_path / "generation", layer, tmp_path / "roles.sqlite")
    try:
        assert [role.name for role in index.list_roles()] == ["Analyst", "Empty"]
        assert [
            role.name for role in index.list_roles(offset=1, limit=1)
        ] == ["Empty"]
        analyst = index.get_role("analyst")
        assert analyst.uuid == "11111111-1111-1111-1111-111111111111"
        assert analyst.synonyms == (("en", "Analyst"), ("ru", "Аналитик"))
        assert analyst.comment == "Только синтетический пример"
        assert analyst.set_for_new_objects is True
        assert analyst.set_for_attributes_by_default is False
        assert analyst.independent_rights_of_child_objects is True

        page = index.role_access(
            "Analyst",
            include_restrictions=True,
            offset=0,
            limit=10,
        )
        assert [(right.target, right.name, right.value) for right in page.rights] == [
            ("Catalog.Orders.Attribute.Code", "Read", True),
            ("Catalog.Orders.Attribute.Code", "Update", False),
        ]
        assert page.rights[0].restrictions[0].condition == "Allowed = true"
        assert page.rights[0].restrictions[0].field == (
            "Catalog.Orders.Attribute.Code"
        )
        compact_page = index.role_access("Analyst", offset=0, limit=10)
        assert compact_page.rights[0].conditional is True
        assert compact_page.rights[0].restrictions == ()
        assert compact_page.templates == ()
        assert page.templates == (("RowFilter", "Tenant = &CurrentTenant"),)
        assert page.next_offset is None
        assert index.role_access("Empty", offset=0, limit=10).rights == ()

        assert index.summary.roles == 2
        assert index.summary.targets == 1
        assert index.summary.rights == 2
        assert index.summary.restrictions == 1
        assert index.summary.templates == 1
        assert index.summary.conditions == 2
    finally:
        index.close()


def test_resolver_использует_явные_true_и_доказывает_минимальное_покрытие(
    tmp_path,
):
    roles = (
        (
            "Reader",
            _descriptor("Reader", uuid="11111111-1111-1111-1111-111111111111"),
            _rights((("Catalog.Orders", (_right("Read", True), _right("Update", False))),)),
        ),
        (
            "Editor",
            _descriptor("Editor", uuid="22222222-2222-2222-2222-222222222222"),
            _rights((("Catalog.Orders", (_right("Update", True),)),)),
        ),
        (
            "ConditionalFull",
            _descriptor(
                "ConditionalFull",
                uuid="33333333-3333-3333-3333-333333333333",
            ),
            _rights(
                ((
                    "Catalog.Orders",
                    (
                        _right("Read", True, condition="Allowed"),
                        _right("Update", True, condition="Allowed"),
                    ),
                ),)
            ),
        ),
    )
    layer, _saved = _role_layer(tmp_path / "generation", roles)
    index = _open(tmp_path / "generation", layer, tmp_path / "roles.sqlite")
    try:
        result = index.find_roles_for_access(
            "Справочник.Orders",
            ("read", "update"),
            include_conditional=False,
            limit=10,
        )
        assert result.source_target == "Catalog.Orders"
        assert result.checked_rights == (("read", "Read"), ("update", "Update"))
        assert result.minimal_role_set == ("Editor", "Reader")
        assert result.minimum_proof == "explicit_unconditional"
        by_name = {candidate.role.name: candidate for candidate in result.candidates}
        assert by_name["Reader"].matched_operations == ("read",)
        assert by_name["Reader"].missing_operations == ("update",)
        assert by_name["Reader"].denied_operations == ("update",)
        assert "ConditionalFull" not in by_name

        conditional = index.find_roles_for_access(
            "Справочник.Orders",
            ("read", "update"),
            include_conditional=True,
            limit=10,
        )
        assert conditional.minimal_role_set == ("ConditionalFull",)
        assert conditional.minimum_proof == "explicit_with_conditions"
        assert any("RLS" in warning for warning in conditional.warnings)
        full = next(
            candidate
            for candidate in conditional.candidates
            if candidate.role.name == "ConditionalFull"
        )
        assert full.conditional_operations == ("read", "update")
        assert full.complete is True
    finally:
        index.close()


def test_child_path_ищется_точно_и_не_смешивается_с_корневым_объектом(tmp_path):
    layer, _saved = _role_layer(
        tmp_path / "generation",
        ((
            "ChildReader",
            _descriptor(
                "ChildReader",
                uuid="11111111-1111-1111-1111-111111111111",
            ),
            _rights((("Catalog.Orders.Attribute.Code", (_right("Read", True),)),)),
        ),),
    )
    index = _open(tmp_path / "generation", layer, tmp_path / "roles.sqlite")
    try:
        assert not index.find_roles_for_access(
            "Справочник.Orders", ("read",), limit=10
        ).candidates
        child = index.find_roles_for_access(
            "Справочник.Orders",
            ("read",),
            child_path="Attribute.Code",
            limit=10,
        )
        assert child.source_target == "Catalog.Orders.Attribute.Code"
        assert child.minimal_role_set == ("ChildReader",)
    finally:
        index.close()


def test_cache_переживает_удаление_snapshot_а_повреждение_требует_пересборки(
    tmp_path,
):
    root = tmp_path / "generation"
    layer, saved = _role_layer(
        root,
        ((
            "Reader",
            _descriptor("Reader", uuid="11111111-1111-1111-1111-111111111111"),
            _rights((("Catalog.Orders", (_right("Read", True),)),)),
        ),),
    )
    cache = tmp_path / "roles.sqlite"
    first = _open(root, layer, cache)
    assert first.from_cache is False
    first.close()

    cache.write_bytes(b"not sqlite")
    rebuilt = _open(root, layer, cache)
    try:
        assert rebuilt.from_cache is False
        assert rebuilt.summary.rights == 1
    finally:
        rebuilt.close()

    for relative_path in saved:
        (root / relative_path).unlink()
    warm = _open(root, layer, cache)
    try:
        assert warm.from_cache is True
        assert warm.summary.rights == 1
    finally:
        warm.close()

    cache.write_bytes(b"not sqlite")
    RoleAccessError = _symbol("RoleAccessError")
    with pytest.raises(RoleAccessError, match="snapshot|member|payload"):
        _open(root, layer, cache)


def test_descriptor_с_неверным_namespace_не_публикуется_как_ready(tmp_path):
    root = tmp_path / "generation"
    descriptor = _descriptor(
        "Reader",
        uuid="11111111-1111-1111-1111-111111111111",
    ).replace(CORE_NS.encode(), b"urn:unexpected", 1)
    layer, _saved = _role_layer(
        root,
        (("Reader", descriptor, _rights()),),
    )

    RoleAccessError = _symbol("RoleAccessError")
    with pytest.raises(RoleAccessError, match="пространство имён"):
        _open(root, layer, tmp_path / "roles.sqlite")


def test_повреждённый_member_не_создаёт_доверенный_индекс(tmp_path):
    root = tmp_path / "generation"
    layer, saved = _role_layer(
        root,
        ((
            "Reader",
            _descriptor("Reader", uuid="11111111-1111-1111-1111-111111111111"),
            _rights((("Catalog.Orders", (_right("Read", True),)),)),
        ),),
    )
    first_member = root / next(iter(saved))
    first_member.write_bytes(first_member.read_bytes() + b"changed")

    RoleAccessError = _symbol("RoleAccessError")
    with pytest.raises(RoleAccessError, match="размер|хеш|gzip|member"):
        _open(root, layer, tmp_path / "roles.sqlite")


def test_pagination_детерминирована_и_unknown_operation_отклоняется(tmp_path):
    rights = tuple(
        (f"Catalog.Item{index}", (_right("Read", True),))
        for index in range(3)
    )
    layer, _saved = _role_layer(
        tmp_path / "generation",
        ((
            "Reader",
            _descriptor("Reader", uuid="11111111-1111-1111-1111-111111111111"),
            _rights(rights),
        ),),
    )
    index = _open(tmp_path / "generation", layer, tmp_path / "roles.sqlite")
    try:
        first = index.role_access("Reader", offset=0, limit=2)
        second = index.role_access(
            "Reader", offset=first.next_offset, limit=2
        )
        assert [right.target for right in first.rights] == [
            "Catalog.Item0",
            "Catalog.Item1",
        ]
        assert [right.target for right in second.rights] == ["Catalog.Item2"]
        assert second.next_offset is None
        with pytest.raises(ValueError, match="operation"):
            index.find_roles_for_access(
                "Справочник.Item0", ("unknown",), limit=10
            )
    finally:
        index.close()


def test_registry_атомарно_подключает_role_index_и_поднимает_cache_после_restart(
    tmp_path,
):
    source_root = tmp_path / "source"
    manifest, payloads = _generation(
        source_root,
        ((
            "Reader",
            _descriptor("Reader", uuid="11111111-1111-1111-1111-111111111111"),
            _rights((("Catalog.Orders", (_right("Read", True),)),)),
        ),),
        generation_id="generation-1",
    )
    registry = Registry(tmp_path / "data")

    registry.publish_generation(registry.stage_generation(manifest, payloads))

    roles = registry.resolve("DemoConfiguration").roles
    assert roles is not None and roles.state == "ready"
    assert roles.generation_id == "generation-1"
    assert roles.source_sha256 == "a" * 64
    assert roles.index is not None
    assert roles.index.find_roles_for_access(
        "Справочник.Orders", ("read",), limit=10
    ).minimal_role_set == ("Reader",)
    cache_path = roles.index.path
    assert cache_path.is_file()
    shutil.rmtree(source_root)

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    restored = restarted.resolve("DemoConfiguration").roles
    assert restored is not None and restored.index is not None
    assert restored.index.from_cache is True
    assert restored.index.path == cache_path


def test_ошибка_semantic_roles_не_ломает_остальные_слои_и_не_оставляет_старый_index(
    tmp_path,
):
    registry = Registry(tmp_path / "data")
    first, first_payloads = _generation(
        tmp_path / "first",
        ((
            "Reader",
            _descriptor("Reader", uuid="11111111-1111-1111-1111-111111111111"),
            _rights((("Catalog.Orders", (_right("Read", True),)),)),
        ),),
        generation_id="generation-1",
    )
    registry.publish_generation(registry.stage_generation(first, first_payloads))
    previous = registry.active_generation_pointer(first.identity)
    assert previous is not None
    malformed = (
        f'<Rights xmlns="{RIGHTS_NS}" version="2.20">'
        "<object><name>Catalog.Orders</name>"
        f"{_right('Read', True)}</object></Rights>"
    ).encode()
    second, second_payloads = _generation(
        tmp_path / "second",
        ((
            "Reader",
            _descriptor("Reader", uuid="11111111-1111-1111-1111-111111111111"),
            malformed,
        ),),
        generation_id="generation-2",
    )

    registry.publish_generation(
        registry.stage_generation(second, second_payloads),
        expected_previous=previous,
    )

    context = registry.resolve("DemoConfiguration")
    assert context.configuration is not None
    assert context.roles is not None
    assert context.roles.state == "error"
    assert context.roles.index is None
    assert "default-флаг" in context.roles.error
