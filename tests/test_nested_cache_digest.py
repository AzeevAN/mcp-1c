"""Вложенный ридер и его путь участвуют в контракте расходного кэша."""

import pytest
from mcp1c import index_cache, role_access


@pytest.mark.parametrize("change", ["content", "rename", "add", "remove"])
@pytest.mark.parametrize("subject", [index_cache, role_access], ids=["indexes", "roles"])
def test_изменение_вложенного_кода_даёт_cache_miss(tmp_path, monkeypatch, change, subject):
    package = tmp_path / "package"
    nested = package / "readers"
    nested.mkdir(parents=True)
    (package / "index_cache.py").write_text("# неизменный верхний уровень\n")
    reader = nested / "forms.py"
    reader.write_text("# первая реализация\n")
    digest = subject._code_digest
    monkeypatch.setattr(subject, "__file__", str(package / "index_cache.py"))
    # Пустой process-cache соответствует новому запуску после обновления.
    def fresh_digest():
        if subject is index_cache:
            return digest([])
        digest.cache_clear()
        try:
            return digest()
        finally:
            digest.cache_clear()
    monkeypatch.setattr(index_cache, "_code_digest", fresh_digest)
    cache = tmp_path / "forms.cache"
    index_cache.save_blob({"fields": ["old"]}, cache, source_sha256="source", kind="forms")
    assert index_cache.load_blob(cache, source_sha256="source", kind="forms") == {"fields": ["old"]}
    if change == "content":
        reader.write_text("# вторая реализация\n")
    elif change == "rename":
        reader.rename(nested / "other.py")
    elif change == "add":
        (nested / "other.py").write_text("# новый ридер\n")
    else:
        reader.unlink()
    assert index_cache.load_blob(cache, source_sha256="source", kind="forms") is None
