"""Измерить постоянную цену схем одного capability в MCP tools/list."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcp1c.reference_provider import ReferenceService  # noqa: E402
from mcp1c.registry import Registry  # noqa: E402
from mcp1c.server import build_server  # noqa: E402
from mcp1c.capabilities import CAPABILITY_DEFINITIONS  # noqa: E402


TOKENIZER_VERSION = "0.11.0"
ENCODING = "o200k_base"
METHOD = "canonical tools/list delta: UTF-8 JSON, sort_keys, compact separators"
MEASURED_AT = {
    "module_source_download": "2026-09-26",
    "forms": "2026-09-20",
    "metadata_authoring": "2026-09-20",
    "reference": "2026-09-21",
    "role_access": "2026-09-22",
}


def _tools(enabled: tuple[str, ...]) -> list[dict[str, object]]:
    with TemporaryDirectory(prefix="mcp1c-context-") as temporary:
        root = Path(temporary)
        server = build_server(
            Registry(root),
            reference=ReferenceService.discover(
                root,
                embedded_path=ROOT / "release-assets" / "reference" / "reference.mcp1cref",
            ),
            enabled_capabilities=enabled,
        )
        return [
            tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in asyncio.run(server.list_tools())
        ]


def canonical_delta(capability: str) -> tuple[list[dict[str, object]], bytes]:
    base = _tools(())
    enabled = _tools((capability,))
    base_names = {str(tool["name"]) for tool in base}
    delta = [tool for tool in enabled if str(tool["name"]) not in base_names]
    payload = json.dumps(
        delta,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return delta, payload


def measurement(capability: str) -> dict[str, object]:
    import tiktoken

    version = importlib.metadata.version("tiktoken")
    if version != TOKENIZER_VERSION:
        raise SystemExit(
            f"Нужен tiktoken {TOKENIZER_VERSION}, получен {version}."
        )
    delta, payload = canonical_delta(capability)
    tokens = len(tiktoken.get_encoding(ENCODING).encode(payload.decode("utf-8")))
    return {
        "status": "measured",
        "tool_count": len(delta),
        "approx_tokens": tokens,
        "tokenizer": f"tiktoken {TOKENIZER_VERSION} / {ENCODING}",
        "method": METHOD,
        "canonical_bytes": len(payload),
        "canonical_sha256": hashlib.sha256(payload).hexdigest(),
        "payload": "tools_list_delta",
        "measured_at": MEASURED_AT.get(capability, "unmeasured"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("capability")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args()

    if args.capability not in CAPABILITY_DEFINITIONS:
        raise SystemExit(f"Неизвестный capability `{args.capability}`.")
    if args.capability == "module_source_download":
        if args.write:
            raise SystemExit("У встроенного скачивания нет отдельного manifest.")
        measured = measurement(args.capability)
        definition = CAPABILITY_DEFINITIONS[args.capability]
        if args.check and (
            definition.tool_count != measured["tool_count"]
            or definition.approx_tokens != measured["approx_tokens"]
        ):
            raise SystemExit("Capability definition не совпадает с tools/list delta.")
        print(json.dumps(measured, ensure_ascii=False, indent=2))
        return 0
    manifest_path = (
        ROOT / "src" / "mcp1c" / "capability_modules" / args.capability / "manifest.json"
    )
    if args.capability == "role_access":
        manifest_path = ROOT / "src" / "mcp1c" / "capability_modules" / "role_access.manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"Manifest capability `{args.capability}` не найден.")
    measured = measurement(args.capability)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if args.check:
        if manifest.get("context_budget") != measured:
            raise SystemExit("Manifest не совпадает с текущим tools/list delta.")
        definition = CAPABILITY_DEFINITIONS[args.capability]
        if definition.tool_count != measured["tool_count"] or definition.approx_tokens != measured["approx_tokens"]:
            raise SystemExit("Capability definition не совпадает с текущим tools/list delta.")
    elif args.write:
        manifest["context_budget"] = measured
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(measured, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
