from __future__ import annotations

import pytest

from mcp1c.capability_modules.forms.diagnostics import (
    Artifact,
    Coverage,
    Diagnostic,
    FormsResult,
)


def test_результат_не_подменяет_непроверенные_уровни_общим_valid():
    result = FormsResult(
        status="compiled",
        artifacts=(
            Artifact(
                path="Forms/ФормаПараметров/Ext/Form.xml",
                media_type="application/xml",
                encoding="utf-8-sig",
                content="<Form/>",
            ),
        ),
        diagnostics=(
            Diagnostic(
                level="structural",
                status="passed",
                code="command_reference_resolved",
                path="$.elements[0].children[2].command",
                message="Ссылка разрешена в commands.",
            ),
        ),
        coverage=Coverage(
            xml_parse="passed",
            structural="passed",
            configuration_links="not_checked",
            bsl_static="passed",
            platform_import="not_checked",
            runtime_visual="not_checked",
        ),
        instructions=("Импорт в 1С требует отдельного разрешения.",),
    ).to_dict()

    assert "valid" not in result
    assert result["coverage"]["platform_import"] == "not_checked"
    assert result["coverage"]["runtime_visual"] == "not_checked"
    assert result["artifacts"][0]["encoding"] == "utf-8-sig"


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (lambda: Coverage(platform_import="unknown"), "platform_import"),
        (
            lambda: Diagnostic(
                level="unknown",
                status="failed",
                code="bad",
                path="$",
                message="Ошибка.",
            ),
            "level",
        ),
        (lambda: FormsResult(status="valid"), "status"),
    ],
)
def test_закрытые_значения_контракта_проверяются_в_runtime(factory, match):
    with pytest.raises(ValueError, match=match):
        factory()
