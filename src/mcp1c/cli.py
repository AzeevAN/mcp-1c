"""Отладочный CLI: посмотреть выгрузку до того, как она поедет в MCP-сервер.

    python3 -m mcp1c.cli info    <выгрузка>
    python3 -m mcp1c.cli stats   <выгрузка>
    python3 -m mcp1c.cli show    <выгрузка> Документ.РеализацияТоваровУслуг [--detail full]
    python3 -m mcp1c.cli related <выгрузка> Документ.РеализацияТоваровУслуг [--depth 2]
    python3 -m mcp1c.cli find    <выгрузка> реализация

Путь — ZIP или распакованный каталог, формат определяется по манифесту.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .graph import Graph
from .loader import ExportError, inspect, load
from .registry import Registry, RegistryError
from .render import DETAIL_LEVELS, FIELDS, render_configuration_summary, render_object


def _cmd_info(args: argparse.Namespace) -> int:
    info = inspect(args.path)
    print(f"Конфигурация : {info.name} {info.version}")
    print(f"Платформа    : {info.platform}")
    print(f"Формат       : {info.fmt}")
    print(f"Выгружено    : {info.exported_at}")
    print(f"Объектов     : {info.objects_total}")
    print(f"Полная       : {'нет — задан лимит' if info.truncated else 'да'}")
    print(f"Предопред.   : {'есть' if info.predefined_available else 'НЕТ'}")
    for warning in info.warnings:
        print(f"  ! {warning}")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    config = load(args.path)
    graph = Graph(config)
    print(render_configuration_summary(config, graph))

    stats = graph.stats()
    print("## Граф")
    print()
    print(f"- рёбер: {stats['edges']} (слабых: {stats['weak_edges']})")
    print(f"- изолированных объектов: {stats['isolated_objects']}")
    print(
        f"- неразрешённых ссылок: {stats['unresolved_total']} "
        f"({stats['unresolved_unique']} уникальных)"
    )
    for kind, count in stats["by_kind"].items():  # type: ignore[union-attr]
        print(f"  - {kind}: {count}")
    if graph.unresolved:
        print()
        print("Неразрешённые ссылки:")
        for target, count in graph.unresolved.most_common(10):
            print(f"  - {target} x{count}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    config = load(args.path)
    obj = config.get(args.object)
    if obj is None:
        print(f"Объект не найден: {args.object}", file=sys.stderr)
        _suggest(config, args.object)
        return 1
    graph = Graph(config) if args.detail != "brief" else None
    print(render_object(obj, args.detail, graph=graph))
    return 0


def _cmd_related(args: argparse.Namespace) -> int:
    config = load(args.path)
    if args.object not in config.objects:
        print(f"Объект не найден: {args.object}", file=sys.stderr)
        _suggest(config, args.object)
        return 1

    graph = Graph(config)
    print(f"# Связи: {args.object}\n")

    outgoing = graph.outgoing(args.object)
    if outgoing:
        print(f"## Ссылается на ({len(outgoing)})\n")
        for edge in outgoing:
            print(f"- `{edge.target}` — {edge.title}")
        print()

    incoming = graph.incoming(args.object)
    if incoming:
        print(f"## Ссылаются на него ({len(incoming)})\n")
        for edge in incoming[:40]:
            print(f"- `{edge.source}` — {edge.title}")
        if len(incoming) > 40:
            print(f"- … ещё {len(incoming) - 40}")
        print()

    if args.depth > 1:
        related = graph.related(args.object, depth=args.depth)
        print(f"## В радиусе {args.depth} ({len(related)})\n")
        for name, distance in sorted(related.items(), key=lambda kv: (kv[1], kv[0])):
            print(f"- [{distance}] `{name}`")
    return 0


def _cmd_find(args: argparse.Namespace) -> int:
    config = load(args.path)
    needle = args.query.lower()
    hits = [
        obj
        for obj in config.objects.values()
        if needle in obj.name.lower() or needle in obj.synonym.lower()
    ]
    if not hits:
        print("Ничего не найдено.")
        return 1
    for obj in sorted(hits, key=lambda o: o.full_name)[: args.limit]:
        print(f"{obj.full_name:70} {obj.synonym}")
    if len(hits) > args.limit:
        print(f"… ещё {len(hits) - args.limit}")
    return 0


# --------------------------------------------------------------- реестр


def _registry(args: argparse.Namespace) -> Registry:
    registry = Registry(args.data)
    for message in registry.startup():
        print(f"  {message}", file=sys.stderr)
    return registry


def _cmd_reg_add(args: argparse.Namespace) -> int:
    registry = Registry(args.data)
    registry.restore()
    path = Path(args.path)
    source = (
        registry.add_syntax(path)
        if path.suffix.lower() == ".hbk"
        else registry.add_configuration(path)
    )
    registry.save()
    print(f"добавлено: {source.id}  ({source.kind}, платформа {source.platform or '—'},"
          f" элементов {source.items_total})")
    for warning in source.warnings:
        print(f"  ! {warning}")
    return 0


def _cmd_reg_list(args: argparse.Namespace) -> int:
    registry = _registry(args)
    rows = registry.overview()
    if not rows:
        # Конфигураций нет — но справки могут быть загружены, и тогда сервер
        # работает: `search_syntax` и `get_syntax` отвечают. Тот же класс
        # дефекта уже чинили в `list_configurations`; здесь ветка «ничего не
        # загружено» срабатывала раньше проверки источников и возвращала ещё
        # и код 1, то есть скрипт вокруг считал бы это отказом.
        источники = []
        if registry.syntax is not None and registry.syntax.syntax.platforms:
            источники.append(
                f"справка платформы {registry.syntax.source.platform}, "
                f"{len(registry.syntax.syntax)} элементов"
            )
        if registry.query_source is not None:
            источники.append(
                f"язык запросов, {registry.query_source.items_total} страниц"
            )
        if not источники:
            print("Ничего не загружено.")
            return 1
        print("Конфигурации не загружены. Подключено:")
        for строка in источники:
            print(f"  {строка}")
        print("Работают search_syntax и get_syntax, без фильтра по версии.")
        return 0

    for row in rows:
        print(f"{row['name']}  {row['version']}  платформа {row['platform']}")
        print(f"  объектов {row['objects']}, связей {row['edges']}, загружено {row['loaded_at']}")
        syntax = row["providers"]["syntax"]
        print(f"  метаданные : да")
        # `providers['syntax']` истинно и когда подключён только язык
        # запросов: `LoadedSyntax` собирает оба источника в один объект. У
        # языка запросов платформы нет, `syntax_platform` тогда пуст, и строка
        # выходила противоречивой — «справка , не подключён». Тот же баг-паттерн
        # чинили в `list_configurations`, в CLI он оставался.
        if syntax and row["syntax_platform"]:
            отношение = _RELATION_TITLES.get(
                row["syntax_relation"], row["syntax_relation"]
            )
            скрыто = f", скрыто {row['syntax_hidden']}" if row["syntax_hidden"] else ""
            состояние = f"справка {row['syntax_platform']}, {отношение}{скрыто}"
        else:
            состояние = "не подключён"
        print(f"  синтаксис  : {состояние}")
        print(f"  модули     : {'да' if row['providers']['modules'] else 'не подключены'}")
        # Язык запросов — самостоятельный источник, не версия справки
        # платформы (`registry.query_source`, а не `row['providers']`), и
        # без отдельной строки в общем списке был не виден вовсе.
        print(
            "  язык запросов: "
            + (
                f"подключён, {registry.query_source.items_total} страниц"
                if registry.query_source is not None
                else "не подключён"
            )
        )
        for note in row["notes"]:
            print(f"  ! {note}")
        print()
    return 0


_RELATION_TITLES = {
    "exact": "версия совпадает",
    "newer": "новее конфигурации",
    "older": "СТАРЕЕ конфигурации",
    "none": "не подключён",
}


def _cmd_reg_search(args: argparse.Namespace) -> int:
    registry = _registry(args)
    try:
        context = registry.resolve(args.config)
    except RegistryError as error:
        print(error, file=sys.stderr)
        return 1

    if args.syntax:
        if context.syntax is None:
            print("Справка не подключена.", file=sys.stderr)
            return 1
        keep = context.syntax_filter()
        raw = context.syntax.index.search(args.query, limit=args.limit * 3)
        hits = [h for h in raw if keep(h.doc.payload)][: args.limit]
        скрытые = [h for h in raw if not keep(h.doc.payload)]
        for hit in hits:
            item = hit.doc.payload
            print(f"{item.full_ru}")
            print(f"    {item.full_en}   [{item.kind}]  с версии {item.since or '—'}")
            if item.availability:
                print(f"    доступность: {', '.join(item.availability)}")

        # Молча выбросить скрытое нельзя: этим CLI разбирают, почему поиск
        # ведёт себя так, и пустой вывод читается как «в справке этого нет».
        if скрытые:
            print(f"скрыто по версии {context.platform}: {len(скрытые)}")
            for hit in скрытые[:3]:
                item = hit.doc.payload
                причина = (
                    f"с {item.since}" if item.since else f"по {item.until} включительно"
                )
                print(f"    {item.full_ru} — {причина}")
    else:
        for hit in context.configuration.index.search(args.query, limit=args.limit):
            obj = hit.doc.payload
            print(f"{obj.full_name:64} {obj.synonym}")

    for note in context.notes():
        print(f"! {note}", file=sys.stderr)
    return 0


# --------------------------------------------------------------- словарь


def _cmd_dict_show(args: argparse.Namespace) -> int:
    registry = Registry(args.data)
    d = registry.dictionary

    for key, value in d.stats().items():
        print(f"{key:32} {value}")

    if d.synonym_groups:
        print("\nсвои группы синонимов:")
        for group in d.synonym_groups:
            print(f"  {' = '.join(group)}")

    entries = d.aliases_with_source(args.config)
    local = {p: v for p, v in entries.items() if v[1] != "встроенный"}
    builtin = {p: v for p, v in entries.items() if v[1] == "встроенный"}

    if local:
        print(f"\nлокальные псевдонимы ({len(local)}):")
        for phrase, (targets, source) in sorted(local.items()):
            print(f"  «{phrase}» -> {', '.join(targets)}")
            print(f"       {source}")

    if builtin and args.all:
        print(f"\nвстроенные псевдонимы ({len(builtin)}):")
        for phrase, (targets, _) in sorted(builtin.items()):
            print(f"  «{phrase}» -> {', '.join(targets)}")
    elif builtin:
        print(f"\nвстроенных псевдонимов: {len(builtin)} (показать: --all)")

    print(f"\nфайл локального словаря: {registry.dictionary_path}")
    print("Встроенное правится в коде и приезжает релизом; локальное — здесь.")
    return 0


def _cmd_dict_synonyms(args: argparse.Namespace) -> int:
    registry = Registry(args.data)

    if args.remove:
        # Группа опознаётся по составу целиком: номер сдвинется от правки
        # соседей, а состав — то, что видно в `dict-show`.
        if not registry.dictionary.remove_synonyms(args.words):
            print(f"такой группы нет: {' = '.join(args.words)}")
            print("Встроенные группы отсюда не снимаются — они в synonyms.py.")
            return 1
        registry.dictionary.save(registry.dictionary_path)
        print(f"снята группа: {' = '.join(args.words)}")
    else:
        group = registry.dictionary.add_synonyms(args.words)
        registry.dictionary.save(registry.dictionary_path)
        print(f"добавлена группа: {' = '.join(group)}")

    print("Изменения подхватятся при перезапуске или POST /admin/reload.")
    return 0


def _cmd_dict_alias(args: argparse.Namespace) -> int:
    registry = Registry(args.data)
    registry.restore()

    if args.remove:
        removed = registry.dictionary.remove_alias(args.phrase, args.config)
        registry.dictionary.save(registry.dictionary_path)
        print("удалено" if removed else "такого псевдонима не было")
        return 0 if removed else 1

    # Проверяем, что объекты существуют: псевдоним на опечатку бесполезен.
    if args.config and args.config in registry.configurations:
        objects = registry.configurations[args.config].config.objects
        unknown = [t for t in args.targets if t not in objects]
        if unknown:
            print(f"В конфигурации {args.config} нет: {', '.join(unknown)}", file=sys.stderr)
            return 1

    phrase, targets = registry.dictionary.add_alias(args.phrase, args.targets, args.config)
    registry.dictionary.save(registry.dictionary_path)
    scope = args.config or "все конфигурации"
    print(f"«{phrase}» -> {', '.join(targets)}   ({scope})")
    print("Изменения подхватятся при перезапуске или POST /admin/reload.")
    return 0


def _suggest(config, wanted: str) -> None:
    needle = wanted.split(".")[-1].lower()[:6]
    if not needle:
        return
    close = [n for n in config.objects if needle in n.lower()][:5]
    if close:
        print("Похожие:", file=sys.stderr)
        for name in close:
            print(f"  {name}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp1c", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler, needs_object in (
        ("info", _cmd_info, False),
        ("stats", _cmd_stats, False),
        ("show", _cmd_show, True),
        ("related", _cmd_related, True),
        ("find", _cmd_find, False),
    ):
        sp = sub.add_parser(name)
        sp.add_argument("path", help="ZIP или каталог выгрузки")
        if needs_object:
            sp.add_argument("object", help="полное имя объекта")
        if name == "show":
            sp.add_argument("--detail", choices=DETAIL_LEVELS, default=FIELDS)
        if name == "related":
            sp.add_argument("--depth", type=int, default=1)
        if name == "find":
            sp.add_argument("query", help="часть имени или синонима")
            sp.add_argument("--limit", type=int, default=30)
        sp.set_defaults(handler=handler)

    for name, handler in (
        ("dict-show", _cmd_dict_show),
        ("dict-synonyms", _cmd_dict_synonyms),
        ("dict-alias", _cmd_dict_alias),
        ("reg-add", _cmd_reg_add),
        ("reg-list", _cmd_reg_list),
        ("reg-search", _cmd_reg_search),
    ):
        sp = sub.add_parser(name)
        sp.add_argument("--data", default="data", help="каталог данных сервера")
        if name == "reg-add":
            sp.add_argument("path", help="ZIP выгрузки или .hbk справки")
        if name == "dict-show":
            sp.add_argument("--config", default=None,
                            help="показать псевдонимы для конкретной конфигурации")
            sp.add_argument("--all", action="store_true",
                            help="включая встроенные")
        if name == "dict-synonyms":
            sp.add_argument("words", nargs="+", help="слова одной группы")
            sp.add_argument("--remove", action="store_true",
                            help="снять группу с таким составом")
        if name == "dict-alias":
            sp.add_argument("phrase", help="как говорят: «справочник физлиц»")
            sp.add_argument("targets", nargs="*", help="полные имена объектов")
            sp.add_argument("--config", default=None,
                            help="конфигурация; без неё — для всех")
            sp.add_argument("--remove", action="store_true")
        if name == "reg-search":
            sp.add_argument("query")
            sp.add_argument("--config", default=None)
            sp.add_argument("--syntax", action="store_true", help="искать в справке")
            sp.add_argument("--limit", type=int, default=10)
        sp.set_defaults(handler=handler)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except RegistryError as error:
        print(f"Реестр: {error}", file=sys.stderr)
        return 2
    except ExportError as error:
        print(f"Ошибка выгрузки: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
