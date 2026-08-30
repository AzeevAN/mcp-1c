"""Уборка разобранных справок, которые больше никем не заявлены.

Справка одна на процесс: загрузили новую — прежняя выбывает из реестра вместе
со своим кэшем. А разобранный индекс в `index/syntax/` оставался лежать, и
каталог рос с каждой попыткой. На рабочей установке накопилось три файла при
одном используемом, плюс 138 МБ исходников.

Индекс — производное: он восстанавливается разбором `.hbk` за считанные
секунды, поэтому сносится так же, как кэш. Исходные `.hbk` не трогаем: справку
от снятой с поддержки платформы взять заново негде.
"""

from __future__ import annotations

import json

from mcp1c.registry import Registry

from conftest import write_syntax, живой_клиент


def test_чужой_разобранный_индекс_сносится_на_старте(tmp_path):
    data = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data.mkdir()
    incoming.mkdir()

    registry = Registry(data)
    registry.add_syntax(write_syntax(data / "index" / "syntax", platform="8.3.99.1"))
    registry.save()

    # Остаток прошлой загрузки: реестр о нём не знает.
    чужой = data / "index" / "syntax" / "8.3.5.1570.json.gz"
    чужой.write_bytes("остаток прошлой справки".encode("utf-8"))

    сообщения = Registry(data).startup()

    assert not чужой.exists()
    assert any("справок" in m for m in сообщения), сообщения


def test_используемый_индекс_остаётся(tmp_path):
    data = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data.mkdir()
    incoming.mkdir()

    registry = Registry(data)
    registry.add_syntax(write_syntax(data / "index" / "syntax", platform="8.3.99.1"))
    registry.save()
    свой = data / "index" / "syntax" / "8.3.99.1.json.gz"
    assert свой.exists()

    заново = Registry(data)
    заново.startup()

    assert свой.exists()
    assert заново.syntax is not None
    assert len(заново.syntax.syntax) == 3


def test_устаревший_вид_источника_снимается_и_его_индекс_убирается(tmp_path):
    """Обновление не должно оставлять источник, которого новый код не читает."""
    data = tmp_path / "data"
    index = data / "index" / "syntax" / "устаревший.json.gz"
    index.parent.mkdir(parents=True)
    index.write_bytes(b"retired derived index")
    registry_path = data / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "registry_version": 1,
                "sources": [
                    {
                        "id": "retired-reference",
                        "kind": "retired-reference",
                        "status": "ready",
                        "stored_path": "index/syntax/устаревший.json.gz",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    registry = Registry(data)
    messages = registry.startup()

    assert registry.sources == {}
    assert not index.exists()
    assert any("больше не поддерживается и снят с учёта" in row for row in messages)
    saved = json.loads(registry_path.read_text(encoding="utf-8"))
    assert saved["sources"] == []


def test_исходные_файлы_не_трогаются(tmp_path):
    """Справку от старой платформы взять заново негде — не удаляем молча."""
    data = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data.mkdir()
    incoming.mkdir()

    registry = Registry(data)
    registry.add_syntax(write_syntax(data / "index" / "syntax", platform="8.3.99.1"))
    registry.save()

    исходник = data / "sources" / "hbk" / "8.3.5.1570.hbk"
    исходник.parent.mkdir(parents=True, exist_ok=True)
    исходник.write_bytes("исходник прошлой справки".encode("utf-8"))

    Registry(data).startup()

    assert исходник.exists()


def test_неиспользуемые_исходники_перечисляются(tmp_path):
    """Место занято — человек должен об этом узнать и решить сам."""
    data = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data.mkdir()
    incoming.mkdir()

    registry = Registry(data)
    registry.add_syntax(write_syntax(data / "index" / "syntax", platform="8.3.99.1"))
    registry.save()

    лишний = data / "sources" / "hbk" / "8.3.5.1570.hbk"
    лишний.parent.mkdir(parents=True, exist_ok=True)
    лишний.write_bytes(b"x" * 2048)

    orphans = Registry(data).orphan_sources()

    assert [(p.name, size) for p, size in orphans] == [("8.3.5.1570.hbk", 2048)]


def test_страница_источников_показывает_неиспользуемые_файлы(tmp_path, monkeypatch):
    """Сто мегабайт молча — плохо; список с размерами и кнопкой — приемлемо."""
    from starlette.applications import Starlette

    from mcp1c import dashboard

    monkeypatch.setenv("ADMIN_TOKEN", "secret-token")
    monkeypatch.delenv("API_TOKEN", raising=False)

    data = tmp_path / "data"
    data.mkdir()
    registry = Registry(data)
    registry.add_syntax(write_syntax(data / "index" / "syntax", platform="8.3.99.1"))

    лишний = data / "sources" / "hbk" / "8.3.5.1570.hbk"
    лишний.parent.mkdir(parents=True, exist_ok=True)
    лишний.write_bytes(b"x" * (3 * 1024 * 1024))

    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))
    страница = client.get("/sources").text

    assert "8.3.5.1570.hbk" in страница
    assert "3" in страница  # размер в мегабайтах
    # Удалять может только администратор.
    assert "/sources/forget" not in страница
    client.post("/login", data={"token": "secret-token"})
    assert "/sources/forget" in client.get("/sources").text


