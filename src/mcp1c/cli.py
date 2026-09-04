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

from . import tools
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
    config = load(args.path, allow_truncated=args.allow_truncated)
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
    config = load(args.path, allow_truncated=args.allow_truncated)
    obj = config.get(args.object)
    if obj is None:
        print(f"Объект не найден: {args.object}", file=sys.stderr)
        _suggest(config, args.object)
        return 1
    graph = Graph(config) if args.detail != "brief" else None
    print(render_object(obj, args.detail, graph=graph))
    return 0


def _cmd_related(args: argparse.Namespace) -> int:
    config = load(args.path, allow_truncated=args.allow_truncated)
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
    config = load(args.path, allow_truncated=args.allow_truncated)
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


def _registry_for_code(args: argparse.Namespace) -> Registry:
    """Холодный одноразовый CLI ждёт кэш, сервер продолжает отвечать фоном."""
    registry = _registry(args)
    if not registry.wait_for_module_builds(timeout=90.0):
        raise RegistryError(
            "Индекс кода не успел построиться за 90 секунд. "
            "Повторите команду: готовый кэш будет поднят при следующем запуске."
        )
    return registry


def _cmd_reg_add(args: argparse.Namespace) -> int:
    registry = Registry(args.data)
    registry.restore()
    path = Path(args.path)
    suffix = path.suffix.lower()
    if suffix == ".hbk":
        source = registry.add_syntax(path)
    elif suffix == ".json":
        source = registry.add_extension_runtime(path)
    else:
        source = registry.add_configuration(
            path, allow_truncated=args.allow_truncated
        )
    registry.save()
    print(f"добавлено: {source.id}  ({source.kind}, платформа {source.platform or '—'},"
          f" элементов {source.items_total})")
    for warning in source.warnings:
        print(f"  ! {warning}")
    return 0


def _cmd_reg_list(args: argparse.Namespace) -> int:
    registry = _registry(args)
    snapshot = tools.configurations_snapshot(registry)
    if not snapshot.rows:
        # Конфигураций нет — но справки могут быть загружены, и тогда сервер
        # работает: `search_syntax` и `get_syntax` отвечают. Тот же класс
        # дефекта уже чинили в `list_configurations`; здесь ветка «ничего не
        # загружено» срабатывала раньше проверки источников и возвращала ещё
        # и код 1, то есть скрипт вокруг считал бы это отказом.
        источники = []
        if snapshot.syntax_platforms:
            источники.append(
                f"справка платформы {snapshot.syntax_source_platform}, "
                f"{snapshot.syntax_items} элементов"
            )
        if not источники:
            print("Ничего не загружено.")
            return 1
        print("Конфигурации не загружены. Подключено:")
        for строка in источники:
            print(f"  {строка}")
        print("Работают search_syntax и get_syntax, без фильтра по версии.")
        return 0

    for row in snapshot.rows:
        print(f"{row.name}  {row.version}  платформа {row.platform or 'неизвестна'}")
        if row.compatibility_mode:
            print(f"  Режим совместимости: {row.compatibility_mode}")
        print(
            f"  объектов {row.objects}, связей {row.edges}, "
            f"загружено {row.loaded_at}"
        )
        print(f"  метаданные : да")
        if row.syntax_present and row.syntax_platform:
            отношение = _RELATION_TITLES.get(
                row.syntax_relation, row.syntax_relation
            )
            скрыто = f", скрыто {row.syntax_hidden}" if row.syntax_hidden else ""
            состояние = f"справка {row.syntax_platform}, {отношение}{скрыто}"
        else:
            состояние = "не подключён"
        print(f"  синтаксис  : {состояние}")
        for state in row.code:
            label = (
                "модули"
                if state.corpus == "Основная конфигурация"
                else "расширение " + state.corpus.removeprefix("Расширение ")
            )
            print(f"  {label:<11}: {state.state}")
            for line in tools.code_coverage_lines(state.coverage):
                print(f"    {line}")
        runtime = row.extension_runtime
        print(
            "  расширения : "
            + (
                f"сеансовый снимок, {runtime.items_total} элементов, "
                f"загружен {runtime.loaded_at}"
                if runtime is not None
                else "фактическая активность unknown"
            )
        )
        for note in row.notes:
            print(f"  ! {note}")
        print()
    return 0


