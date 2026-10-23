"""Версия справки из пути и публичный ID принятого синтетического HBK."""

import io
from pathlib import Path
import zipfile

import pytest

from mcp1c.registry import Registry, _platform_from_path
from module_samples import v8_container_bytes


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("shcntx_ru_8.5.1.1529.hbk", "8.5.1.1529"),
        ("shcntx_ru.8.3.27.hbk", "8.3.27"),
        ("8.3.27.2130/shcntx_ru.hbk", "8.3.27.2130"),
        ("syntax-8.3.5.1570.hbk", "8.3.5.1570"),
        ("v8.3.5.1.hbk", "8.3.5.1"),
        ("shcntx_ru.hbk", ""),
    ],
)
def test_platform_from_path_сохраняет_полную_версию(path, expected):
    assert _platform_from_path(Path(path)) == expected


@pytest.fixture
def synthetic_hbk(tmp_path):
    # Настоящий V8-контейнер и вложенный ZIP проходят боевой разбор без подмен.
    page = (
        '<html><h1 class="V8SH_pagetitle">Массив (Array)</h1>'
        '<p class="V8SH_chapter">Описание:</p>'
        '<p>Синтетическая коллекция элементов.</p></html>'
    ).encode("utf-8")
    storage = io.BytesIO()
    with zipfile.ZipFile(storage, "w") as archive:
        archive.writestr("objects/Array.html", page)
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    path = incoming / "shcntx_ru_8.5.1.1529.hbk"
    path.write_bytes(v8_container_bytes([("FileStorage", storage.getvalue())]))
    return path


def test_add_syntax_берёт_полную_версию_из_имени_hbk(tmp_path, synthetic_hbk):
    registry = Registry(tmp_path / "registry")

    source = registry.add_syntax(synthetic_hbk)

    assert source.id == "syntax-8.5.1.1529"
    assert source.platform == "8.5.1.1529"
