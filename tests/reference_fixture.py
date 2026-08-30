"""Синтетическая каноническая база без материалов реальной справки."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources (
    source_key TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
    book_id TEXT NOT NULL, language TEXT NOT NULL, platform_version TEXT,
    source_sha256 TEXT NOT NULL, source_size INTEGER NOT NULL,
    parser_version TEXT NOT NULL
);
CREATE TABLE items (
    id TEXT PRIMARY KEY, source_key TEXT NOT NULL REFERENCES sources(source_key),
    source_path TEXT NOT NULL, source_content_sha256 TEXT NOT NULL,
    domain TEXT NOT NULL, kind TEXT NOT NULL,
    access_scope TEXT NOT NULL CHECK(access_scope IN ('default', 'explicit', 'hidden')),
    safety TEXT NOT NULL CHECK(safety IN ('informational', 'operator', 'administrative')),
    title_ru TEXT NOT NULL, title_en TEXT NOT NULL, signature TEXT NOT NULL,
    body TEXT NOT NULL, search_text TEXT NOT NULL,
    accepted INTEGER NOT NULL CHECK(accepted IN (0, 1)),
    content_sha256 TEXT NOT NULL
);
CREATE TABLE sections (
    id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(id),
    parent_id TEXT REFERENCES sections(id), ordinal INTEGER NOT NULL,
    heading_level INTEGER NOT NULL, anchor TEXT NOT NULL,
    title_ru TEXT NOT NULL, title_en TEXT NOT NULL, signature TEXT NOT NULL,
    parameters_json TEXT NOT NULL, examples_json TEXT NOT NULL,
    body TEXT NOT NULL, content_sha256 TEXT NOT NULL,
    UNIQUE(item_id, ordinal)
);
CREATE TABLE aliases (
    id INTEGER PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(id),
    value TEXT NOT NULL, normalized TEXT NOT NULL, language TEXT NOT NULL,
    alias_kind TEXT NOT NULL, UNIQUE(item_id, normalized, language)
);
CREATE TABLE parameters (
    item_id TEXT NOT NULL REFERENCES items(id), ordinal INTEGER NOT NULL,
    name TEXT NOT NULL, required INTEGER NOT NULL, types_json TEXT NOT NULL,
    description TEXT NOT NULL, default_value TEXT NOT NULL,
    PRIMARY KEY(item_id, ordinal)
);
CREATE TABLE examples (
    item_id TEXT NOT NULL REFERENCES items(id), ordinal INTEGER NOT NULL,
    label TEXT NOT NULL, content TEXT NOT NULL, locator_json TEXT NOT NULL,
    PRIMARY KEY(item_id, ordinal)
);
CREATE TABLE item_tables (
    item_id TEXT NOT NULL REFERENCES items(id), ordinal INTEGER NOT NULL,
    header_json TEXT NOT NULL, rows_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL, PRIMARY KEY(item_id, ordinal)
);
CREATE TABLE templates (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    article_item_id TEXT REFERENCES items(id), internal_name TEXT NOT NULL,
    content_ru TEXT NOT NULL, content_en TEXT NOT NULL,
    placeholders_json TEXT NOT NULL, parsed_structure_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL
);
CREATE TABLE terms (
    id TEXT PRIMARY KEY, source_key TEXT NOT NULL REFERENCES sources(source_key),
    domain TEXT NOT NULL, source_path TEXT NOT NULL, ordinal INTEGER NOT NULL,
    name_ru TEXT NOT NULL, name_en TEXT NOT NULL, target_href TEXT NOT NULL,
    target_item_id TEXT REFERENCES items(id), status TEXT NOT NULL,
    correction_reason TEXT NOT NULL, UNIQUE(source_key, source_path, ordinal)
);
CREATE TABLE relations (
    id TEXT PRIMARY KEY, source_item_id TEXT REFERENCES items(id),
    source_path TEXT NOT NULL, ordinal INTEGER NOT NULL,
    relation_kind TEXT NOT NULL, label TEXT NOT NULL,
    original_href TEXT NOT NULL, resolved_href TEXT NOT NULL,
    target_item_id TEXT REFERENCES items(id), target_section_id TEXT REFERENCES sections(id),
    status TEXT NOT NULL, correction_reason TEXT NOT NULL
);
CREATE TABLE version_facts (
    id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(id),
    fact_kind TEXT NOT NULL, version TEXT NOT NULL, evidence_kind TEXT NOT NULL,
    evidence_ref TEXT NOT NULL, evidence_note TEXT NOT NULL,
    UNIQUE(item_id, fact_kind, version, evidence_kind, evidence_ref)
);
CREATE TABLE observations (
    item_id TEXT NOT NULL REFERENCES items(id),
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    presence TEXT NOT NULL, platform_version TEXT, evidence_ref TEXT NOT NULL,
    PRIMARY KEY(item_id, source_key)
);
CREATE TABLE search_hints (
    item_id TEXT NOT NULL REFERENCES items(id), ordinal INTEGER NOT NULL,
    text TEXT NOT NULL, source_kind TEXT NOT NULL,
    PRIMARY KEY(item_id, ordinal), UNIQUE(item_id, text)
);
CREATE TABLE assets (
    id TEXT PRIMARY KEY, source_key TEXT NOT NULL REFERENCES sources(source_key),
    source_path TEXT NOT NULL, media_type TEXT NOT NULL,
    width INTEGER, height INTEGER, content_sha256 TEXT NOT NULL
);
CREATE TABLE asset_relations (
    article_item_id TEXT NOT NULL REFERENCES items(id),
    asset_id TEXT REFERENCES assets(id), ordinal INTEGER NOT NULL,
    src TEXT NOT NULL, alt TEXT NOT NULL,
    PRIMARY KEY(article_item_id, ordinal)
);
CREATE TABLE build_issues (
    id INTEGER PRIMARY KEY, severity TEXT NOT NULL, code TEXT NOT NULL,
    entity_id TEXT NOT NULL, message TEXT NOT NULL
);
"""


