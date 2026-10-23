"""Синтетические замеры размеров без чтения архивов владельца."""

import importlib.util
import io
from pathlib import Path
import sys
from xml.parsers.expat import ExpatError
import zipfile

import pytest

from module_samples import v8_container_bytes


@pytest.fixture(scope="module")
def measure():
    path = Path(__file__).resolve().parents[1] / "tools/lab/measure_audit_limits.py"
    spec = importlib.util.spec_from_file_location("measure_audit_limits_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_zip(path, members, *, comment=b""):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            archive.writestr(name, payload)
        archive.comment = comment
    return path


def test_eocd_measures_directory_bytes_and_entries_with_archive_comment(measure, tmp_path):
    archive = write_zip(
        tmp_path / "source.zip", [("a.txt", b"a"), ("nested/b.txt", b"bbb")],
        comment=b"synthetic EOCD comment",
    )

    result = measure.read_eocd(archive)

    # Два стандартных заголовка по 46 байт и имена длиной 5 и 12 байт.
    assert result["size_cd"] == 109
    assert result["entries"] == 2


def test_eocd_rejects_missing_end_record(measure, tmp_path):
    archive = tmp_path / "not-a-zip.zip"
    archive.write_bytes(b"synthetic invalid archive")

    with pytest.raises((ValueError, zipfile.BadZipFile)):
        measure.read_eocd(archive)


@pytest.mark.parametrize("chunk_bytes", [1, 7, 65536])
def test_rights_text_node_accumulates_callbacks_and_counts_utf8_bytes_and_chars(
    measure, chunk_bytes
):
    xml = ("<Rights><A>" + "Ж" * 100 + "&amp;" + "Я" * 100
           + "</A><B>" + "x" * 250 + "</B></Rights>").encode("utf-8")

    result = measure.measure_xml(io.BytesIO(xml), kind="rights", chunk_bytes=chunk_bytes)

    assert result["file_bytes"] == len(xml)
    assert result["max_text_node_bytes"] == 401
    assert result["max_text_node_chars"] == 250


def test_rights_text_node_resets_at_start_and_end_of_elements(measure):
    xml = "<Rights>аб<A>в</A>гд</Rights>".encode("utf-8")

    result = measure.measure_xml(io.BytesIO(xml), kind="rights", chunk_bytes=1)

    assert result["max_text_node_bytes"] == 4
    assert result["max_text_node_chars"] == 2


def test_configuration_version_counts_stripped_unicode_chars_with_namespace(measure):
    xml = (
        '<x:Configuration xmlns:x="urn:synthetic"><x:Properties>'
        '<x:Version> \n Ред.1 \t </x:Version>'
        '</x:Properties></x:Configuration>'
    ).encode("utf-8")

    result = measure.measure_xml(io.BytesIO(xml), kind="configuration", chunk_bytes=1)

    assert result["max_version_chars"] == 5


@pytest.mark.parametrize("kind", ["rights", "configuration"])
def test_malformed_xml_is_not_reported_as_zero_maximum(measure, kind):
    with pytest.raises((ValueError, ExpatError)):
        measure.measure_xml(io.BytesIO(b"<Configuration><Version>1.0"), kind=kind)


def test_archive_aggregates_nested_xml_and_unpacked_module_and_form_entries(
    measure, tmp_path
):
    rights_small = b"<Rights><Text>abcdefghij</Text></Rights>"
    rights_large = "<Rights><A>Я&amp;Ю</A><B>ab</B><C /></Rights>".encode("utf-8")
    first_form = v8_container_bytes([("module", b"m" * 200), ("form", b"f" * 20)])
    second_form = v8_container_bytes([("module", b"n" * 30), ("form", b"g" * 300)])
    archive = write_zip(tmp_path / "synthetic.zip", [
        ("Configuration.xml", b"<Configuration><Version> 1.2 </Version></Configuration>"),
        ("Extension/Configuration.xml", "<Configuration><Version>Ред.1</Version></Configuration>"),
        ("Roles/A/Rights.xml", rights_small),
        ("Extension/Roles/B/Rights.xml", rights_large),
        ("Catalogs/A/Forms/B/Ext/Form.bin", first_form),
        ("Extension/Forms/Example.Form", second_form),
        ("unrelated.txt", b"z" * 1000),
    ])

    result = measure.measure_archive(archive)

    assert result["eocd"]["entries"] == 7
    assert result["rights"]["files"] == 2
    assert result["rights"]["max_file_bytes"] == max(len(rights_small), len(rights_large))
    assert result["rights"]["max_text_node_bytes"] == 10
    assert result["rights"]["max_text_node_chars"] == 10
    assert result["forms"]["files"] == 2
    assert result["forms"]["max_module_bytes"] == 200
    assert result["forms"]["max_form_bytes"] == 300
    assert result["configuration"]["files"] == 2
    assert result["configuration"]["max_version_chars"] == 5


def test_archive_reports_zero_files_when_measured_classes_are_absent(measure, tmp_path):
    archive = write_zip(tmp_path / "empty-classes.zip", [("notes.txt", b"synthetic")])

    result = measure.measure_archive(archive)

    assert result["eocd"]["entries"] == 1
    assert result["rights"]["files"] == 0
    assert result["rights"]["max_file_bytes"] == 0
    assert result["rights"]["max_text_node_bytes"] == 0
    assert result["rights"]["max_text_node_chars"] == 0
    assert result["forms"]["files"] == 0
    assert result["forms"]["max_module_bytes"] == 0
    assert result["forms"]["max_form_bytes"] == 0
    assert result["configuration"]["files"] == 0
    assert result["configuration"]["max_version_chars"] == 0


def test_archive_propagates_malformed_rights_xml(measure, tmp_path):
    archive = write_zip(tmp_path / "broken-xml.zip", [("Roles/A/Rights.xml", b"<Rights>")])

    with pytest.raises((ValueError, ExpatError)):
        measure.measure_archive(archive)


def test_archive_propagates_invalid_zip(measure, tmp_path):
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"not a ZIP")

    with pytest.raises((ValueError, zipfile.BadZipFile)):
        measure.measure_archive(archive)


def test_hbk_measures_html_bytes_and_maximum_chapters_on_one_page(measure, tmp_path):
    large_page = ('<html><p class="V8SH_chapter">Раздел</p>'
                  + "Я" * 100 + "</html>").encode("utf-8")
    many_chapters = (b'<html><div class="V8SH_chapter">A</div>'
                     b'<p class="V8SH_chapter">B</p></html>')
    storage = io.BytesIO()
    write_zip(storage, [
        ("objects/large.html", large_page),
        ("objects/chapters.html", many_chapters),
        ("images/not-html.txt", b"x" * 1000),
    ])
    hbk = tmp_path / "synthetic.hbk"
    hbk.write_bytes(v8_container_bytes([("FileStorage", storage.getvalue())]))

    result = measure.measure_hbk(hbk)

    assert result["pages"] == 2
    assert result["max_html_bytes"] == len(large_page)
    assert result["max_chapters"] == 2