def test_удаление_неиспользуемого_файла(tmp_path, monkeypatch):
    from starlette.applications import Starlette

    from mcp1c import dashboard

    monkeypatch.setenv("ADMIN_TOKEN", "secret-token")
    monkeypatch.delenv("API_TOKEN", raising=False)

    data = tmp_path / "data"
    data.mkdir()
    registry = Registry(data)
    registry.add_syntax(write_syntax(data / "index" / "syntax", platform="8.3.99.1"))
    лишний = data / "sources" / "hbk" / "8.3.5.1570.hbk"
    лишний.parent.mkdir(parents=True, exist_ok=True)
    лишний.write_bytes(b"x" * 1024)

    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))
    client.post("/login", data={"token": "secret-token"})
    ответ = client.post(
        "/sources/forget", data={"path": "sources/hbk/8.3.5.1570.hbk"},
        follow_redirects=False,
    )

    assert ответ.status_code == 303
    assert not лишний.exists()
    # Используемое остаётся на месте.
    assert registry.syntax is not None


def test_удаление_не_выходит_за_каталог_данных(tmp_path, monkeypatch):
    """Путь приходит от клиента — из него нельзя сложить дорогу наружу."""
    from starlette.applications import Starlette

    from mcp1c import dashboard

    monkeypatch.setenv("ADMIN_TOKEN", "secret-token")
    monkeypatch.delenv("API_TOKEN", raising=False)

    data = tmp_path / "data"
    data.mkdir()
    посторонний = tmp_path / "чужое.txt"
    посторонний.write_bytes(b"not yours")

    registry = Registry(data)
    registry.add_syntax(write_syntax(data / "index" / "syntax", platform="8.3.99.1"))
    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))
    client.post("/login", data={"token": "secret-token"})

    client.post("/sources/forget", data={"path": "../чужое.txt"})

    assert посторонний.exists()


def test_справка_не_копируется_в_sources(tmp_path, monkeypatch):
    """Копия `.hbk` не нужна: восстановление идёт из разобранного индекса.

    Реестр хранит у справки `stored_path` на `index/syntax/*.json.gz` и при
    старте читает именно его — исходник не открывается ни разу. Копирование
    стоило 39 МБ на каждую загруженную справку и не давало ничего: команды
    «переразобрать из сохранённого» не существует, а повторная загрузка того же
    файла отсекается по хешу.
    """
    data = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data.mkdir()
    incoming.mkdir()

    # Разбор подменяется: проверяем поведение реестра, а не работу парсера.
    from conftest import build_syntax
    from mcp1c import registry as registry_module

    источник = incoming / "shcntx_ru.hbk"
    источник.write_bytes(b"container bytes, parsing is stubbed out")
    monkeypatch.setattr(
        registry_module, "parse_hbk", lambda path, platform="": build_syntax("8.3.99.1")
    )
    registry = Registry(data)
    registry.add_syntax(источник)

    assert not (data / "sources" / "hbk").exists()
    # Чужой файл на месте: мы его не перекладываем и не удаляем.
    assert источник.exists()
    assert registry.syntax is not None
    # Восстановление читает разобранный индекс, а не исходник. Имя источника
    # здесь выводится из самих данных: в «shcntx_ru.hbk» версии нет.
    учётная = registry.sources[registry.syntax.source.id]
    assert учётная.stored_path.startswith("index/syntax/")


def test_выгрузка_конфигурации_по_прежнему_копируется(tmp_path):
    """У конфигураций иначе: реестр восстанавливается именно из `.zip`."""
    from conftest import build_configuration, write_export

    data = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data.mkdir()
    incoming.mkdir()

    registry = Registry(data)
    registry.add_configuration(write_export(incoming, build_configuration()))

    хранимые = list((data / "sources" / "configurations").glob("*.zip"))
    assert len(хранимые) == 1
