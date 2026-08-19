"""Дашборд и CLI знают про источник «язык запросов».

После задач 1-4 сервер принимает `shquery_ru.hbk` отдельным видом и отвечает
по нему наравне со справкой платформы. Подсказка на `/sources` и списки —
дашбордный и `reg-list` — обязаны говорить о нём правду: до этой задачи
подсказка прямо утверждала, что файл не подходит, а загруженный источник в
списках был виден только сырым словом `query`, не человеку.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c import dashboard
from mcp1c.cli import main
from mcp1c.registry import Registry

from conftest import build_configuration, query_hbk_stub, write_export, живой_клиент


def client_for(tmp_path) -> tuple[TestClient, Registry]:
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    registry.add_configuration(write_export(incoming, build_configuration()))
    app = Starlette(routes=dashboard.routes(registry))
    return живой_клиент(app), registry


def test_страница_источников_называет_язык_запросов(tmp_path, monkeypatch):
    """Подсказка о принимаемых файлах не должна лгать про `shquery_ru.hbk`.

    До правки в этом же абзаце было сказано, что справка по языку запросов
    «не подходит» — а реестр её уже принимает. Простого «подстрока на
    странице» мало: она была там и раньше, в ложном контексте. Проверяем,
    что файл описан отдельным пунктом — так же, как `shcntx_ru.hbk`.
    """
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, _ = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    страница = client.get("/sources").text

    assert "shquery_ru.hbk" in страница
    assert "<b>Язык запросов</b>" in страница
    # Ложного утверждения, что файл — «другая справка» наравне с
    # неподходящими, быть не должно.
    assert "это другие справки" not in страница


def test_загруженный_язык_запросов_виден_в_списке(tmp_path):
    client, registry = client_for(tmp_path)
    источник = registry.add_syntax(query_hbk_stub(tmp_path / "incoming" / "query"))
    registry.save()

    страница = client.get("/sources").text

    assert "Язык запросов" in страница
    assert str(источник.items_total) in страница


def test_reg_list_называет_язык_запросов(tmp_path, capsys):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(incoming, build_configuration()))
    источник = registry.add_syntax(query_hbk_stub(incoming / "query"))
    registry.save()

    код = main(["reg-list", "--data", str(tmp_path / "data")])
    вывод = capsys.readouterr().out

    assert код == 0
    # Не просто «слово где-то в выводе»: строка «синтаксис» и так называет
    # язык запросов в примечании про недоступность справки платформы — этим
    # тест ловится и без правки. Проверяем отдельную строку с числом страниц.
    assert f"язык запросов: подключён, {источник.items_total} страниц" in вывод


def test_подпись_переключателя_syntax_называет_язык_запросов(tmp_path):
    """Подпись «по справке платформы» врёт: поиск в этом режиме идёт и по
    языку запросов (`shquery_ru.hbk`) — с задачи query-ranking он в том же
    индексе, что и справка платформы."""
    client, _ = client_for(tmp_path)

    страница = client.get("/queries").text

    assert "по справке платформы и языку запросов" in страница


def test_таблица_выдачи_показывает_вид_найденного_элемента(tmp_path):
    """Без вида страница не отвечает на свой главный вопрос «почему выдача
    такая»: не видно, что первое место заняла статья языка запросов, а не
    платформенное ключевое слово с похожим именем. Подписи — те же, что
    печатает MCP (`KIND_TITLES` из `syntax_model.py`)."""
    client, registry = client_for(tmp_path)
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming" / "query"))
    registry.save()

    response = client.post(
        "/queries",
        data={
            "config": "ТестоваяКонфигурация",
            "scope": "syntax",
            "phrases": "итоги по иерархии",
        },
    )

    assert "<th>Вид" in response.text
    assert "Статья по языку запросов" in response.text


def test_адрес_элемента_языка_запросов_одинаков_везде(tmp_path):
    """Владелец заметил на живом дашборде 2026-08-19: в таблице выдачи стоит
    голое `СтрНайти`, а карточка одноимённых печатает `Запрос.СтрНайти`. Один
    элемент — два адреса, и голое имя неоднозначно: по нему `get_syntax`
    вернёт список одноимённых, а не карточку.
    """
    client, registry = client_for(tmp_path)
    registry.add_syntax(query_hbk_stub(tmp_path / "incoming" / "query"))
    registry.save()

    response = client.post(
        "/queries",
        data={
            "config": "ТестоваяКонфигурация",
            "scope": "syntax",
            "phrases": "левое соединение",
        },
    )

    # Ключевое слово языка запросов — адрес с квалификатором и в подписи,
    # и в ссылке на карточку.
    assert "Запрос.Левое внешнее соединение" in response.text
    assert "name=Запрос." in response.text or "name=%D0%97" in response.text
