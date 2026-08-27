import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { StatusBadge } from "./StatusBadge";

it.each(["success", "warning", "danger", "info"] as const)(
  "состояние %s получает самостоятельный визуальный тон",
  (tone) => {
    render(<StatusBadge tone={tone}>Состояние</StatusBadge>);

    expect(screen.getByText("Состояние")).toHaveClass(`is-${tone}`);
  },
);
