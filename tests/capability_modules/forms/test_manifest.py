from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path

from tools.measure_capability_context import canonical_delta
from mcp1c.capability_modules.forms.version_catalog import CONFIRMED_FORM_FORMATS


def test_manifest_совпадает_с_фактическим_tools_list_и_документацией():
    path = files("mcp1c.capability_modules.forms").joinpath("manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    tools, canonical = canonical_delta("forms")
    root = Path(__file__).resolve().parents[3]
    readme = (root / "README.md").read_text(encoding="utf-8")
    dashboard = (root / "dashboard/src/pages/CapabilitiesPage.tsx").read_text(
        encoding="utf-8"
    )

    assert payload == {
        "schema_version": 1,
        "name": "forms",
        "title": "Управляемые формы",
        "description": (
            "Правила, генерация, разбор и статическая проверка "
            "управляемых форм."
        ),
        "context_budget": {
            "status": "measured",
            "tool_count": 4,
            "approx_tokens": 6141,
            "tokenizer": "tiktoken 0.11.0 / o200k_base",
            "method": (
                "canonical tools/list delta: UTF-8 JSON, sort_keys, "
                "compact separators"
            ),
            "canonical_bytes": 26224,
            "canonical_sha256": (
                "288dd8c3761bcdd328ddea2f861b0f8f4ab95ebe3eca5bd7aaf9faaaf48a136d"
            ),
            "payload": "tools_list_delta",
            "measured_at": "2026-09-20",
        },
    }
    assert len(tools) == payload["context_budget"]["tool_count"]
    assert len(canonical) == payload["context_budget"]["canonical_bytes"]
    assert hashlib.sha256(canonical).hexdigest() == (
        payload["context_budget"]["canonical_sha256"]
    )
    for text in (readme, dashboard):
        assert "6 141" in text
        assert "o200k_base" in text
    assert "2026-09-20" in readme
    assert "20.09.2026" in dashboard


def test_public_forms_docs_совпадают_с_каталогом_и_замером():
    root = Path(__file__).resolve().parents[3]
    readme = " ".join((root / "README.md").read_text(encoding="utf-8").split())
    dashboard = (root / "dashboard" / "README.md").read_text(encoding="utf-8")

    assert "`diagnostics`" not in dashboard
    assert "2 439" not in dashboard
    assert "6 141" in dashboard
    assert "o200k_base" in dashboard
    assert "2026-09-20" in dashboard
    assert "tools/measure_capability_context.py forms" in dashboard
    assert CONFIRMED_FORM_FORMATS == ("2.16", "2.20")
    assert "форматы `2.19`/`2.20` читает только как inventory" not in readme
    assert "форматы `2.16` и `2.20`" in readme
