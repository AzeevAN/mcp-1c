"""Разбор расширения по кнопке: вид источника по-русски, дубликат по имени файла."""
import threading
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


def дождаться_завершения(client, registry: Registry, имя: str) -> str:
    """Дождаться и результата job, и освобождения слота фонового разбора."""
    сканер = dashboard._scanner(registry)

    def завершено(_текст: str) -> bool:
        задания = [job for job in dashboard._JOBS if job["name"] == имя]
        return bool(
            задания
            and задания[-1]["state"] in (dashboard.JOB_DONE, dashboard.JOB_FAILED)
            and имя not in сканер.running
        )

    return дождаться(client, завершено)


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
    текст = дождаться_завершения(client, registry, "расширение.zip")

    # Строка «Загружено» называет вид по-русски, а не сырым словом `extension`.
    assert "Розница:ext:РасширениеА" in текст
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
    сканер = dashboard._scanner(registry)
    # Источник публикуется раньше финальной очистки. Удерживаем именно это окно,
    # чтобы тест не зависел от того, успел ли фоновый поток на конкретной машине.
    дошёл_до_завершения = threading.Event()
    разрешить_завершение = threading.Event()
    настоящий_clear_failure = type(сканер).clear_failure

    def задержать_первый_clear_failure(self, путь):
        if путь.name == оригинал.name:
            дошёл_до_завершения.set()
            assert разрешить_завершение.wait(5)
        return настоящий_clear_failure(self, путь)

    monkeypatch.setattr(
        type(сканер), "clear_failure", задержать_первый_clear_failure
    )

    client.post(
        "/sources/incoming/parse",
        data={"name": "оригинал.zip"},
        follow_redirects=False,
    )
    дождаться(client, lambda t: "Розница:ext:РасширениеА" in t)
    assert дошёл_до_завершения.wait(5)
    assert оригинал.name in сканер.running

    разрешить_завершение.set()
    дождаться_завершения(client, registry, оригинал.name)

    client.post(
        "/sources/incoming/parse",
        data={"name": "РасширениеА-копия.zip"},
        follow_redirects=False,
    )
    текст = дождаться_завершения(client, registry, копия.name)

    # Один источник расширения на два файла с одинаковым содержимым.
    ключи_расширений = [i for i in registry.sources if i.startswith("Розница:ext:")]
    assert ключи_расширений == ["Розница:ext:РасширениеА"]
    assert "оригинал.zip" in текст
    assert "РасширениеА-копия.zip" in текст
    assert текст.count("разобрано") >= 2
    # Последний разобранный файл — источник происхождения в реестре.
    assert registry.sources["Розница:ext:РасширениеА"].origin == "РасширениеА-копия.zip"
