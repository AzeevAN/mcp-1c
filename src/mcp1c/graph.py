"""Граф связей между объектами конфигурации.

Рёбра выводятся из уже выгруженных данных — обработка их не считает.
Источники рёбер:

    тип реквизита / измерения / ресурса / реквизита ТЧ  -> ссылочный объект
    движения документа                                   -> регистр
    ввод на основании                                    -> документ-основание
    владелец справочника                                 -> справочник-владелец
    обработчик подписки на событие                       -> общий модуль
    метод регламентного задания                          -> общий модуль

Это то, ради чего сервер вообще нужен: агент за один вызов получает список
объектов, которые задевает задача, вместо десятка слепых поисков.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass

from .model import Configuration, MetadataObject, normalize_common_module_binding

# Ссылки на саму конфигурацию (план обмена ссылается на Конфигурация.<Имя>)
# объектами метаданных не являются и рёбрами не считаются.
_NON_OBJECT_PREFIXES = ("Конфигурация.",)

EDGE_TITLES = {
    "attribute": "реквизит",
    "dimension": "измерение",
    "resource": "ресурс",
    "tabular": "реквизит ТЧ",
    "value_type": "тип значения",
    "movement": "движение",
    "based_on": "ввод на основании",
    "owner": "владелец",
    "handler": "обработчик",
    "method": "метод",
    "source": "источник подписки",
    "chart_of_accounts": "план счетов",
    "ext_dimension_types": "виды субконто",
    "characteristic_ext_values": "значения характеристик",
    "addressing": "регистр адресации",
    "task": "задача",
    "chart_of_calculation_types": "план видов расчёта",
    "schedule": "график",
    "applies_to": "применяется к",
    "conditional_separation": "условное разделение",
    "data_separation_use": "параметр разделения",
    "registers_document": "регистрирует документ",
    "column_field": "источник графы",
    "exchange_content": "состав плана обмена",
    "event_source": "источник события",
}

# Свойства, значение которых — полное имя другого объекта. Это прямые связи:
# регистр бухгалтерии ведётся по плану счетов, план счетов — по видам
# субконто, бизнес-процесс порождает задачу. Держать их только текстом в
# свойствах значит не отвечать на «что задевает этот объект».
_REFERENCE_PROPS = (
    "chart_of_accounts",
    "ext_dimension_types",
    "characteristic_ext_values",
    "addressing",
    "task",
    "chart_of_calculation_types",
    "schedule",
)


# Реквизиты вроде ЗначениеДоступа, Значение или Регистратор перечисляют сотни
# типов. Формально это рёбра, но смысла в них мало: они связывают почти всё со
# всем и топят полезные связи. Помечаем слабыми и по умолчанию не показываем.
WEAK_EDGE_TYPE_COUNT = 12


@dataclass(slots=True, frozen=True)
class Edge:
    source: str
    target: str
    kind: str
    via: str = ""
    weak: bool = False

    @property
    def title(self) -> str:
        label = EDGE_TITLES.get(self.kind, self.kind)
        return f"{label} {self.via}" if self.via else label


class Graph:
    """Двусторонний индекс рёбер по объектам."""

    def __init__(self, config: Configuration):
        self.config = config
        self.edges: list[Edge] = []
        self.out: dict[str, list[Edge]] = defaultdict(list)
        self.inc: dict[str, list[Edge]] = defaultdict(list)
        self.unresolved: Counter[str] = Counter()
        self._object_names = {
            name.casefold(): name for name in config.objects
        }
        self._field_owners = {
            f"{obj.full_name}.{path}".casefold(): obj.full_name
            for obj in config.objects.values()
            for path, _field in obj.all_fields()
        }
        self._build()

    # ------------------------------------------------------------- построение

    def _add(
        self, source: str, target: str, kind: str, via: str = "", weak: bool = False
    ) -> None:
        if not target or target.startswith(_NON_OBJECT_PREFIXES):
            return
        if target not in self.config.objects:
            self.unresolved[target] += 1
            return
        if target == source:
            return
        edge = Edge(source=source, target=target, kind=kind, via=via, weak=weak)
        self.edges.append(edge)
        self.out[source].append(edge)
        self.inc[target].append(edge)

    def _build(self) -> None:
        for obj in self.config.objects.values():
            source = obj.full_name

            for kind, fields in (
                ("attribute", obj.attributes),
                ("dimension", obj.dimensions),
                ("resource", obj.resources),
            ):
                for item in fields:
                    weak = len(item.types) >= WEAK_EDGE_TYPE_COUNT
                    for target in item.object_types():
                        self._add(source, target, kind, item.name, weak)

            for part in obj.tabular_parts:
                for item in part.attributes:
                    weak = len(item.types) >= WEAK_EDGE_TYPE_COUNT
                    via = f"{part.name}.{item.name}"
                    for target in item.object_types():
                        self._add(source, target, "tabular", via, weak)

            if obj.value_type is not None:
                weak = len(obj.value_type.types) >= WEAK_EDGE_TYPE_COUNT
                for target in obj.value_type.object_types():
                    self._add(source, target, "value_type", "", weak)

            for target in obj.movements:
                self._add(source, target, "movement")
            for target in obj.based_on:
                self._add(source, target, "based_on")
            for target in obj.owners:
                self._add(source, target, "owner")

            self._add_module_edge(obj, "handler")
            self._add_module_edge(obj, "method")

            # Источники подписки на событие. Отвечает на вопрос «что сработает,
            # если записать этот справочник»: без ребра подписка видна только
            # со своей стороны, а спрашивают всегда со стороны объекта.
            #
            # Подписка «на запись любого объекта» перечисляет сотни типов — в
            # Документообороте максимум 827 при медиане 4. Такие связывают всё
            # со всем ровно как реквизиты вида `ЗначениеДоступа`, поэтому и
            # правило то же: широкая подписка — слабое ребро.
            sources = obj.props.get("source") or []
            weak_subscription = len(sources) >= WEAK_EDGE_TYPE_COUNT
            for target in sources:
                if isinstance(target, str) and target:
                    self._add(source, target, "source", weak=weak_subscription)

            for prop in _REFERENCE_PROPS:
                target = obj.props.get(prop)
                if isinstance(target, str) and target:
                    self._add(source, target, prop)

            for relation in obj.relations:
                # Часть typed relations — более подробное доказательство уже
                # существующей base-связи. В resolved graph оно не должно
                # превращаться во второе ребро к той же цели.
                if (
                    relation.kind == "event_source"
                    and relation.target in sources
                ) or (
                    relation.kind == "based_on"
                    and relation.target in obj.based_on
                ):
                    continue
                if relation.state != "resolved":
                    self.unresolved[relation.target] += 1
                    continue
                target = relation.target
                if target not in self.config.objects:
                    target = self._object_names.get(
                        target.casefold(),
                        self._field_owners.get(target.casefold(), target),
                    )
                via = ", ".join(
                    f"{key}={value}"
                    for key, value in relation.properties
                    if value
                )
                self._add(source, target, relation.kind, via)

    def _add_module_edge(self, obj: MetadataObject, prop: str) -> None:
        """Обработчик подписки и метод задания записаны как ИмяМодуля.Процедура."""
        value = obj.props.get(prop)
        if not isinstance(value, str) or "." not in value:
            return
        value = normalize_common_module_binding(value)
        module_name, _, procedure = value.partition(".")
        self._add(obj.full_name, f"ОбщийМодуль.{module_name}", prop, procedure)

    # ------------------------------------------------------------- обход

    def outgoing(self, full_name: str, *, include_weak: bool = True) -> list[Edge]:
        edges = self.out.get(full_name, [])
        return edges if include_weak else [e for e in edges if not e.weak]

    def incoming(self, full_name: str, *, include_weak: bool = False) -> list[Edge]:
        """Входящие связи. Слабые скрыты по умолчанию — их сотни и они бесполезны."""
        edges = self.inc.get(full_name, [])
        return edges if include_weak else [e for e in edges if not e.weak]

    def neighbours(self, full_name: str, *, include_weak: bool = False) -> set[str]:
        names = {e.target for e in self.outgoing(full_name, include_weak=include_weak)}
        names |= {e.source for e in self.incoming(full_name, include_weak=include_weak)}
        return names

    def related(
        self,
        full_name: str,
        depth: int = 1,
        *,
        direction: str = "both",
        limit: int = 200,
    ) -> dict[str, int]:
        """Объекты в радиусе depth рёбер. Значение — расстояние."""
        if full_name not in self.config.objects:
            return {}

        seen: dict[str, int] = {full_name: 0}
        queue: deque[tuple[str, int]] = deque([(full_name, 0)])

        while queue and len(seen) < limit:
            current, distance = queue.popleft()
            if distance >= depth:
                continue

            names: set[str] = set()
            if direction in ("out", "both"):
                names |= {e.target for e in self.outgoing(current, include_weak=False)}
            if direction in ("in", "both"):
                names |= {e.source for e in self.incoming(current)}

            for name in sorted(names):
                if name in seen:
                    continue
                seen[name] = distance + 1
                queue.append((name, distance + 1))
                if len(seen) >= limit:
                    break

        seen.pop(full_name, None)
        return seen

    # ------------------------------------------------------------- статистика

    def stats(self) -> dict[str, object]:
        by_kind = Counter(e.kind for e in self.edges)
        weak = sum(1 for e in self.edges if e.weak)
        isolated = sum(
            1
            for name in self.config.objects
            if not self.out.get(name) and not self.inc.get(name)
        )
        return {
            "edges": len(self.edges),
            "weak_edges": weak,
            "by_kind": dict(by_kind.most_common()),
            "unresolved_unique": len(self.unresolved),
            "unresolved_total": sum(self.unresolved.values()),
            "isolated_objects": isolated,
        }

    def hubs(self, limit: int = 10) -> list[tuple[str, int]]:
        """Объекты, на которые ссылаются чаще всего."""
        counts = Counter(
            {
                name: sum(1 for e in edges if not e.weak)
                for name, edges in self.inc.items()
            }
        )
        return counts.most_common(limit)
