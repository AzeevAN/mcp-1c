"""Структурные слои поколения: writer converter-а и reader runtime согласованы.

Слой пишет `intake_v2_converter` (`base_layer_data`, `extended_layer_data`),
а читает `intake_v2_runtime` (`configuration_from_base_layer`,
`_compose_extended_structure`). Общего модуля схемы у них нет намеренно:
reader — не обратная функция writer-а. Он принимает и прежние наборы
полей base-слоя, переосмысляет `platform` для Source B и размечает ссылки
на определяемые типы; extended-слой он не восстанавливает в
`ExtendedStructure`, а проецирует на объекты runtime-каталога. Поэтому
согласованность закреплена здесь: всё, что пишет writer, reader принимает
без потерь в той части, которую он читает.
"""

from __future__ import annotations

import json

from mcp1c.intake_v2 import LayerSourceProfile
from mcp1c.intake_v2_converter import (
    base_layer_data,
    convert_collection,
    extended_layer_data,
)
from mcp1c.intake_v2_runtime import (
    _compose_extended_structure,
    configuration_from_base_layer,
)

import test_intake_v2_converter as cases


def _json_copy(value: object) -> object:
    # На диске слой лежит JSON-ом: кортежи становятся массивами.
    return json.loads(json.dumps(value, ensure_ascii=False))


def _conversion(tmp_path):
    return convert_collection(
        cases._collection(
            tmp_path,
            journal=True,
            bindings=True,
            common_forms=True,
            bots=True,
            http_services=True,
        )
    )


def test_base_слой_переживает_запись_и_чтение(tmp_path):
    conversion = _conversion(tmp_path)
    written = _json_copy(base_layer_data(conversion.base))

    restored = configuration_from_base_layer(
        written, source_profile=LayerSourceProfile.SOURCE_B
    )
    rewritten = _json_copy(base_layer_data(restored))

    # Source B не доказывает версию платформы: reader её сбрасывает.
    assert rewritten == {**written, "platform": ""}
    assert len(restored.objects) == len(conversion.base.objects) > 0


def test_extended_слой_целиком_принимается_reader_ом(tmp_path):
    conversion = _conversion(tmp_path)
    written = _json_copy(extended_layer_data(conversion.extended))
    configuration = configuration_from_base_layer(
        _json_copy(base_layer_data(conversion.base)),
        source_profile=LayerSourceProfile.SOURCE_B,
    )

    _compose_extended_structure(
        configuration, written, require_base_overlays=False
    )

    assert written["objects"], "фикстура должна давать extended-объекты"
    for item in written["objects"]:
        target = configuration.objects[item["full_name"]]
        assert target.kind == item["kind"]
        assert target.code_address == item["code_address"]
        assert target.forms == item["forms"]
        assert [
            [relation.kind, relation.target, relation.state,
             [list(pair) for pair in relation.properties]]
            for relation in target.relations
        ] == [
            [relation["kind"], relation["target"], relation["state"],
             relation["properties"]]
            for relation in item["relations"]
        ]
        assert target.extended == (item["payload"] or {})
