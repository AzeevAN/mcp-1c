import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { useUiStore } from "../../store/uiStore";
import { AppShell } from "./AppShell";

beforeEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  document.documentElement.dataset.theme = "light";
  useUiStore.setState({ sidebarCompact: false, theme: "light" });
});

function LoginTarget() {
  const location = useLocation();
  return <div>Форма входа {location.search}</div>;
}

function renderShell(initialEntry = "/sources?config=Пример") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/login" element={<LoginTarget />} />
          <Route path="/" element={<AppShell />}>
            <Route path="sources" element={<h1>Экран источников</h1>} />
          </Route>
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const bootstrap = (admin: boolean, sessionLevel: "read" | "admin") => ({
  api_version: "v1",
  dashboard_mode: "spa",
  server: { status: "ok", version: "1.1.0" },
  permissions: { read: true, admin },
  authentication: {
    read_required: true,
    admin_available: true,
    session_level: sessionLevel,
  },
  summary: {
    configurations: 1,
    metadata_objects: 2,
    code_corpora: 1,
    reference_sources: 0,
  },
});

it("при 401 не показывает оболочку и сохраняет прямую ссылку для входа", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 401 }));
  renderShell();

  expect(await screen.findByText(/Форма входа/)).toHaveTextContent(
    "next=%2Fsources%3Fconfig%3D",
  );
  expect(screen.queryByText("Данные MCP-сервера")).not.toBeInTheDocument();
});

it("показывает уровень чтения, повышение прав и выход", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, json: async () => bootstrap(false, "read") }),
  );
  renderShell();

  expect(await screen.findByText("Только чтение")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Общая справка" })).toHaveAttribute("href", "/reference");
  expect(screen.getByRole("link", { name: "Роли и права" })).toHaveAttribute("href", "/roles");
  expect(screen.getByRole("link", { name: /Войти как администратор/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Выйти/ })).toBeInTheDocument();
});

it("явно показывает административный уровень", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, json: async () => bootstrap(true, "admin") }),
  );
  renderShell();

  expect(await screen.findByText("Администратор")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /Войти как администратор/ })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Выйти/ })).toBeInTheDocument();
});

it("переключает тему, применяет её к документу и сохраняет выбор", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: true, json: async () => bootstrap(false, "read") }),
  );
  renderShell();

  const toggle = await screen.findByRole("button", { name: "Включить тёмную тему" });
  fireEvent.click(toggle);

  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(screen.getByRole("button", { name: "Включить светлую тему" })).toBeInTheDocument();
  expect(JSON.parse(localStorage.getItem("mcp1c-dashboard-ui") || "{}").state.theme).toBe("dark");
});
