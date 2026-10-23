"""Регрессия: get_object не выдаёт признак происхождения структуры.

Для B_FULL с native-расширением раньше каждая карточка получала ложную
оговорку «Происхождение структуры: неизвестно», а пометок «Объявлен
расширением» в B_FULL не бывает (связка Source A + расширение запрещена).
"""

from __future__ import annotations

import importlib
from dataclasses import replace

import pytest

from mcp1c import tools
from mcp1c.intake_v2 import LayerKind, LayerManifest, LayerState
from mcp1c.intake_v2_generation import MaterializedGeneration
from mcp1c.registry import Registry
from test_intake_v2_extensions import _materialized, _stage_active_base

ЗАПРЕЩЕННЫЕ = (
    "Происхождение структуры",
    "Объявлен расширением",
    "объявлен расширением",
    "Источник объекта неоднозначен",
)
DETAILS = ("fields", "full")
EXT_KEY = "DemoConfiguration:ext:DemoExtension"


def _без_кода(generation: MaterializedGeneration) -> MaterializedGeneration:
    """Расширение без слоя кода: CODE помечен unavailable, payload убран."""
    layers = tuple(
        LayerManifest(LayerKind.CODE, LayerState.UNAVAILABLE)
        if layer.kind is LayerKind.CODE
        else layer
        for layer in generation.manifest.layers
    )
    payloads = {
        kind: payload
        for kind, payload in generation.payloads.items()
        if kind is not LayerKind.CODE
    }
    return MaterializedGeneration(
        generation.root,
        replace(generation.manifest, layers=layers),
        payloads,
    )


def _реестр(tmp_path, *, расширение: bool, с_кодом: bool = True, name="data"):
    _c, base = _materialized(tmp_path, f"base-{name}")
    registry = Registry(tmp_path / name)
    registry.publish_generation(_stage_active_base(registry, base))
    if расширение:
        _c, ext = _materialized(
            tmp_path,
            f"ext-{name}",
            configuration_name="DemoExtension",
            extension=True,
        )
        if not с_кодом:
            ext = _без_кода(ext)
        registry.publish_generation(
            registry.stage_generation(ext.manifest, ext.payloads)
        )
    return registry


def _карточка(registry, detail):
    return tools.get_object(
        registry, "Справочник.Items", "DemoConfiguration", detail=detail
    )


def _проверить(registry):
    for detail in DETAILS:
        text = _карточка(registry, detail)
        assert "Items" in text
        for фраза in ЗАПРЕЩЕННЫЕ:
            assert фраза not in text, (detail, фраза)


@pytest.mark.parametrize("detail", DETAILS)
def test_b_full_с_расширением_и_кодом_без_признака_происхождения(tmp_path, detail):
    registry = _реестр(tmp_path, расширение=True)
    assert EXT_KEY in registry.modules
    text = _карточка(registry, detail)
    assert "Items" in text
    for фраза in ЗАПРЕЩЕННЫЕ:
        assert фраза not in text


def test_после_restore_без_признака_происхождения(tmp_path):
    registry = _реестр(tmp_path, расширение=True)
    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert EXT_KEY in restarted.modules
    _проверить(restarted)


def test_после_startup_без_признака_происхождения(tmp_path):
    registry = _реестр(tmp_path, расширение=True)
    restarted = Registry(registry.data_dir)
    restarted.startup()
    assert EXT_KEY in restarted.modules
    _проверить(restarted)


def test_расширение_без_слоя_кода_без_признака_происхождения(tmp_path):
    registry = _реестр(tmp_path, расширение=True, с_кодом=False)
    _проверить(registry)
    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    _проверить(restarted)


@pytest.mark.parametrize("detail", DETAILS)
def test_карточка_с_расширением_совпадает_с_карточкой_без_него(tmp_path, detail):
    с_расширением = _реестр(tmp_path, расширение=True, name="with")
    без = _реестр(tmp_path, расширение=False, name="without")
    expected = _карточка(без, detail)
    assert expected
    assert _карточка(с_расширением, detail) == expected


def test_модуль_structure_origin_удалён():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("mcp1c.structure_origin")
