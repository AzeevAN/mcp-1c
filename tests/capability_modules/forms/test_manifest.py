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
            "approx_tokens": 5953,
            "tokenizer": "tiktoken 0.11.0 / o200k_base",
            "method": (
                "canonical tools/list delta: UTF-8 JSON, sort_keys, "
                "compact separators"
            ),
            "canonical_bytes": 24849,
            "canonical_sha256": (
                "de7e329d592f54ab3fc8409a3cc64268309c4cf189e2d8ab31b0bd39549b448e"
            ),
            "payload": "tools_list_delta",
            "measured_at": "2026-09-18",
        },
    }
    assert len(tools) == payload["context_budget"]["tool_count"]
    assert len(canonical) == payload["context_budget"]["canonical_bytes"]
    assert hashlib.sha256(canonical).hexdigest() == (
        payload["context_budget"]["canonical_sha256"]
    )
    for text in (readme, dashboard):
        assert "5 953" in text
        assert "o200k_base" in text


def test_public_forms_docs_совпадают_с_каталогом_и_замером():
    root = Path(__file__).resolve().parents[3]
    readme = " ".join((root / "README.md").read_text(encoding="utf-8").split())
    dashboard = (root / "dashboard" / "README.md").read_text(encoding="utf-8")

    assert "`diagnostics`" not in dashboard
    assert "2 439" not in dashboard
    assert "5 953" in dashboard
    assert "o200k_base" in dashboard
    assert "2026-09-18" in dashboard
    assert "tools/measure_capability_context.py forms" in dashboard
    assert CONFIRMED_FORM_FORMATS == ("2.16", "2.20")
    assert "форматы `2.19`/`2.20` читает только как inventory" not in readme
    assert "форматы `2.16` и `2.20`" in readme
