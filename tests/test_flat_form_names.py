"""Имена форм не подменяются служебными компонентами плоского пути."""

import pytest

from mcp1c.intake_v2_collector import collect_source_b
from mcp1c.intake_v2_converter import convert_collection
from mcp1c.intake_v2_generation import materialize_generation
from mcp1c.intake_v2_probe import probe_export
from mcp1c.registry import Registry
from mcp1c.module_address import разобрать_плоскую_xml_форму
from test_intake_v2_collector import MemoryTree
from test_intake_v2_converter import _document, _common_form, _common_form_xml, _catalog


@pytest.mark.parametrize("path", ["CommonForm.Name.Form.Module.txt.xml", "Catalog.Items.xml", "CommonForm.Name.Help.xml"])
def test_похожие_суффиксы_не_становятся_структурой_формы(path):
    with pytest.raises(ValueError):
        разобрать_плоскую_xml_форму(path)


@pytest.mark.parametrize("name", ["Workspace", "Form", "Module"])
@pytest.mark.parametrize("complete", [False, True])
def test_плоская_форма_сохраняет_descriptor_и_структуру(tmp_path, name, complete):
    payloads = {
        "Configuration.xml": _document(
            "Configuration",
            "<Name>Demo</Name><Version>1.0</Version><CompatibilityMode>Version8_3_21</CompatibilityMode>",
            f"<CommonForm>{name}</CommonForm><Catalog>Items</Catalog>",
        ),
        "Catalog.Items.xml": _catalog(),
        f"CommonForm.{name}.xml": _common_form(name),
        f"Catalog.Items.Form.{name}.xml": _document("Form", f"<Name>{name}</Name>"),
    }
    prefixes = (f"CommonForm.{name}", f"Catalog.Items.Form.{name}")
    if complete:
        for prefix in prefixes:
            payloads[f"{prefix}.Form.xml"] = _common_form_xml()
            payloads[f"{prefix}.Form.Module.txt"] = b"procedure OnOpen() endprocedure"
    tree = MemoryTree(payloads)
    collection = collect_source_b(tree, probe_export(tree), tmp_path / "collection")
    assert {a.source_path for a in collection.forms} == {
        key for key in payloads if any(key.startswith(prefix + ".") for prefix in prefixes)
        and key.endswith(".xml")
    }
    generation = materialize_generation(collection, convert_collection(collection), tmp_path / "generation", generation_id="generation-names")
    registry = Registry(tmp_path / "data")
    registry.publish_generation(registry.stage_generation(generation.manifest, generation.payloads))
    for current in (registry, Registry(registry.data_dir)):
        if current is not registry:
            assert current.restore() == []
        forms = current.resolve("Demo").modules.формы
        for address in (f"ОбщаяФорма.{name}", f"Справочник.Items.Форма.{name}"):
            form = forms.состав(address)
            assert form is not None
            assert form.имя == name
            assert not form.битая
            assert form.состояние_xml == ("ready" if complete else "missing")
            if complete:
                assert "Filter" in form.реквизиты
