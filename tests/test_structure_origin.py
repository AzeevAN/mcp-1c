"""Происхождение эффективной структуры из файловых выгрузок расширений.

Тесты используют только синтетические имена и архивы.  Schema v1 намеренно
уже содержит итоговое поле: новый слой объясняет его происхождение, а не
меняет контракт структурной выгрузки.
"""

from __future__ import annotations

import copy
import gzip
import time
import zipfile
from pathlib import Path

from starlette.applications import Starlette

from conftest import build_configuration, write_export, живой_клиент, состарить
from mcp1c import dashboard_backend as dashboard
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.intake_v2_api import IntakeApiService
from mcp1c.model import Field
from mcp1c.registry import Registry
from native_export import старый_реестр


_NS = "http://v8.1c.ru/8.3/MDClasses"
_CONFIG = "ТестоваяКонфигурация"
_OBJECT = "Справочник.Контрагенты"
_FIELD = "ДополнительныйКод"


def _configuration_xml(
    *,
    extension: str = "",
    catalogs: tuple[str, ...] = ("Контрагенты",),
) -> str:
    if extension:
        properties = (
            f"<Name>{extension}</Name><NamePrefix>{extension}_</NamePrefix>"
            "<ObjectBelonging>Adopted</ObjectBelonging>"
            "<ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose>"
        )
    else:
        properties = (
            f"<Name>{_CONFIG}</Name><Version>1.0</Version><NamePrefix/>"
            "<CompatibilityMode>Version8_3_21</CompatibilityMode>"
        )
    children = "".join(f"<Catalog>{name}</Catalog>" for name in catalogs)
    return (
        f'<MetaDataObject xmlns="{_NS}">'
        '<Configuration uuid="00000000-0000-0000-0000-000000000000">'
        f"<Properties>{properties}</Properties>"
        f"<ChildObjects>{children}</ChildObjects>"
        "</Configuration></MetaDataObject>"
    )


def _catalog_xml(
    fields: tuple[str, ...],
    *,
    name: str = "Контрагенты",
    adopted: bool = False,
) -> str:
    belonging = "<ObjectBelonging>Adopted</ObjectBelonging>" if adopted else ""
    attributes = "".join(
        "<Attribute uuid=\"00000000-0000-0000-0000-000000000000\">"
        f"<Properties><Name>{name}</Name></Properties></Attribute>"
        for name in fields
    )
    return (
        f'<MetaDataObject xmlns="{_NS}">'
        '<Catalog uuid="00000000-0000-0000-0000-000000000000">'
        f"<Properties><Name>{name}</Name>{belonging}</Properties>"
        f"<ChildObjects>{attributes}</ChildObjects>"
        "</Catalog></MetaDataObject>"
    )


def _archive(
    tmp_path: Path,
    filename: str,
    *,
    fields: tuple[str, ...],
    extension: str = "",
) -> Path:
    archive = tmp_path / filename
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Configuration.xml", _configuration_xml(extension=extension))
        zf.writestr(
            "Catalogs/Контрагенты.xml",
            _catalog_xml(fields, adopted=bool(extension)),
        )
        zf.writestr(
            "Catalogs/Контрагенты/Ext/ObjectModule.bsl",
            "Процедура Проверка() Экспорт\nКонецПроцедуры\n",
        )
    return archive


def test_source_a_не_создаёт_смешанный_overlay_через_legacy_http(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "synthetic-admin-token")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setattr(dashboard, "JOBS", [])
    registry = Registry(tmp_path / "data")
    service = IntakeApiService.for_registry(registry, directory_settle_seconds=0)
    base_archive = _archive(
        tmp_path,
        "base-native.zip",
        fields=("ИНН", "Телефон"),
    )
    with base_archive.open("rb") as stream:
        candidate = service.accept_upload(
            base_archive.name,
            stream,
            expected_size=base_archive.stat().st_size,
        )
    work = service.start(candidate["id"], "create")
    service.prepare(work)
    service.confirm(work.job_id)

    native_catalog = registry.resolve(_CONFIG).modules.структура
    assert native_catalog is not None and native_catalog.complete
    assert f"{_OBJECT}.{_FIELD}" not in native_catalog.fields

    source_a = copy.deepcopy(registry.resolve(_CONFIG).configuration.config)
    for obj in source_a.objects.values():
        obj.attributes[:] = [field for field in obj.attributes if not field.standard]
    source_a.get(_OBJECT).attributes.append(Field(name=_FIELD))
    source_a.source_format = "json"
    source_a.exporter_version = "test"
    source_a.platform = "8.3.21"
    source_a_root = tmp_path / "source-a"
    source_a_root.mkdir()
    registry.add_configuration(write_export(source_a_root, source_a))

    assert registry.resolve(_CONFIG).modules is None

    registry.incoming_dir.mkdir(exist_ok=True)
    extension = состарить(
        _archive(
            registry.incoming_dir,
            "extension.zip",
            fields=("ИНН", "Телефон", _FIELD),
            extension="Дополнение",
        )
    )

    def parse_with_legacy_http(target_registry):
        client = живой_клиент(
            Starlette(routes=routes(target_registry, mode=DASHBOARD_ON))
        )
        login = client.post(
            "/login",
            data={"token": "synthetic-admin-token"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        response = client.post(
            "/api/v1/sources/incoming/parse",
            json={"name": extension.name, "configuration": _CONFIG},
            follow_redirects=False,
        )
        assert response.status_code == 410
        assert target_registry.resolve(_CONFIG).modules is None
        assert "Дополнение" not in target_registry.snapshot().extension_names(_CONFIG)

    parse_with_legacy_http(registry)

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert restarted.resolve(_CONFIG).modules is None
    assert "Дополнение" not in restarted.snapshot().extension_names(_CONFIG)

    monkeypatch.setattr(dashboard, "JOBS", [])
    parse_with_legacy_http(restarted)


def test_служебный_каталог_не_считается_модулем_без_адреса(tmp_path):
    from mcp1c import structure_origin

    # Каталог происхождения лежит в корне кода только у data/ прежней версии.
    code = tmp_path / "code"
    module = code / "Catalogs" / "Контрагенты" / "Ext" / "ObjectModule.bsl"
    module.parent.mkdir(parents=True)
    module.write_text("Процедура Проверка() Экспорт\nКонецПроцедуры\n", encoding="utf-8")
    (code / structure_origin.CATALOG_FILE).write_bytes(gzip.compress(b"{}"))
    registry = Registry(
        старый_реестр(
            tmp_path / "old", code, configuration=build_configuration(name=_CONFIG)
        )
    )
    registry.startup()
    assert registry.wait_for_module_builds(timeout=10)

    coverage = registry.modules[f"{_CONFIG}:modules"].каталог.coverage

    assert coverage.total_candidates == 1
    assert coverage.unknown_address == 0


def test_повреждённый_каталог_даёт_unknown_а_не_роняет_restore(tmp_path):
    from mcp1c import structure_origin

    root = tmp_path / "code"
    root.mkdir()
    (root / structure_origin.CATALOG_FILE).write_bytes(gzip.compress(b"[]"))

    assert structure_origin.load(root) is None


