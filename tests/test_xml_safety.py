"""Серверные XML-парсеры отвергают DOCTYPE/ENTITY до раскрытия сущностей.

Выгрузки 1С DTD не содержат, а внутренний DTD с сущностями превращает
байтовый лимит входа в сотни мегабайт текста (expat ограничивает усиление
лишь примерно 100x). Поэтому пролог проверяется до любого разбора.
"""

from __future__ import annotations

import io
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from mcp1c import xml_safety
from mcp1c.xml_safety import XmlDeclarationError


SRC = Path(__file__).resolve().parents[1] / "src" / "mcp1c"

# 20 000 ссылок на сущность 1 КиБ: без проверки это ~20 МиБ текста из ~40 КиБ.
LAUGHS = (
    b'<?xml version="1.0"?>\n<!DOCTYPE r [<!ENTITY a "'
    + b"x" * 1024
    + b'">]>\n<r>'
    + b"&a;" * 20_000
    + b"</r>"
)

LEGAL = (
    "﻿<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
    "<!-- комментарий -->\n<?pi data?>\n"
    '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
    "<![CDATA[<!DOCTYPE в данных — не пролог>]]>"
    "<Text>&lt;!ENTITY x&gt;</Text></MetaDataObject>"
).encode("utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        LAUGHS,
        b"<!DOCTYPE r><r/>",
        b"\xef\xbb\xbf<?xml version='1.0'?><!-- c --><!DOCTYPE r SYSTEM 'x.dtd'><r/>",
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r><r/>'.encode("utf-16"),
        '<?xml version="1.0" encoding="windows-1251"?><!DOCTYPE r [<!ENTITY e "ы">]><r>&e;</r>'.encode(
            "cp1251"
        ),
    ],
    ids=["entity-laughs", "plain", "bom-comment-system", "utf-16", "cp1251"],
)
def test_doctype_в_прологе_отвергается_для_байтов_и_потока(payload):
    started = time.perf_counter()
    with pytest.raises(XmlDeclarationError, match="DOCTYPE"):
        xml_safety.fromstring(payload)
    with pytest.raises(XmlDeclarationError, match="DOCTYPE"):
        xml_safety.parse(io.BytesIO(payload))
    with pytest.raises(XmlDeclarationError, match="DOCTYPE"):
        list(xml_safety.iterparse(io.BytesIO(payload), events=("start",)))
    with pytest.raises(XmlDeclarationError, match="DOCTYPE"):
        xml_safety.canonicalize(from_file=io.BytesIO(payload))
    # Отказ приходит до раскрытия: на 20 000 сущностей это миллисекунды.
    assert time.perf_counter() - started < 1.0


def test_отказ_совместим_с_обработкой_ошибок_разбора():
    assert issubclass(XmlDeclarationError, ET.ParseError)


def test_легальный_xml_разбирается_как_раньше():
    root = xml_safety.fromstring(LEGAL)
    assert root.tag == "{http://v8.1c.ru/8.3/MDClasses}MetaDataObject"
    assert xml_safety.parse(io.BytesIO(LEGAL)).getroot().tag == root.tag
    events = list(xml_safety.iterparse(io.BytesIO(LEGAL), events=("start", "end")))
    assert [event for event, _ in events] == ["start", "start", "end", "end"]
    assert xml_safety.canonicalize(from_file=io.BytesIO(LEGAL)) == ET.canonicalize(
        from_file=io.BytesIO(LEGAL)
    )
    assert xml_safety.fromstring(LEGAL.decode("utf-8-sig")).tag == root.tag


def test_проверенный_поток_отдаёт_все_байты_без_seek(tmp_path):
    class OneWay(io.RawIOBase):
        def __init__(self, data: bytes):
            self._inner = io.BytesIO(data)

        def readable(self) -> bool:
            return True

        def readinto(self, buffer) -> int:
            chunk = self._inner.read(min(len(buffer), 7))
            buffer[: len(chunk)] = chunk
            return len(chunk)

        def seekable(self) -> bool:
            return False

    payload = LEGAL + b"<!-- " + b"y" * 100_000 + b" -->"
    checked = xml_safety.checked_stream(OneWay(payload))
    assert b"".join(iter(lambda: checked.read(4096), b"")) == payload

    path = tmp_path / "file.xml"
    path.write_bytes(LEGAL)
    assert xml_safety.parse(path).getroot().tag.endswith("MetaDataObject")
    assert len(list(xml_safety.iterparse(str(path)))) == 2


def test_битый_xml_оставляет_ошибку_самому_разборщику():
    with pytest.raises(ET.ParseError) as error:
        xml_safety.fromstring(b"<r><unclosed></r>")
    assert not isinstance(error.value, XmlDeclarationError)
    with pytest.raises(ET.ParseError):
        xml_safety.fromstring(b"")


def test_слишком_длинный_пролог_отвергается():
    payload = b"<!--" + b"x" * (xml_safety.PROLOG_LIMIT + 10) + b"--><r/>"
    with pytest.raises(XmlDeclarationError, match="пролог"):
        xml_safety.fromstring(payload)


# canonicalize от ET.tostring собственного дерева допустим: DOCTYPE в
# сериализованном ElementTree не бывает.
DIRECT_XML = re.compile(
    r"\bET\.(parse|iterparse|fromstring|fromstringlist|XML|XMLParser|"
    r"XMLPullParser)\(|\bET\.canonicalize\((?!\s*xml_data=ET\.tostring\()"
    r"|\bxml\.(dom|sax|parsers)\b|\bpyexpat\b"
    # Обход через другое имя модуля: ElementTree.parse(...), импорт
    # функций разбора напрямую, minidom, expat и lxml.
    r"|\bElementTree\.(parse|iterparse|fromstring|fromstringlist|XML|"
    r"XMLParser|XMLPullParser|canonicalize)\b"
    r"|\bfrom\s+xml\.etree(\.ElementTree|\.cElementTree)?\s+import\b"
    r"|\bimport\s+xml\.etree\.cElementTree\b"
    r"|\bminidom\b|\bexpat\b|\bParserCreate\b|\blxml\b"
    r"|\bimport\s+xml\.etree\.ElementTree(?!\s+as\s+ET\b)"
)


