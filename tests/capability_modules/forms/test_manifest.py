from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path

from tools.measure_capability_context import canonical_delta


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
            "approx_tokens": 3503,
            "tokenizer": "tiktoken 0.11.0 / o200k_base",
            "method": (
                "canonical tools/list delta: UTF-8 JSON, sort_keys, "
                "compact separators"
            ),
            "canonical_bytes": 15180,
                "canonical_sha256": (
                    "393d799fa8e035738e6522a6161e73686aaa0a36ce2443b0c69a1ab5699de1c7"
            ),
            "payload": "tools_list_delta",
            "measured_at": "2026-09-13",
        },
    }
    assert len(tools) == payload["context_budget"]["tool_count"]
    assert len(canonical) == payload["context_budget"]["canonical_bytes"]
    assert hashlib.sha256(canonical).hexdigest() == (
        payload["context_budget"]["canonical_sha256"]
    )
    for text in (readme, dashboard):
        assert "3 503" in text
        assert "o200k_base" in text
