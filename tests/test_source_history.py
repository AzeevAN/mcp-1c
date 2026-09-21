from mcp1c.source_history import SourceHistoryEntry, SourceHistoryStatus, reconnect_candidates


def _entry(**changes):
    values = {
        "identity": "Ext.A", "parent": "Cfg", "locator": "incoming/a.zip",
        "origin": "a.zip", "transport": "incoming", "raw_sha256": "a" * 64,
    }
    values.update(changes)
    return SourceHistoryEntry(**values)


def test_reconnect_использует_identity_parent_raw_sha_а_не_display_name():
    result = reconnect_candidates(
        [_entry()],
        [("Ext.A", "Cfg", "incoming/renamed.zip", "a" * 64)],
    )
    assert result[0].status is SourceHistoryStatus.ACTIVE
    assert result[0].locator == "incoming/renamed.zip"


def test_reconnect_changed_missing_and_parent_are_diagnostic_only():
    result = reconnect_candidates(
        [_entry(), _entry(identity="Ext.B"), _entry(identity="Ext.C", parent="Other")],
        [
            ("Ext.A", "Cfg", "incoming/new.zip", "b" * 64),
            ("Ext.B", "Other", "incoming/b.zip", "a" * 64),
        ],
    )
    assert result[0].status is SourceHistoryStatus.INCOMPATIBLE
    assert result[1].status is SourceHistoryStatus.MISSING
    assert result[2].status is SourceHistoryStatus.MISSING