def build_reference_database(path: Path, *, body: str = "Синтетическое описание.") -> Path:
    """Создать минимальную schema v1 с двумя вымышленными карточками."""
    from mcp1c.reference_provider import calculate_logical_hash

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        (("schema_version", "1"), ("curation_schema_version", "1"), ("raw_schema_version", "1")),
    )
    connection.execute(
        "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "synthetic-current", "Синтетическая книга", "synthetic", "Synthetic",
            "ru", "8.3.20", "a" * 64, 100, "test-1",
        ),
    )
    items = (
        (
            "bsl/Example", "synthetic-current", "example.html", "b" * 64,
            "bsl_language", "article", "default", "informational",
            "Пример языка", "Language example", "Пример()", body,
            f"Пример языка {body}", 1, "c" * 64,
        ),
        (
            "dcs/Sum", "synthetic-current", "sum.html", "d" * 64,
            "dcs", "function", "explicit", "informational",
            "Сумма", "Sum", "Сумма(Значение)", "Суммирует значения.",
            "Сумма агрегат значение", 1, "e" * 64,
        ),
    )
    connection.executemany(
        "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        items,
    )
    connection.execute(
        "INSERT INTO sections VALUES (?, ?, NULL, 1, 2, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "bsl/Example#usage", "bsl/Example", "usage", "Использование", "Usage",
            "", "[]", "[]", "Подробное синтетическое описание применения.", "f" * 64,
        ),
    )
    connection.execute(
        "INSERT INTO aliases(item_id, value, normalized, language, alias_kind) "
        "VALUES (?, ?, ?, ?, ?)",
        ("bsl/Example", "Образец", "образец", "ru", "synonym"),
    )
    connection.execute(
        "INSERT INTO parameters VALUES (?, 1, ?, 1, ?, ?, ?)",
        ("dcs/Sum", "Значение", "[]", "Суммируемое значение.", ""),
    )
    connection.execute(
        "INSERT INTO examples VALUES (?, 1, ?, ?, ?)",
        ("bsl/Example", "Пример", "Результат = Пример();", "{}"),
    )
    connection.execute(
        "INSERT INTO version_facts VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "version:bsl-example", "bsl/Example", "introduced", "8.3.10",
            "curated", "synthetic", "Синтетический факт.",
        ),
    )
    connection.execute(
        "INSERT INTO search_hints VALUES (?, 1, ?, ?)",
        ("bsl/Example", "показать образец", "measured"),
    )
    connection.commit()
    digest = calculate_logical_hash(connection)
    connection.execute(
        "INSERT INTO meta(key, value) VALUES ('content_sha256', ?)", (digest,)
    )
    connection.commit()
    connection.close()
    return path


def reference_manifest(
    database: bytes,
    *,
    key_id: str,
    logical_sha256: str,
) -> dict[str, object]:
    """Собрать синтетический manifest без заимствованного содержимого."""
    return {
        "artifact": "reference.sqlite3",
        "artifact_sha256": hashlib.sha256(database).hexdigest(),
        "artifact_size": len(database),
        "format": "mcp1c-reference",
        "format_version": "1",
        "key_id": key_id,
        "logical_sha256": logical_sha256,
        "schema_version": "1",
        "signature_algorithm": "ed25519",
    }


def manifest_bytes(manifest: dict[str, object]) -> bytes:
    return (
        json.dumps(
            manifest,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def build_reference_artifact(
    path: Path,
    database: Path,
    private_key: Ed25519PrivateKey,
    *,
    key_id: str = "synthetic-ephemeral",
    manifest_override: dict[str, object] | None = None,
    signed_manifest: bytes | None = None,
    signature: bytes | None = None,
    database_bytes: bytes | None = None,
    include_manifest: bool = True,
    include_signature: bool = True,
) -> Path:
    """Упаковать три синтетических файла; ключ создаёт сам тест во временном каталоге."""
    raw_database = database.read_bytes()
    connection = sqlite3.connect(database)
    logical_row = connection.execute(
        "SELECT value FROM meta WHERE key='content_sha256'"
    ).fetchone()
    logical_sha256 = logical_row[0] if logical_row is not None else "0" * 64
    connection.close()
    manifest = reference_manifest(
        raw_database,
        key_id=key_id,
        logical_sha256=logical_sha256,
    )
    if manifest_override:
        manifest.update(manifest_override)
    raw_manifest = manifest_bytes(manifest)
    raw_signature = signature or private_key.sign(signed_manifest or raw_manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as bundle:
        if include_manifest:
            bundle.writestr("manifest.json", raw_manifest)
        if include_signature:
            bundle.writestr("manifest.sig", raw_signature)
        bundle.writestr("reference.sqlite3", database_bytes or raw_database)
    return path


@dataclass(frozen=True)
class SyntheticReferenceSigner:
    """Эфемерный ключ одного теста; приватные байты не пишутся на диск."""

    private_key: Ed25519PrivateKey
    key_id: str = "synthetic-ephemeral"

    @classmethod
    def generate(cls) -> "SyntheticReferenceSigner":
        return cls(Ed25519PrivateKey.generate())

    def verifier(self):
        from mcp1c.reference_provider import SignedArtifactVerifier

        public = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        return SignedArtifactVerifier({self.key_id: public})

    def build(self, path: Path, database: Path, **kwargs) -> Path:
        return build_reference_artifact(
            path,
            database,
            self.private_key,
            key_id=self.key_id,
            **kwargs,
        )