_RELATION_TITLES = {
    "exact": "версия совпадает",
    "newer": "новее конфигурации",
    "older": "СТАРЕЕ конфигурации",
    "none": "не подключён",
    "unknown": "фактическая версия платформы неизвестна",
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
        hits = context.syntax.index.search(
            args.query, limit=args.limit, predicate=lambda doc: keep(doc.payload),
        )
        # Предварительное окно ограничивает лишь диагностическую выборку.
        raw = context.syntax.index.search(args.query, limit=args.limit * 3)
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


def _cmd_reg_search_procedures(args: argparse.Namespace) -> int:
    """CLI-зеркало MCP `search_procedures` без второго пути рендера."""
    registry = _registry_for_code(args)
    print(
        tools.search_procedures(
            registry,
            args.query,
            args.config,
            args.extension,
            args.scope,
            args.limit,
        )
    )
    return 0


def _cmd_reg_get_procedure(args: argparse.Namespace) -> int:
    """CLI-зеркало MCP `get_procedure` с теми же границами окна."""
    registry = _registry_for_code(args)
    print(
        tools.get_procedure(
            registry,
            args.address,
            args.config,
            args.extension,
            args.start_line,
            args.lines,
        )
    )
    return 0


def _cmd_reg_get_callers(args: argparse.Namespace) -> int:
    """CLI-зеркало MCP `get_callers`, включая честные предупреждения."""
    registry = _registry_for_code(args)
    print(
        tools.get_callers(
            registry,
            args.address,
            args.config,
            args.extension,
            args.limit,
        )
    )
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
    snapshot = registry.snapshot()
    if args.config and args.config in snapshot.configurations:
        objects = snapshot.configurations[args.config].config.objects
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
        if name != "info":
            sp.add_argument(
                "--allow-truncated",
                action="store_true",
                help="явно разрешить неполную выгрузку truncated=true",
            )
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
            sp.add_argument(
                "path",
                help="ZIP структуры, .hbk справки или .json снимка расширений",
            )
            sp.add_argument(
                "--allow-truncated",
                action="store_true",
                help="явно опубликовать неполную выгрузку truncated=true",
            )
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

    search_procedures = sub.add_parser(
        "reg-search-procedures",
        help="найти процедуры в загруженном коде",
    )
    search_procedures.add_argument(
        "query", help="имя, слова о назначении или фраза о типовом событии"
    )
    search_procedures.add_argument("--data", default="data")
    search_procedures.add_argument("--config", default=None)
    search_procedures.add_argument("--extension", default=None)
    search_procedures.add_argument("--scope", default=None)
    search_procedures.add_argument("--limit", type=int, default=10)
    search_procedures.set_defaults(handler=_cmd_reg_search_procedures)

    get_procedure = sub.add_parser(
        "reg-get-procedure",
        help="показать оглавление модуля или тело процедуры",
    )
    get_procedure.add_argument("address", help="адрес модуля или Модуль::Имя")
    get_procedure.add_argument("--data", default="data")
    get_procedure.add_argument("--config", default=None)
    get_procedure.add_argument("--extension", default=None)
    get_procedure.add_argument("--start-line", type=int, default=0)
    get_procedure.add_argument("--lines", type=int, default=200)
    get_procedure.set_defaults(handler=_cmd_reg_get_procedure)

    get_callers = sub.add_parser(
        "reg-get-callers",
        help="показать места вызова точной процедуры",
    )
    get_callers.add_argument("address", help="точный адрес Модуль::Имя")
    get_callers.add_argument("--data", default="data")
    get_callers.add_argument("--config", default=None)
    get_callers.add_argument("--extension", default=None)
    get_callers.add_argument("--limit", type=int, default=20)
    get_callers.set_defaults(handler=_cmd_reg_get_callers)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except RegistryError as error:
        print(f"Реестр: {error}", file=sys.stderr)
        return 2
    except ExportError as error:
        print(f"Ошибка выгрузки: {error}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"Ошибка параметров: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
