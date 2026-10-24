"""Формулы id источников: это контракт на диске и в HTTP, менять нельзя."""

import pytest

from mcp1c import index_cache, source_ids
from mcp1c.intake_v2 import CandidateTransport, ExportIdentity, GenerationManifest
from mcp1c.intake_v2_registry import GenerationPointer


def test_формы_идентификаторов_совпадают_с_форматом_реестра():
    assert source_ids.modules_id("УТ") == "УТ:modules"
    assert source_ids.extension_prefix("УТ") == "УТ:ext:"
    assert source_ids.extension_modules_id("УТ", "Доп") == "УТ:ext:Доп"


def test_разбор_идентификатора_кода():
    assert source_ids.configuration_of_modules("УТ:modules") == "УТ"
    assert source_ids.configuration_of_modules("УТ") == "УТ"
    assert source_ids.is_code_source_id("УТ:modules")
    assert source_ids.is_code_source_id("УТ:ext:Доп")
    assert not source_ids.is_code_source_id("УТ")


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        pytest.param(
            ExportIdentity.configuration("Розница 2.3"),
            "Розница 2.3:modules",
            id="configuration",
        ),
        pytest.param(
            ExportIdentity.configuration("Розница/β_日本"),
            "Розница/β_日本:modules",
            id="configuration-name-preserved",
        ),
        pytest.param(
            ExportIdentity.extension("Доп", parent_configuration="УТ"),
            "УТ:ext:Доп",
            id="extension",
        ),
        pytest.param(
            ExportIdentity.extension(
                "Заказы/Мобильные:β_日本🔧", parent_configuration="Розница 2.3"
            ),
            "Розница 2.3:ext:Заказы_Мобильные_β_日本_",
            id="unicode-and-separators",
        ),
        pytest.param(
            ExportIdentity.extension("Ж" * 60, parent_configuration="УТ"),
            "УТ:ext:" + "Ж" * 60,
            id="utf8-120-bytes",
        ),
        pytest.param(
            ExportIdentity.extension("Ж" * 60 + "A", parent_configuration="УТ"),
            "УТ:ext:" + "Ж" * 55 + "_92d40986",
            id="utf8-121-bytes",
        ),
        pytest.param(
            ExportIdentity.extension("Ж" * 200 + "A", parent_configuration="УТ"),
            "УТ:ext:" + "Ж" * 55 + "_52c6093a",
            id="long-name-a",
        ),
        pytest.param(
            ExportIdentity.extension("Ж" * 200 + "B", parent_configuration="УТ"),
            "УТ:ext:" + "Ж" * 55 + "_c2ea31f7",
            id="long-name-b",
        ),
    ],
)
def test_id_корпуса_сохраняется_после_сериализации_identity_и_pointer(
    identity, expected
):
    manifest = GenerationManifest(
        format_version=1,
        generation_id="generation-001",
        identity=identity,
        parser_version=1,
        selection_version=1,
        source_transport=CandidateTransport.INCOMING,
        origin_name="export.zip",
        raw_sha256="a" * 64,
        layers=(),
    )
    serialized = manifest.to_json_bytes()
    restored = GenerationManifest.from_json_bytes(serialized)
    pointer = GenerationPointer.for_manifest(restored)
    pointer_payload = pointer.to_dict()
    restored_pointer = GenerationPointer.from_dict(pointer_payload)

    assert identity.modules_source_id() == expected
    assert restored.identity.modules_source_id() == expected
    assert restored_pointer.modules_source_id() == expected
    assert restored.to_json_bytes() == serialized
    assert set(pointer_payload) == {
        "identity", "generation_id", "manifest_path", "manifest_sha256", "root_path"
    }
    assert restored_pointer.to_dict() == pointer_payload


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("", ""),
        (".", "."),
        ("..", ".."),
        ("Розница 2.3-β_日本", "Розница 2.3-β_日本"),
        ("Заказы/Мобильные:β_日本🔧", "Заказы_Мобильные_β_日本_"),
    ],
)
def test_совместимый_safe_name_сохраняет_короткие_имена_и_границы(name, expected):
    assert index_cache.safe_name(name) == expected


def test_путь_кэша_сохраняет_существующий_формат_id(tmp_path):
    identity = ExportIdentity.extension("Заказы/β_日本", parent_configuration="УТ")

    path = index_cache.path_for(tmp_path, identity.modules_source_id(), "modules-toc")

    assert path == tmp_path / "УТ_ext_Заказы_β_日本.modules-toc"
