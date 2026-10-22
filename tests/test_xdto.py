"""Ресурсные границы и namespace scopes полного XDTO member."""

from __future__ import annotations

import pytest

from mcp1c import xdto
from mcp1c.model import Configuration, MetadataObject


def _package(full_name: str, namespace: str, **members: list[str]) -> MetadataObject:
    return MetadataObject(
        full_name=full_name,
        kind="ПакетXDTO",
        name=full_name.split(".", 1)[1],
        extended={"target_namespace": namespace, **members},
    )


@pytest.mark.parametrize(
    "order",
    [
        ("ПакетXDTO.Envelope", "ПакетXDTO.SOAP_Envelope_1_1"),
        ("ПакетXDTO.SOAP_Envelope_1_1", "ПакетXDTO.Envelope"),
    ],
    ids=["по_порядку", "в_обратном_порядке"],
)
def test_catalog_namespace_принадлежит_первому_по_имени_пакету(order):
    namespace = "http://schemas.xmlsoap.org/soap/envelope/"
    configuration = Configuration()
    for full_name in order:
        configuration.objects[full_name] = _package(
            full_name, namespace, object_types=["Envelope"]
        )

    assert xdto._catalog(configuration) == {
        namespace: {"type:Envelope": "ПакетXDTO.Envelope.ТипОбъекта.Envelope"}
    }


def test_catalog_порядок_имён_нечувствителен_к_регистру():
    configuration = Configuration()
    for full_name in ("ПакетXDTO.beta", "ПакетXDTO.Альфа", "ПакетXDTO.Alpha"):
        configuration.objects[full_name] = _package(
            full_name, "urn:shared", value_types=["Code"]
        )

    assert xdto._catalog(configuration) == {
        "urn:shared": {"type:Code": "ПакетXDTO.Alpha.ТипЗначения.Code"}
    }


def test_namespace_scope_переиспользуется_и_локально_переопределяется():
    payload = (
        '<package xmlns="http://v8.1c.ru/8.1/xdto" xmlns:t="urn:root" '
        'targetNamespace="urn:main"><property name="First" type="t:Thing"/>'
        '<objectType name="Nested"><property xmlns:t="urn:local" '
        'name="Second" type="t:Thing"/></objectType>'
        '<property name="Third" type="t:Thing"/></package>'
    ).encode()

    root, namespaces = xdto._parse_with_namespaces(payload)
    properties = [
        element for element in root.iter() if element.tag.endswith("property")
    ]
    first, local, third = (namespaces[id(element)] for element in properties)

    assert first is third
    assert local is not first
    assert first["t"] == "urn:root"
    assert local["t"] == "urn:local"
    with pytest.raises(TypeError):
        first["t"] = "urn:changed"  # type: ignore[index]


@pytest.mark.parametrize(
    ("limit_name", "limit", "payload", "message"),
    [
        (
            "MAX_PACKAGE_SIZE",
            10,
            '<package xmlns="http://v8.1c.ru/8.1/xdto"/>',
            "размер XDTO package",
        ),
        (
            "MAX_PACKAGE_ELEMENTS",
            2,
            '<package xmlns="http://v8.1c.ru/8.1/xdto"><a/><b/></package>',
            "XML-элементов",
        ),
        (
            "MAX_NAMESPACE_DECLARATIONS",
            2,
            '<package xmlns="http://v8.1c.ru/8.1/xdto" xmlns:a="a" '
            'xmlns:b="b"/>',
            "объявлений namespace",
        ),
        (
            "MAX_NAMESPACE_SCOPE_SIZE",
            1,
            '<package xmlns="http://v8.1c.ru/8.1/xdto" xmlns:a="a"/>',
            "namespace в области",
        ),
        (
            "MAX_PACKAGE_DEPTH",
            2,
            '<package xmlns="http://v8.1c.ru/8.1/xdto"><a><b/></a></package>',
            "глубины XML",
        ),
    ],
)
def test_xdto_parser_останавливается_на_ресурсном_бюджете(
    monkeypatch, limit_name, limit, payload, message
):
    monkeypatch.setattr(xdto, limit_name, limit)

    with pytest.raises(xdto.XDTOReadError, match=message):
        xdto._parse_with_namespaces(payload.encode())
