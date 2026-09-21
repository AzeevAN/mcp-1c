import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { OverviewPage } from "./OverviewPage";

const bootstrap = {
  api_version: "v1",
  dashboard_mode: "spa",
  server: { status: "ok", version: "1.1.0" },
  permissions: { read: true, admin: false },
  authentication: {
    read_required: false,
    admin_available: false,
    session_level: null,
  },
  summary: {
    configurations: 1,
    metadata_objects: 420,
    code_corpora: 0,
    reference_sources: 2,
  },
};

const sources = {
  api_version: "v1",
  permissions: { read: true, admin: false },
  configurations: [{
    id: "Демонстрационная конфигурация",
    version: "1.0",
    platform: "8.3.27",
    objects: 420,
    edges: 780,
    loaded_at: "2026-09-21T12:00:00Z",
    notes: [],
    native_generation: true,
    activation_mode: "B_FULL",
    activation_status: "ACTIVE",
    platform_declaration: null,
    source: {
      id: "configuration:demo",
      kind: "configuration",
      platform: "8.3.27",
      items_total: 420,
      status: "ready",
      loaded_at: "2026-09-21T12:00:00Z",
      code_version: "",
      incomplete: false,
      warnings: [],
    },
    corpora: [{
      id: "modules:demo",
      label: "Основная конфигурация",
      kind: "modules",
      native_generation: true,
      phase: "ready",
      state: "ready",
      source: null,
      coverage: null,
      journal: "",
      journal_url: "",
      error: "",
    }],
  }],
  references: [],
};

const capabilities = {
  available: ["reference", "role_access"],
  active: ["reference"],
  desired: ["reference"],
  pending_restart: false,
  runtime: { self_restart: true },
  modules: [{
    id: "reference",
    display_name: "Общая справка",
    description: "Поиск по общей справке.",
    tool_count: 2,
    approx_tokens: 1070,
    tokenizer: "o200k_base",
    measured_at: "2026-09-21",
    measurement_command: "measure",
    active: true,
    desired: true,
    pending_restart: false,
  }],
};

function response(payload: unknown) {
  return Promise.resolve({ ok: true, json: async () => payload });
}

function renderOverview() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <OverviewPage />
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api/v1/dashboard/bootstrap")) return response(bootstrap);
    if (url.endsWith("/api/v1/sources")) return response(sources);
    if (url.endsWith("/api/v1/capabilities")) return response(capabilities);
    throw new Error(`Неожиданный URL: ${url}`);
  }));
});

it("показывает оперативную сводку вместо демонстрационных блоков", async () => {
  renderOverview();

  expect(await screen.findAllByText("420")).toHaveLength(2);
  expect(screen.getByText("Корпуса кода").parentElement).toHaveTextContent("1");
  expect(await screen.findByText("Система готова")).toHaveClass("is-success");
  expect(screen.getByText("Общая справка")).toBeInTheDocument();
  expect(screen.getByText("≈ 1 070")).toBeInTheDocument();
  expect(screen.getByText("Демонстрационная конфигурация")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /Проверить запрос/ })).toHaveAttribute("href", "/queries");
  expect(screen.queryByText("Один владелец данных")).not.toBeInTheDocument();
  expect(screen.queryByText("Состояния должны различаться сразу")).not.toBeInTheDocument();
});

it("выносит ожидающий перезапуск в готовность и список внимания", async () => {
  vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api/v1/dashboard/bootstrap")) return response(bootstrap) as Promise<Response>;
    if (url.endsWith("/api/v1/sources")) return response(sources) as Promise<Response>;
    if (url.endsWith("/api/v1/capabilities")) {
      return response({ ...capabilities, pending_restart: true }) as Promise<Response>;
    }
    throw new Error(`Неожиданный URL: ${url}`);
  });
  renderOverview();

  expect(await screen.findByText("Требуется перезапуск")).toHaveClass("is-warning");
  expect(screen.getByText("Настройки модулей ждут перезапуска")).toBeInTheDocument();
});

it("не подменяет отсутствующий замер контекста нулём", async () => {
  vi.mocked(fetch).mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/api/v1/dashboard/bootstrap")) return response(bootstrap) as Promise<Response>;
    if (url.endsWith("/api/v1/sources")) return response(sources) as Promise<Response>;
    if (url.endsWith("/api/v1/capabilities")) {
      return response({
        ...capabilities,
        modules: [{ ...capabilities.modules[0], tool_count: null, approx_tokens: null }],
      }) as Promise<Response>;
    }
    throw new Error(`Неожиданный URL: ${url}`);
  });
  renderOverview();

  expect(await screen.findAllByText("не измерено")).toHaveLength(2);
  expect(screen.getByText("не все модули измерены")).toBeInTheDocument();
  expect(screen.queryByText("≈ 0")).not.toBeInTheDocument();
});
