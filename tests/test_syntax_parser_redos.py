"""линейность и локальный отказ страниц."""

from copy import copy
import io
from pathlib import Path
from random import Random
import subprocess
import sys
from time import perf_counter
import zipfile

import pytest

from mcp1c import registry as registry_module
from mcp1c import syntax_parser
from mcp1c.registry import Registry
from mcp1c.resource_limits import ResourceLimitError
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem
from module_samples import v8_container_bytes


# Линейность меряется отношением, а не абсолютным порогом: скорость машины
# сокращается. Вход в 4 раза больше даёт ≈4× времени у линейного разбора и
# ≈16× у квадратичного (до фикса: 4n/n = 15,7–16,2, после — 3,8–4,0).
# Порог 8 — посередине. Минимум из нескольких запусков отсекает паузы
# планировщика; до 20 мс на большом входе рост не различим и не важен.
_SCALE = 4
_MAX_RATIO = 8
_NOISE_FLOOR = 0.02


def _best_time(function, argument, repeats=5):
    best = float("inf")
    for _ in range(repeats):
        started = perf_counter()
        result = function(argument)
        best = min(best, perf_counter() - started)
    return best, result


def _assert_linear(function, make_input, size, what):
    small, _ = _best_time(function, make_input(size // _SCALE))
    big, result = _best_time(function, make_input(size))
    assert big < max(_MAX_RATIO * small, _NOISE_FLOOR), (
        f"{what}: {big:.4f} с на n={size} против {small:.4f} с на n/{_SCALE}"
    )
    return result


@pytest.mark.parametrize("fragment_kind", ["title", "see_also", "title_cr", "title_cr_bilingual"])
def test_повторяющиеся_атрибуты_не_блокируют_parse_page(fragment_kind):
    """Незавершённый тег допустимого размера обрабатывается за ограниченное время."""
    if fragment_kind == "title":
        raw = ('<h1 ' + 'class="V8SH_pagetitle" ' * 16_000).encode("utf-8")
    elif fragment_kind.startswith("title_cr"):
        title = "A" + "\r" * 50_000 + "Z"
        if fragment_kind == "title_cr_bilingual":
            title += "(English)"
        raw = f'<h1 class="V8SH_pagetitle">{title}</h1>'.encode("utf-8")
    else:
        raw = (
            '<h1 class="V8SH_pagetitle">Synthetic</h1>'
            '<p class="V8SH_chapter">См. также</p>'
            '<a ' + 'href="x" ' * 16_000 + '>'
        ).encode("utf-8")
    assert len(raw) < 454_773

    code = r"""
import sys
from mcp1c.syntax_parser import parse_page

item = parse_page("objects/Synthetic.html", sys.stdin.buffer.read())
if sys.argv[1] == "title":
    assert item is None
elif sys.argv[1].startswith("title_cr"):
    assert item is not None
    assert item.name_ru == "A" + "\r" * 50_000 + "Z"
    assert item.name_en == ("English" if sys.argv[1] == "title_cr_bilingual" else "")
else:
    assert item is not None
    assert item.name_ru == "Synthetic"
    assert item.see_also == []
"""
    # Таймаут завершает дочерний процесс даже при регрессии в регулярном выражении.
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code, fragment_kind],
            input=raw,
            capture_output=True,
            cwd=Path(syntax_parser.__file__).resolve().parents[1],
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("Разбор повторяющихся атрибутов не завершился за 10 с")
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def _m5_page(size=None, *, title="Sized"):
    prefix = (
        f'<h1 class="V8SH_pagetitle">{title}</h1>'
        '<p class="V8SH_chapter">Описание:</p><p>Data</p><!--'
    ).encode("utf-8")
    suffix = b"-->"
    filler_size = 32 if size is None else size - len(prefix) - len(suffix)
    assert filler_size >= 0
    filler = Random(17).randbytes((filler_size + 1) // 2).hex().encode("ascii")[:filler_size]
    result = prefix + filler + suffix
    if size is not None:
        assert len(result) == size
    return result


def _m5_hbk(tmp_path):
    storage = io.BytesIO()
    with zipfile.ZipFile(storage, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("objects/Large.html", _m5_page(454_773 + 65_536, title="Large"))
        archive.writestr("objects/Good.html", _m5_page(title="Good"))
    path = tmp_path / "syntax-8.3.27.2130.hbk"
    path.write_bytes(v8_container_bytes([("FileStorage", storage.getvalue())]))
    return path


def test_4000_незакрытых_chapter_не_блокируют_parse_page():
    def page(count):
        return (
            '<h1 class="V8SH_pagetitle">BudgetObject</h1>'
            '<p class="V8SH_chapter">Описание:</p>Синтетическое описание'
            + '<p class="V8SH_chapter" a=abcd>' * count
        ).encode("utf-8")

    item = _assert_linear(
        lambda raw: syntax_parser.parse_page("objects/BudgetObject.html", raw),
        page, 4000, "Разбор незакрытых chapter",
    )

    assert item is not None
    assert item.name_ru == "BudgetObject"
    assert item.description == "Синтетическое описание"


def test_угловые_скобки_без_закрытия_не_блокируют_очистку_тегов():
    result = _assert_linear(
        syntax_parser._text, lambda count: "<" * count, 32_000,
        "Очистка незавершённых тегов",
    )

    assert result == "<" * 32_000


def test_начала_ссылок_без_закрывающей_скобки_не_блокируют_выдачу_href():
    href = "v8help://SyntaxHelperContext/objects/Good.html"
    result = _assert_linear(
        syntax_parser._link_hrefs,
        lambda count: f'<a href="{href}">Good</a>' + "<a " * count, 8000,
        "Разбор незавершённых открытий a",
    )

    assert result == [href]


def test_открытые_a_без_закрытия_не_блокируют_выдачу_href():
    href = "v8help://SyntaxHelperContext/objects/Good.html"
    result = _assert_linear(
        syntax_parser._link_hrefs,
        lambda count: f'<a href="{href}">Good</a>' + '<a href="x">' * count, 4000,
        "Разбор открытых a без закрытия",
    )

    assert result == [href]


@pytest.mark.parametrize("size", [454_773, 454_774])
def test_parse_page_различает_границу_размера_и_следующий_байт(size):
    raw = _m5_page(size)

    if size <= 454_773:
        item = syntax_parser.parse_page("objects/Sized.html", raw)
        assert item is not None
        assert item.name_ru == "Sized"
    else:
        with pytest.raises(ResourceLimitError):
            syntax_parser.parse_page("objects/Sized.html", raw)


@pytest.mark.parametrize("consumer", ["parse_hbk", "iter_pages"])
@pytest.mark.parametrize("understated_size", [False, True])
def test_hbk_пропускает_oversized_page_с_диагностикой_и_ограниченным_чтением(
    tmp_path, monkeypatch, consumer, understated_size
):
    from mcp1c.resource_limits import LimitedZipFile

    hbk = _m5_hbk(tmp_path)
    read_sizes = []
    returned_bytes = 0
    original_read = zipfile.ZipExtFile.read
    original_infos = LimitedZipFile.infolist

    def checked_read(stream, n=-1):
        nonlocal returned_bytes
        if stream.name != "objects/Large.html":
            return original_read(stream, n)
        assert 0 <= n <= 454_774, "Страница запрошена целиком вместо bounded read"
        read_sizes.append(n)
        result = original_read(stream, n)
        returned_bytes += len(result)
        assert returned_bytes <= 454_774
        return result

    def understated_infos(archive):
        infos = [copy(info) for info in original_infos(archive)]
        for info in infos:
            if info.filename == "objects/Large.html":
                info.file_size = 454_773
        return infos

    monkeypatch.setattr(zipfile.ZipExtFile, "read", checked_read)
    if understated_size:
        # Только метаданные потребителя лгут: реальный ZIP-reader отдаёт исходные байты.
        monkeypatch.setattr(LimitedZipFile, "infolist", understated_infos)

    if consumer == "parse_hbk":
        index = syntax_parser.parse_hbk(hbk, platform="8.3.27.2130")
        assert set(index.items) == {"objects/Good"}
        assert any("objects/Large.html" in warning for warning in index.warnings), index.warnings
        assert index.items["objects/Good"].description.startswith("Data")
    else:
        with pytest.warns(RuntimeWarning, match=r"objects/Large\.html"):
            items = list(syntax_parser.iter_pages(hbk))
        assert [item.id for item in items] == ["objects/Good"]
        assert items[0].description.startswith("Data")

    if understated_size:
        assert read_sizes
        assert returned_bytes == 454_774
    else:
        assert read_sizes == []
        assert returned_bytes == 0


def test_add_syntax_сохраняет_предупреждения_индекса_в_source(tmp_path, monkeypatch):
    warning = "objects/Large.html: страница больше предела 454773 байта"
    index = SyntaxIndex(platforms=["8.3.27.2130"], warnings=[warning])
    index.add(SyntaxItem(id="objects/Good", kind="object", name_ru="Good",
                         description="Синтетическое описание"))
    hbk = tmp_path / "syntax-8.3.27.2130.hbk"
    hbk.write_bytes(b"synthetic parser input")

    def parsed(path, *, platform):
        assert path == hbk
        assert platform == "8.3.27.2130"
        return index

    monkeypatch.setattr(registry_module, "parse_hbk", parsed)

    source = Registry(tmp_path / "registry").add_syntax(hbk)

    assert source.id == "syntax-8.3.27.2130"
    assert source.items_total == 1
    assert warning in source.warnings
