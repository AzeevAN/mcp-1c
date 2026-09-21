from mcp1c.defined_type_contract import (
    DefinedTypeState,
    resolve_defined_type,
)


def test_defined_type_exact_publishes_all_members():
    result = resolve_defined_type(
        "cfg:DefinedTypeRef.Price",
        {"Price": ("Число", "Строка")},
    )
    assert result.state is DefinedTypeState.EXACT
    assert result.members == ("Число", "Строка")


def test_defined_type_missing_and_generic_are_not_exact():
    missing = resolve_defined_type("cfg:DefinedTypeRef.Missing", {})
    generic = resolve_defined_type("cfg:CatalogRef", {})
    assert missing.state is DefinedTypeState.UNKNOWN
    assert generic.state is DefinedTypeState.GENERIC
