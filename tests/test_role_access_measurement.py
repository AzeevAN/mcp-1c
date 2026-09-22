"""Опубликованная команда замера role_access совпадает с tools/list."""

import json
import subprocess
import sys
from pathlib import Path

from mcp1c.capabilities import CAPABILITY_DEFINITIONS
from tools.measure_capability_context import measurement


ROOT = Path(__file__).parents[1]


def test_role_access_check_использует_реальный_manifest():
    manifest = json.loads(
        (ROOT / "src/mcp1c/capability_modules/role_access.manifest.json").read_text(
            encoding="utf-8"
        )
    )
    budget = manifest["context_budget"]
    assert budget == measurement("role_access")
    assert (budget["tool_count"], budget["canonical_bytes"], budget["approx_tokens"]) == (
        2, 7170, 1245,
    )
    definition = CAPABILITY_DEFINITIONS["role_access"]
    assert (definition.tool_count, definition.approx_tokens) == (2, 1245)

    result = subprocess.run(
        [sys.executable, "tools/measure_capability_context.py", "role_access", "--check"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == budget