@pytest.mark.parametrize(
    "line",
    [
        "root = ET.fromstring(data)",
        "ET.parse(stream)",
        "for e in ET.iterparse(stream):",
        "ET.canonicalize(from_file=stream)",
        "tree = ElementTree.parse(path)",
        "from xml.etree.ElementTree import fromstring",
        "from xml.etree import ElementTree",
        "import xml.etree.ElementTree",
        "import xml.etree.cElementTree as ET",
        "from xml.dom import minidom",
        "parser = expat.ParserCreate()",
        "from pyexpat import ParserCreate",
        "import xml.sax",
        "from lxml import etree",
    ],
)
def test_страж_ловит_каждый_вариант_обхода(line):
    assert DIRECT_XML.search(line)


@pytest.mark.parametrize(
    "line",
    [
        "import xml.etree.ElementTree as ET",
        "def f(element: ET.Element) -> ET.Element:",
        "ET.canonicalize(xml_data=ET.tostring(root, encoding='unicode'))",
        "root = xml_safety.fromstring(data)",
    ],
)
def test_страж_не_трогает_допустимое(line):
    assert not DIRECT_XML.search(line)


def test_все_серверные_парсеры_идут_через_xml_safety():
    """Прямой вызов разборщика ElementTree в обход проверки не допускается."""
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        if path.name == "xml_safety.py":
            continue
        text = path.read_text(encoding="utf-8")
        for match in DIRECT_XML.finditer(text):
            number = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.name}:{number}")
    assert offenders == []


def _zip(path: Path, payloads: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads.items():
            archive.writestr(name, payload)
    return path


def _with_dtd(xml: bytes, references: int = 1000) -> bytes:
    """Вставить внутренний DTD и ``references`` ссылок в начало корня.

    1000 ссылок на 1 КиБ — ниже порога защиты expat: без проверки пролога
    такой XML разбирается успешно и раскрывается в мегабайт текста.
    """
    root_start = xml.index(b"<", xml.index(b"?>") + 2 if xml.startswith(b"<?") else 0)
    root_end = xml.index(b">", root_start) + 1
    name = xml[root_start + 1 : root_end].split()[0].rstrip(b">/")
    return (
        xml[:root_start]
        + b"<!DOCTYPE " + name + b' [<!ENTITY a "' + b"x" * 1024 + b'">]>'
        + xml[root_start:root_end]
        + b"&a;" * references
        + xml[root_end:]
    )


def test_без_проверки_expat_раскрыл_бы_такой_вход():
    from test_intake_v2_collector import _configuration

    payload = _with_dtd(_configuration())
    expanded = ET.fromstring(payload)
    assert len(expanded.text) == 1000 * 1024
    with pytest.raises(XmlDeclarationError):
        xml_safety.fromstring(payload)


def test_probe_intake_отвергает_configuration_xml_с_dtd():
    from mcp1c.intake_v2_probe import ProbeError, _properties
    from test_intake_v2_collector import _configuration

    assert _properties(_configuration())
    with pytest.raises(ProbeError, match="не разбирается"):
        _properties(_with_dtd(_configuration()))


def test_collector_помечает_роль_с_dtd_ошибкой_ролей(tmp_path):
    from test_intake_v2_collector import MemoryTree, _configuration, _collect, _role

    result = _collect(
        MemoryTree(
            {
                "Configuration.xml": _configuration(),
                "Roles/Bomb.xml": _with_dtd(_role("Bomb")),
            }
        ),
        tmp_path / "collection",
    )

    assert result.roles.state.value == "error"
    assert "DOCTYPE" in result.roles.error


def test_loader_отвергает_manifest_с_dtd():
    from mcp1c.loader import _read_manifest_xml

    assert _read_manifest_xml(io.BytesIO(b"<manifest/>")) == {}
    with pytest.raises(XmlDeclarationError):
        _read_manifest_xml(io.BytesIO(_with_dtd(b"<manifest></manifest>")))


def test_role_access_отвергает_descriptor_с_dtd():
    from mcp1c.role_access import RoleAccessError, _parse_descriptor
    from test_intake_v2_collector import _role

    with pytest.raises(RoleAccessError, match="неверный XML"):
        _parse_descriptor(io.BytesIO(_with_dtd(_role("Bomb"))), "Bomb")


def test_xdto_отвергает_package_с_dtd():
    from mcp1c.xdto import XDTOReadError, semantic_package_bytes

    package = b'<package xmlns="http://v8.1c.ru/8.1/xdto" targetNamespace="urn:x"/>'
    assert semantic_package_bytes(package)
    with pytest.raises(XDTOReadError):
        semantic_package_bytes(_with_dtd(package))


def test_registry_отвергает_configuration_xml_с_dtd(tmp_path):
    from mcp1c.registry import RegistryError, _сведения_о_выгрузке
    from test_intake_v2_collector import _configuration

    archive = _zip(
        tmp_path / "bomb.zip", {"Configuration.xml": _with_dtd(_configuration())}
    )
    with pytest.raises(RegistryError, match="DOCTYPE"):
        _сведения_о_выгрузке(archive)
