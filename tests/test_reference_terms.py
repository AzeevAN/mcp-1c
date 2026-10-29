"""Указатель терминов общей справки: resolved-термин становится точным ключом цели."""

from __future__ import annotations

import sqlite3

from mcp1c.reference_provider import ReferenceService, calculate_logical_hash

from reference_fixture import SyntheticReferenceSigner, build_reference_database

ЦЕЛЬ = "query/ShqueryUsingPredefinedConfigurationData"


def _статья(item_id: str, title_ru: str, title_en: str, body: str) -> tuple:
    return (
        item_id, "synthetic-current", f"{item_id.split('/')[1]}.html", "1" * 64,
        "query", "article", "default", "informational",
        title_ru, title_en, "", body, f"{title_ru} {body}", 1, "2" * 64,
    )


def _сервис(tmp_path, *, статус: str):
    """Пакет: статья-цель без слова «значение» и шумовые статьи, где оно в описании."""
    база = build_reference_database(tmp_path / "source.sqlite3")
    соединение = sqlite3.connect(база)
    соединение.executemany(
        "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _статья(ЦЕЛЬ, "Использование предопределенных данных конфигурации",
                    "Using predefined configuration data",
                    "Предопределенные элементы можно сравнивать в тексте запроса."),
            _статья("query/ShqueryNoiseOne", "Работа с числовыми полями",
                    "Working with numeric fields",
                    "значение значение значение значение поля, значение выражения"),
            _статья("query/ShqueryNoiseTwo", "Приведение типов в запросе",
                    "Type casting in a query",
                    "Каждое значение приводится к типу; значение NULL, значение даты"),
        ),
    )
    соединение.execute(
        "INSERT INTO terms VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "term:value", "synthetic-current", "query", "index.html", 1,
            "ЗНАЧЕНИЕ", "VALUE", "ShqueryUsingPredefinedConfigurationData.html",
            ЦЕЛЬ if статус == "resolved" else None, статус, "",
        ),
    )
    соединение.commit()
    логический = calculate_logical_hash(соединение)
    соединение.execute("UPDATE meta SET value=? WHERE key='content_sha256'", (логический,))
    соединение.commit()
    соединение.close()
    signer = SyntheticReferenceSigner.generate()
    пакет = signer.build(tmp_path / "release" / "reference.mcp1cref", база)
    return ReferenceService.discover(
        tmp_path / "data", embedded_path=пакет, verifier=signer.verifier(),
    )


def _идентификаторы(сервис, запрос: str) -> list[str]:
    assert сервис.provider is not None
    return [
        строка["id"]
        for строка in сервис.provider.search(запрос, domain="query")["results"]
    ]


def test_термин_значение_находит_статью(tmp_path):
    сервис = _сервис(tmp_path, статус="resolved")
    assert сервис.status.state == "ready"
    assert _идентификаторы(сервис, "ЗНАЧЕНИЕ")[0] == ЦЕЛЬ


def test_английский_термин_value(tmp_path):
    сервис = _сервис(tmp_path, статус="resolved")
    assert _идентификаторы(сервис, "VALUE")[0] == ЦЕЛЬ


def test_неразрешённый_термин_не_влияет(tmp_path):
    """Страж: термин не в статусе resolved целью не становится."""
    сервис = _сервис(tmp_path, статус="unresolved")
    assert _идентификаторы(сервис, "ЗНАЧЕНИЕ")[:1] != [ЦЕЛЬ]
    assert _идентификаторы(сервис, "VALUE")[:1] != [ЦЕЛЬ]
