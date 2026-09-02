"""Контракт воспроизводимого синтетического замера extension recheck."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "tools" / "lab" / "measure_extension_recheck.py"


def test_extension_recheck_lab_меряет_0_1_и_несколько_расширений():
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--base-identities",
            "100",
            "--edges-per-extension",
            "10",
            "--extensions",
            "0,1,3",
            "--repeat",
            "2",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema"] == "extension-recheck-v1"
    assert payload["method"] == "saved generation snapshot; source ZIP не читается"
    assert [item["extensions"] for item in payload["scenarios"]] == [0, 1, 3]
    assert [item["borrowed_edges"] for item in payload["scenarios"]] == [0, 10, 30]
    assert all(item["peak_rss_mib"] > 0 for item in payload["scenarios"])
    assert all(item["latency_ms"]["maximum"] >= 0 for item in payload["scenarios"])
