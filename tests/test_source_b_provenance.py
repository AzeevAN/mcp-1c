"""Source B не выдаёт неизвестные runtime-факты за проверенные сведения."""

from copy import deepcopy

import pytest

from conftest import write_export
from mcp1c.intake_v2_converter import base_layer_data, convert_collection
from mcp1c.intake_v2 import LayerSourceProfile
from mcp1c.intake_v2_runtime import configuration_from_base_layer
from mcp1c.registry import Registry
from mcp1c.store import save_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem
from mcp1c.tools import get_object, list_configurations, search_syntax
from test_intake_v2_runtime import _materialized
from test_dashboard_admin_api import _client
import test_intake_v2_converter as fixtures


def _syntax(registry, root):
    syntax = SyntaxIndex(platforms=["8.3.27.2130"], source="test")
    syntax.add(SyntaxItem(
        id="ctx/FutureMethod", kind="method", name_ru="БудущийМетод",
        since="8.3.27", description="Синтетический метод",
    ))
    registry.add_syntax(save_syntax(syntax, root / "syntax.json.gz"))


@pytest.mark.parametrize("compatibility", ["Version8_3_24", "DontUse"])
def test_b_only_явно_сообщает_неизвестные_факты_и_переживает_restart(
    tmp_path, monkeypatch, compatibility,
):
    monkeypatch.delenv("API_TOKEN", raising=False)
    original = fixtures._configuration
    monkeypatch.setattr(fixtures, "_configuration", lambda **kw: original(**kw).replace(
        b"Version8_3_24", compatibility.encode(),
    ))
    _, generation = _materialized(tmp_path, "b-only")
    registry = Registry(tmp_path / "data")
    registry.publish_generation(registry.stage_generation(generation.manifest, generation.payloads))
    _syntax(registry, tmp_path)
    registry.save()
    for current in (registry, Registry(registry.data_dir)):
        if current is not registry:
            assert current.restore() == []
        context = current.resolve("DemoConfiguration")
        config = context.configuration.config
        assert config.platform == ""
        assert config.compatibility_mode == compatibility
        assert config.predefined_available is False
        assert context.syntax_relation == "unknown"
        assert context.syntax_filter()(SyntaxItem(id="future", kind="method", name_ru="БудущийМетод", since="8.3.27"))
        listing = list_configurations(current)
        assert "версия совпадает" not in listing
        assert "не используется" not in listing
        assert current.syntax_coverage()["unused"] == []
        assert "Фактическая версия платформы неизвестна" in listing
        if compatibility:
            assert f"Режим совместимости: {compatibility}" in listing
        for response in (get_object(current, "Справочник.Items"), search_syntax(current, "БудущийМетод")):
            assert "Фактическая версия платформы неизвестна" in response
            assert "Сведения о предопределённых элементах не получены" in response
        row = current.overview()[0]
        assert row["platform"] == ""
        assert row["compatibility_mode"] == compatibility
        assert row["predefined_available"] is False
        assert row["syntax_relation"] == "unknown"
        response = _client(current).get("/api/v1/sources")
        assert response.status_code == 200
        api_row = response.json()["configurations"][0]
        for key in ("platform", "compatibility_mode", "predefined_available", "syntax_relation", "notes"):
            assert api_row[key] == row[key]


@pytest.mark.parametrize("legacy_platform", ["Version8_3_24", ""])
def test_старый_b_snapshot_читается_без_перезаписи_и_ложных_фактов(tmp_path, legacy_platform):
    collection, _ = _materialized(tmp_path, "old")
    semantic = base_layer_data(convert_collection(collection).base)
    semantic.pop("compatibility_mode", None)
    semantic.update(platform=legacy_platform, predefined_available=True)
    before = deepcopy(semantic)
    config = configuration_from_base_layer(semantic)
    assert config.platform == ""
    assert config.compatibility_mode == legacy_platform
    assert config.predefined_available is False
    assert semantic == before


