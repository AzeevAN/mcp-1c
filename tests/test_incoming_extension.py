"""Разбор расширения по кнопке: вид источника по-русски, дубликат по имени файла."""
import time
import zipfile
from pathlib import Path

from conftest import build_configuration, состарить, write_export, живой_клиент
from starlette.applications import Starlette

from mcp1c import dashboard
from mcp1c.registry import Registry

_NS = "http://v8.1c.ru/8.3/MDClasses"


def _extension_xml(*, name: str = "РасширениеА") -> str:
    return (
        f'<MetaDataObject xmlns="{_NS}">'
        '<Configuration uuid="00000000-0000-0000-0000-000000000000">'
        "<Properties>"
        f"<Name>{name}</Name>"
        f"<NamePrefix>{name}_</NamePrefix>"
        "<ObjectBelonging>Adopted</ObjectBelonging>"
        "<ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose>"
        "</Properties></Configuration></MetaDataObject>"
    )


def _расширение(путь: Path, *, name: str = "РасширениеА") -> Path:
    with zipfile.ZipFile(путь, "w") as zf:
        zf.writestr("Configuration.xml", _extension_xml(name=name))
        zf.writestr("Catalogs/Р/Ext/ObjectModule.bsl", "Процедура А() КонецПроцедуры")
    return состарить(путь)


def _стенд(tmp_path):
    данные = tmp_path / "data"
    входящее = tmp_path / "in"
    данные.mkdir()
    входящее.mkdir()
    registry = Registry(данные)
    registry.add_configuration(write_export(входящее, build_configuration(name="Розница")))
    registry.incoming_dir.mkdir(parents=True, exist_ok=True)
    client = живой_клиент(Starlette(routes=dashboard.routes(registry)))
    return client, registry


def дождаться(client, условие, таймаут: float = 20.0) -> str:
    предел = time.monotonic() + таймаут
    текст = ""
    while time.monotonic() < предел:
        текст = client.get("/sources").text
        if условие(текст):
            return текст
        time.sleep(0.05)
    raise AssertionError(f"за {таймаут} с условие не выполнилось:\n{текст}")


def test_вид_расширения_подписан_по_русски(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})
    _расширение(registry.incoming_dir / "расширение.zip")

    client.post(
        "/sources/incoming/parse",
        data={"name": "расширение.zip"},
        follow_redirects=False,
    )
    текст = дождаться(client, lambda t: "Розница:ext:РасширениеА" in t)

    # Строка «Загружено» называет вид по-русски, а не сырым словом `extension`.
    assert "<td>Расширение<td>" in текст


def test_две_копии_одного_расширения_под_разными_именами_показывают_разобрано(
    tmp_path, monkeypatch
):
    """Владелец: два файла-переименования одного расширения — источник один,
    а обе строки «Входящих» после разбора показывают «разобрано»."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    monkeypatch.delenv("API_TOKEN", raising=False)
    client, registry = _стенд(tmp_path)
    client.post("/login", data={"token": "секрет"})
    оригинал = registry.incoming_dir / "оригинал.zip"
    _расширение(оригинал)
    копия = registry.incoming_dir / "РасширениеА-копия.zip"
    копия.write_bytes(оригинал.read_bytes())
    состарить(копия)

    client.post(
        "/sources/incoming/parse",
        data={"name": "оригинал.zip"},
        follow_redirects=False,
    )
    дождаться(client, lambda t: "Розница:ext:РасширениеА" in t)

    client.post(
        "/sources/incoming/parse",
        data={"name": "РасширениеА-копия.zip"},
        follow_redirects=False,
    )
    текст = дождаться(client, lambda t: t.count("разобрано") >= 2)

    # Один источник расширения на два файла с одинаковым содержимым.
    ключи_расширений = [i for i in registry.sources if i.startswith("Розница:ext:")]
    assert ключи_расширений == ["Розница:ext:РасширениеА"]
    assert "оригинал.zip" in текст
    assert "РасширениеА-копия.zip" in текст
    assert текст.count("разобрано") >= 2
    # Последний разобранный файл — источник происхождения в реестре.
    assert registry.sources["Розница:ext:РасширениеА"].origin == "РасширениеА-копия.zip"
