"""Синтетические архивы выгрузки модулей без данных реальных конфигураций."""

from __future__ import annotations

import struct
import warnings
import zipfile
import zlib
from pathlib import Path

from mcp1c.v8container import BLOCK_HEADER_SIZE, EMPTY_ADDR, HEADER_SIZE


def _block(payload: bytes) -> bytes:
    header = (
        b"\r\n"
        + f"{len(payload):08x} {len(payload):08x} {EMPTY_ADDR:08x}".encode("ascii")
        + b" \r\n"
    )
    assert len(header) == BLOCK_HEADER_SIZE
    return header + payload


def _deflate(payload: bytes) -> bytes:
    compressor = zlib.compressobj(level=6, wbits=-15)
    return compressor.compress(payload) + compressor.flush()


def v8_container_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    """Собрать минимальный контейнер, читаемый боевым ``V8Container``.

    Формат нужен только как тестовый транспорт: записи и блоки настоящие,
    служебные времена нулевые, цепочки продолжения не создаются.
    """

    table_size = 12 * len(entries)
    cursor = HEADER_SIZE + BLOCK_HEADER_SIZE + table_size
    addresses: list[tuple[int, int]] = []
    blocks: list[bytes] = []

    for name, payload in entries:
        attrs = b"\x00" * 20 + name.encode("utf-16-le") + b"\x00\x00"
        attrs_block = _block(attrs)
        attrs_address = cursor
        cursor += len(attrs_block)

        data_block = _block(_deflate(payload))
        data_address = cursor
        cursor += len(data_block)

        addresses.append((attrs_address, data_address))
        blocks.extend((attrs_block, data_block))

    table = b"".join(struct.pack("<III", attrs, data, 0) for attrs, data in addresses)
    header = struct.pack("<IIII", EMPTY_ADDR, 512, 0, 0)
    return header + _block(table) + b"".join(blocks)


_FOLDERS = {
    "Catalog": "Catalogs",
    "Document": "Documents",
    "DataProcessor": "DataProcessors",
    "Report": "Reports",
    "CommonForm": "CommonForms",
}


class ModulesArchiveBuilder:
    """Накопитель членов ZIP для иерархической или плоской раскладки."""

    def __init__(self, layout: str, *, wrapper: str = "", extension: bool = False):
        self.layout = layout
        self.wrapper = wrapper.strip("/")
        self.extension = extension
        self._members: list[tuple[str, bytes]] = []
        self.raw("Configuration.xml", self._configuration_xml())

    @classmethod
    def tree(cls, *, wrapper: str = "", extension: bool = False) -> "ModulesArchiveBuilder":
        return cls("tree", wrapper=wrapper, extension=extension)

    @classmethod
    def flat(cls, *, wrapper: str = "", extension: bool = False) -> "ModulesArchiveBuilder":
        return cls("flat", wrapper=wrapper, extension=extension)

    def _configuration_xml(self) -> bytes:
        prefix = "Тест" if self.extension else ""
        compatibility = "" if self.extension else "Version8_3_5"
        extension_properties = (
            '<ObjectBelonging>Adopted</ObjectBelonging>'
            '<ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose>'
            if self.extension
            else ""
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
            '<Configuration><Properties>'
            '<Name>СинтетическаяКонфигурация</Name>'
            f'<NamePrefix>{prefix}</NamePrefix>'
            f'{extension_properties}'
            f'<CompatibilityMode>{compatibility}</CompatibilityMode>'
            '</Properties></Configuration></MetaDataObject>'
        ).encode("utf-8")

    def _wrapped(self, name: str) -> str:
        return f"{self.wrapper}/{name}" if self.wrapper else name

    def raw(self, name: str, payload: bytes | str) -> "ModulesArchiveBuilder":
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._members.append((name, payload))
        return self

    def text(self, name: str, source: str) -> "ModulesArchiveBuilder":
        return self.raw(name, source.encode("utf-8"))

    def compiled(self, name: str) -> "ModulesArchiveBuilder":
        return self.raw(name, v8_container_bytes([("image", b"compiled")]))

    def duplicate(self, name: str, first: bytes, second: bytes) -> "ModulesArchiveBuilder":
        self.raw(name, first)
        self.raw(name, second)
        return self

    def form_descriptor(self, kind: str, object_name: str, form_name: str) -> "ModulesArchiveBuilder":
        folder = _FOLDERS.get(kind, kind if kind.endswith("s") else f"{kind}s")
        descriptor = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">'
            '<Form><Properties>'
            f'<Name>{form_name}</Name><Synonym><item><content>{form_name}</content></item></Synonym>'
            '<FormType>Managed</FormType>'
            '</Properties></Form></MetaDataObject>'
        )
        return self.raw(f"{folder}/{object_name}/Forms/{form_name}.xml", descriptor)

    def xml_form(
        self,
        folder: str,
        object_name: str,
        form_name: str,
        *,
        with_module: bool,
    ) -> "ModulesArchiveBuilder":
        base = f"{folder}/{object_name}/Forms/{form_name}/Ext"
        self.raw(
            f"{base}/Form.xml",
            '<Form><Attributes><Attribute name="Объект"/></Attributes></Form>',
        )
        if with_module:
            self.text(
                f"{base}/Form/Module.bsl",
                "Процедура ПриОткрытии()\nКонецПроцедуры\n",
            )
        return self

    def binary_form(
        self,
        folder: str,
        object_name: str,
        form_name: str,
        *,
        module: str | bytes,
        form: str,
    ) -> "ModulesArchiveBuilder":
        singular = next((kind for kind, value in _FOLDERS.items() if value == folder), folder)
        self.form_descriptor(singular, object_name, form_name)
        module_bytes = module.encode("utf-8") if isinstance(module, str) else module
        payload = v8_container_bytes([("module", module_bytes), ("form", form.encode("utf-8"))])
        return self.raw(f"{folder}/{object_name}/Forms/{form_name}/Ext/Form.bin", payload)

    def broken_binary_form(self, folder: str, object_name: str, form_name: str) -> "ModulesArchiveBuilder":
        singular = next((kind for kind, value in _FOLDERS.items() if value == folder), folder)
        self.form_descriptor(singular, object_name, form_name)
        return self.raw(f"{folder}/{object_name}/Forms/{form_name}/Ext/Form.bin", b"broken")

    def container_form(
        self, name: str, *, module: str | bytes, form: str
    ) -> "ModulesArchiveBuilder":
        module_bytes = module.encode("utf-8") if isinstance(module, str) else module
        return self.raw(
            name,
            v8_container_bytes(
                [("module", module_bytes), ("form", form.encode("utf-8"))]
            ),
        )

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                for name, payload in self._members:
                    archive.writestr(self._wrapped(name), payload)
        return path
