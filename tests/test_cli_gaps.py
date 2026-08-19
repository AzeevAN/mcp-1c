"""Отладочный CLI обязан говорить человеку правду.

Три дефекта одного класса, все найдены и отложены раньше: сервер знает, что
загружено, а `reg-list` и первый отказ реестра говорят другое. В путь агента
это не входит — платит за такое человек у первого запуска, и молча.
"""

import pytest

from conftest import build_configuration, query_hbk_stub, write_export
from mcp1c.cli import main
from mcp1c.registry import Registry, RegistryError


def test_reg_list_не_врёт_про_пустоту_когда_есть_язык_запросов(tmp_path, capsys):
    """Тот же класс дефекта, что чинили в `list_configurations`: ветка
    «ничего не загружено» срабатывает раньше проверки источников. Здесь она
    ещё и возвращала код 1 — то есть скрипт вокруг считал бы это отказом."""
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    источник = registry.add_syntax(query_hbk_stub(incoming / "query"))
    registry.save()

    код = main(["reg-list", "--data", str(tmp_path / "data")])
    вывод = capsys.readouterr().out

    assert код == 0
    assert "Ничего не загружено" not in вывод
    assert f"{источник.items_total} страниц" in вывод


def test_reg_list_не_печатает_пустое_имя_платформы(tmp_path, capsys):
    """При конфигурации без справки платформы, но с языком запросов строка
    выходила противоречивой: «синтаксис : справка , не подключён» — пустое
    имя платформы рядом со словом «справка»."""
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(incoming, build_configuration()))
    registry.add_syntax(query_hbk_stub(incoming / "query"))
    registry.save()

    main(["reg-list", "--data", str(tmp_path / "data")])
    вывод = capsys.readouterr().out

    assert "справка ," not in вывод
    assert "справка  " not in вывод


def test_пустой_реестр_называет_оба_недостающих_источника(tmp_path):
    """На полностью пустом сервере отказ приходит из `registry.resolve` и
    говорит только про выгрузку структуры. Справок в таком реестре тоже нет,
    и человек у первого запуска узнаёт об этом лишь со второго захода."""
    registry = Registry(tmp_path / "data")

    with pytest.raises(RegistryError) as ошибка:
        registry.resolve(None)

    текст = str(ошибка.value)
    assert "выгрузк" in текст.lower()
    assert "справк" in текст.lower()


def test_дашборд_рисует_блок_кода_моноширинным():
    """`render_markdown` знал заголовки, списки, цитаты, таблицы и
    внутристрочный код, но ограждённые блоки печатал абзацами: сигнатуры и
    примеры теряли и моноширинный вид, и отступы. Дефект ровесник самого
    `render_markdown` — карточка справки состоит из таких блоков."""
    from mcp1c.dashboard import render_markdown

    html = render_markdown(
        "Текст до.\n```bsl\nЕсли Истина Тогда\n    Сообщить(1);\nКонецЕсли;\n```\nТекст после."
    )

    assert "<pre><code>" in html
    assert "    Сообщить(1);" in html
    assert "```" not in html
    assert "<p>Текст до.</p>" in html and "<p>Текст после.</p>" in html


def test_незакрытый_блок_кода_не_съедает_остаток_страницы():
    """Разметку пишем мы сами, но карточка собирается из текста справки, и
    тройная кавычка может прийти оттуда. Проглотить хвост страницы молча
    нельзя — это тот же класс, что незакрытая таблица в справке."""
    from mcp1c.dashboard import render_markdown

    html = render_markdown("```bsl\nСообщить(1);\n\nОбычный абзац, блок не закрыт.")

    assert "Обычный абзац, блок не закрыт." in html
    assert "Сообщить(1);" in html


def test_внутри_блока_кода_разметка_не_разбирается():
    """Звёздочка и решётка в коде — часть кода, а не markdown."""
    from mcp1c.dashboard import render_markdown

    html = render_markdown("```\n# не заголовок\n- не список\n```")

    assert "<h1>" not in html and "<ul>" not in html
    assert "# не заголовок" in html
