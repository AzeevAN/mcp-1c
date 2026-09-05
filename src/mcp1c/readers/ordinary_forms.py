"""Семантический ридер обычных форм профилей 25, 26 и 27."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .list_stream import ListStreamError, ListStreamScan, materialize_list_stream


_IDENTIFIER = re.compile(r"^[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*$")
_CONTROL_TYPES = {
    "09ccdc77-ea1a-4a6d-ab1c-3435eada2433": "Panel",
    "621e95f1-064f-11d4-9400-008048da11f9": "ActiveXControl",
    "0fc7e20d-f241-460c-bdf4-5ad88e5474a5": "LabelDecoration",
    "151ef23e-6bb2-4681-83d0-35bc2217230c": "PictureDecoration",
    "6ff79819-710e-4145-97cd-1618da79e3e2": "Button",
    "381ed624-9217-4e63-85db-c4c3cb87daae": "InputField",
    "e69bf21d-97b2-4f37-86db-675aea9ec2cb": "CommandBar",
    "35af3d93-d7c7-4a2e-a8eb-bac87a1a3f26": "CheckBox",
    "ea83fe3a-ac3c-4cce-8045-3dddf35b28b1": "Table",
    "64483e7f-3833-48e2-8c75-2c31aac49f6e": "ChoiceField",
    "236a17b3-7f44-46d9-a907-75f9cdc61ab5": "SpreadsheetDocumentField",
    "90db814a-c75f-4b54-bc96-df62e554d67d": "GroupBox",
    "782e569a-79a7-4a4f-a936-b48d013936ec": "RadioButton",
    "36e52348-5d60-4770-8e89-a16ed50a2006": "Splitter",
    "a8b97779-1a4b-4059-b09c-807f86d2a461": "Chart",
    "19f8b798-314e-4b4e-8121-905b2a7a03f5": "ListBox",
    "d92a805c-98ae-4750-9158-d9ce7cec2f20": "HTMLDocumentField",
    "6c06cd5d-8481-4b6f-a90a-7a97a8bb8bef": "TrackBar",
    "e3c063d8-ef92-41be-9c89-b70290b5368b": "CalendarField",
    "14c4a229-bfc3-42fe-9ce1-2da049fd0109": "TextDocumentField",
    "a26da99e-184a-4823-b0d6-62816d38dc4e": "PivotChart",
    "ad37194e-555e-4305-b718-5dca84baf145": "GeographicalSchemaField",
    "b1db1f86-abbb-4cf0-8852-fe6ae21650c2": "ProgressBar",
    "42248403-7748-49da-b782-e4438fd7bff3": "GraphicalSchemaField",
    "e5fdc112-5c84-4a16-9728-72b85692b6e2": "GanttChart",
    "984981b1-622d-4ebc-94f7-885f0cdfb59a": "Dendrogram",
}


@dataclass(frozen=True, slots=True)
class OrdinaryFormSemantic:
    attributes: tuple[str, ...]
    elements: tuple[str, ...]
    control_types: tuple[str, ...]


class OrdinaryFormReader:
    markers = frozenset({25, 26, 27})

    def applicable(self, marker: int) -> bool:
        return marker in self.markers

    def read(self, scan: ListStreamScan) -> OrdinaryFormSemantic:
        root = materialize_list_stream(scan)
        expected_length = {25: 18, 26: 19, 27: 20}[scan.marker]
        if len(root) != expected_length or root[0] != str(scan.marker):
            raise ListStreamError(
                "unsupported_profile",
                "корневая запись form не совпадает с известным профилем",
            )
        attributes = self._attributes(root[2])
        elements, control_types = self._elements(root[1])
        return OrdinaryFormSemantic(attributes, elements, control_types)

    @staticmethod
    def _attributes(value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or len(value) != 4:
            raise ListStreamError(
                "unsupported_profile",
                "таблица реквизитов form имеет неизвестную форму",
            )
        table = value[2]
        if (
            not isinstance(table, list)
            or not table
            or not str(table[0]).isdigit()
        ):
            raise ListStreamError(
                "unsupported_profile",
                "таблица реквизитов form не содержит счётчик",
            )
        declared = int(str(table[0]))
        rows = table[1:]
        if declared != len(rows):
            raise ListStreamError(
                "unsupported_profile",
                "счётчик реквизитов form не совпадает с записями",
            )
        names: list[str] = []
        for row in rows:
            if not isinstance(row, list):
                raise ListStreamError(
                    "unsupported_profile",
                    "запись реквизита form имеет неизвестную форму",
                )
            candidates: list[str] = []
            for index, item in enumerate(row):
                if isinstance(item, list) and item and item[0] == "Pattern" and index:
                    candidate = row[index - 1]
                    if isinstance(candidate, str) and _IDENTIFIER.fullmatch(candidate):
                        candidates.append(candidate)
            if len(candidates) != 1:
                raise ListStreamError(
                    "unsupported_profile", "имя реквизита form не доказано"
                )
            names.append(candidates[0])
        if len(set(names)) != len(names):
            raise ListStreamError(
                "unsupported_profile", "имена реквизитов form дублируются"
            )
        return tuple(names)

    @classmethod
    def _elements(cls, value: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
        elements: list[str] = []
        types: list[str] = []

        def walk(node: object) -> None:
            if not isinstance(node, list):
                return
            control_type = _CONTROL_TYPES.get(str(node[0]).lower()) if node else None
            if control_type:
                records = [
                    item
                    for item in cls._walk_lists(node)
                    if len(item) >= 2 and item[0] == "14"
                ]
                names = [
                    item[1]
                    for item in records
                    if isinstance(item[1], str) and _IDENTIFIER.fullmatch(item[1])
                ]
                if not names:
                    raise ListStreamError(
                        "unsupported_profile", "имя элемента form не доказано"
                    )
                elements.append(names[0])
                types.append(control_type)
            for child in node:
                if isinstance(child, list):
                    walk(child)

        walk(value)
        # Техническая корневая панель всегда первая в профилях 25-27.
        if types and types[0] == "Panel":
            elements.pop(0)
            types.pop(0)
        return tuple(elements), tuple(types)

    @staticmethod
    def _walk_lists(value: list[object]):
        yield value
        for item in value:
            if isinstance(item, list):
                yield from OrdinaryFormReader._walk_lists(item)


__all__ = ["OrdinaryFormReader", "OrdinaryFormSemantic"]
