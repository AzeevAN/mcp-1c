from __future__ import annotations

import json
from importlib.resources import files


def test_manifest_фиксирует_честный_неизмеренный_контекст():
    path = files("mcp1c.capability_modules.forms").joinpath("manifest.json")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload == {
        "schema_version": 1,
        "name": "forms",
        "title": "Управляемые формы",
        "description": (
            "Правила, генерация, разбор и статическая проверка "
            "управляемых форм."
        ),
        "context_budget": {
            "status": "unmeasured",
            "tool_count": None,
            "approx_tokens": None,
            "tokenizer": None,
            "payload": "tools_list_delta",
            "measured_at": None,
        },
    }
