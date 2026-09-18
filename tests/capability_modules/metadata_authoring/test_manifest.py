from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path

from tools.measure_capability_context import canonical_delta


def test_manifest_совпадает_с_tools_list_и_публичной_документацией():
    path = files("mcp1c.capability_modules.metadata_authoring").joinpath(
        "manifest.json"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    tools, canonical = canonical_delta("metadata_authoring")
    root = Path(__file__).resolve().parents[3]
    readme = (root / "README.md").read_text(encoding="utf-8")
    dashboard = (root / "dashboard/src/pages/CapabilitiesPage.tsx").read_text(
        encoding="utf-8"
    )

    assert payload["schema_version"] == 1
    assert payload["name"] == "metadata_authoring"
    budget = payload["context_budget"]
    assert budget["tool_count"] == 3
    assert budget["approx_tokens"] == 3627
    assert budget["canonical_bytes"] == 14525
    assert budget["canonical_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert len(tools) == 3
    assert len(canonical) == 14525
    for text in (readme, dashboard):
        assert "3 627" in text
        assert "o200k_base" in text
