"""Единый отпечаток исходников для расходных индексов пакета."""

import hashlib
from pathlib import Path


def package_digest(root: Path) -> str:
    """Учесть вложенные ридеры и границы файлов, независимо от каталога установки."""
    digest = hashlib.sha256(b"mcp1c-package-code-v1\0")
    for path in sorted(root.rglob("*.py")):
        name = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        # Длины исключают одинаковый отпечаток при перераспределении байтов
        # между файлами; относительное имя учитывает переносы и переименования.
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()
