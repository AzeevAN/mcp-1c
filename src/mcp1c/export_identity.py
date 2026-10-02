"""Правило «конфигурация или расширение» по `Properties` из Configuration.xml.

Одно правило на всех потребителей: раньше оно жило копиями в intake probe
и в реестре, и копия, разошедшаяся с другой, тихо классифицировала бы одну
и ту же выгрузку по-разному. Здесь только решение; тексты отказов и вид
исключения остаются у вызывающего — у каждого свой контракт ошибок.

Порядок проверки — часть контракта:

1. Сначала сильные признаки расширения (`ObjectBelonging`,
   `ConfigurationExtensionPurpose`). Виден хоть один — дальше выбор только
   между «расширение» (оба сильных признака, непустой `NamePrefix`, пустой
   `CompatibilityMode`) и отказом. В конфигурацию отсюда хода нет ни при
   каком `CompatibilityMode`: у настоящей конфигурации сильных тегов не
   бывает вовсе.
2. Сильных признаков нет — конфигурация, если и только если
   `CompatibilityMode` непустой. `NamePrefix` для этого ненадёжен: у плоской
   выгрузки 8.3.5 тега нет.
3. Иначе — отказ.

Цена ошибки несимметрична: расширение, принятое за конфигурацию, заменило
бы собой её данные. Поэтому решает только явный положительный признак.
"""

from __future__ import annotations

from enum import Enum
from typing import Mapping

# Теги `Configuration/Properties`, по которым принимается решение и которые
# probe возвращает вместе с ним.
PROPERTY_TAGS = (
    "Name",
    "Version",
    "NamePrefix",
    "ObjectBelonging",
    "ConfigurationExtensionPurpose",
    "CompatibilityMode",
)


class ExportVerdict(str, Enum):
    CONFIGURATION = "configuration"
    EXTENSION = "extension"
    # Есть сильный признак расширения, но набор неполон или противоречив.
    INCOMPLETE_EXTENSION = "incomplete_extension"
    # Нет ни признака расширения, ни признака конфигурации.
    NO_SIGNAL = "no_signal"


def classify(properties: Mapping[str, str]) -> ExportVerdict:
    """Решение по уже извлечённым и обрезанным значениям тегов.

    Отсутствующий тег — пустая строка.
    """

    def value(tag: str) -> str:
        return properties.get(tag, "")

    belonging = value("ObjectBelonging")
    purpose = value("ConfigurationExtensionPurpose")
    compatibility = value("CompatibilityMode")
    if belonging or purpose:
        if belonging and purpose and value("NamePrefix") and not compatibility:
            return ExportVerdict.EXTENSION
        return ExportVerdict.INCOMPLETE_EXTENSION
    if compatibility:
        return ExportVerdict.CONFIGURATION
    return ExportVerdict.NO_SIGNAL