def test_старое_поколение_v3_восстанавливается_без_перезаписи_bundle(tmp_path, monkeypatch):
    from hashlib import sha256
    import mcp1c.intake_v2_converter as converter
    import mcp1c.intake_v2_generation as generation_module

    real_data = converter.base_layer_data

    def old_data(base):
        payload = real_data(base)
        payload["platform"] = payload.pop("compatibility_mode")
        payload["predefined_available"] = True
        return payload

    with monkeypatch.context() as legacy:
        legacy.setattr(converter, "base_layer_data", old_data)
        legacy.setattr(generation_module, "base_layer_data", old_data)
        legacy.setattr(generation_module, "GENERATION_PARSER_VERSION", 3)
        _, generation = _materialized(tmp_path, "old-bundle")
    registry = Registry(tmp_path / "data")
    pointer = registry.publish_generation(registry.stage_generation(generation.manifest, generation.payloads))
    root = registry.data_dir / pointer.root_path
    before = {p.relative_to(root): sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}
    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    config = restarted.resolve("DemoConfiguration").configuration.config
    assert config.platform == ""
    assert config.compatibility_mode == "Version8_3_24"
    assert config.predefined_available is False
    assert before == {p.relative_to(root): sha256(p.read_bytes()).hexdigest() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("with_b", [False, True])
def test_source_a_сохраняет_доказанные_факты_в_том_числе_поверх_b(tmp_path, with_b):
    collection, generation = _materialized(tmp_path, "a")
    registry = Registry(tmp_path / "data")
    if with_b:
        registry.publish_generation(registry.stage_generation(generation.manifest, generation.payloads))
    base = convert_collection(collection).base
    base.platform = "8.3.24.1548"
    base.source_format = "json"
    base.predefined_available = True
    source = tmp_path / "schema-a"
    source.mkdir()
    registry.add_configuration(write_export(source, base))
    _syntax(registry, tmp_path)
    registry.save()
    for current in (registry, Registry(registry.data_dir)):
        if current is not registry:
            assert current.restore() == []
        context = current.resolve("DemoConfiguration")
        assert context.platform == "8.3.24.1548"
        assert context.configuration.config.predefined_available is True
        assert context.syntax_relation == "newer"
        assert "Фактическая версия платформы неизвестна" not in list_configurations(current)


def test_полная_публикация_b_после_a_не_приписывает_b_факты_a(tmp_path):
    collection, generation = _materialized(tmp_path, "replace-a")
    base = convert_collection(collection).base
    base.platform = "8.3.24.1548"
    base.source_format = "json"
    base.predefined_available = True
    source = tmp_path / "schema-a"
    source.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(source, base))
    # Проверяется существующая полная замена base, а не content-only update.
    registry.publish_generation(registry.stage_generation(generation.manifest, generation.payloads))
    _syntax(registry, tmp_path)
    registry.save()
    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    config = restarted.resolve("DemoConfiguration").configuration.config
    assert config.platform == ""
    assert config.predefined_available is False
    assert config.compatibility_mode == "Version8_3_24"


def test_сохранённый_schema_v1_не_теряет_предопределённые_и_версию(tmp_path):
    collection, _ = _materialized(tmp_path, "schema-payload")
    base = convert_collection(collection).base
    base.platform = "8.3.24.1548"
    base.predefined_available = True
    base.get("Справочник.Items").predefined = ["Основной"]
    raw = base_layer_data(base)
    # Provenance манифеста приоритетнее исторического source_format payload.
    restored = configuration_from_base_layer(raw, source_profile=LayerSourceProfile.SCHEMA_V1)
    assert restored.platform == "8.3.24.1548"
    assert restored.predefined_available is True
    assert restored.get("Справочник.Items").predefined == ["Основной"]


def test_пустой_compatibility_в_converter_не_превращается_в_runtime(tmp_path):
    import xml.etree.ElementTree as ET
    from mcp1c.intake_v2_converter import _configuration, _Diagnostics

    collection, _ = _materialized(tmp_path, "empty-mode")
    root = ET.fromstring(fixtures._configuration().replace(b"Version8_3_24", b""))
    # Probe намеренно не принимает новый неоднозначный вход без признака
    # конфигурации; отдельно проверяем семантику уже определённой identity.
    config = _configuration(root, collection, _Diagnostics())
    assert config.platform == ""
    assert config.compatibility_mode == ""
    assert config.predefined_available is False
