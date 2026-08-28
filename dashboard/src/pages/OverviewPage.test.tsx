import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { OverviewPage } from "./OverviewPage";

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        api_version: "v1",
        dashboard_mode: "spa",
        server: { status: "ok", version: "1.0.0" },
        permissions: { read: true, admin: false },
        authentication: {
          read_required: false,
          admin_available: false,
          session_level: null,
        },
        summary: {
          configurations: 2,
          metadata_objects: 420,
          code_corpora: 3,
          reference_sources: 2,
        },
      }),
    }),
  );
});

it("показывает живую сводку и три различимых состояния", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <OverviewPage />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("420")).toBeInTheDocument();
  expect(screen.getByText("Готово")).toHaveClass("is-success");
  expect(screen.getByText("Внимание")).toHaveClass("is-warning");
  expect(screen.getByText("Ошибка")).toHaveClass("is-danger");
});
