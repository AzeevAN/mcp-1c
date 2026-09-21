import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { ReferenceSidebar } from "./ReferenceSidebar";

const reference = {
  api_version: "v1" as const,
  active: { state: "ready", ready: true, message: "Каноническая база подключена.", items: 445, signature: "ed25519", schema_version: "1", index_cache: "hit" },
  catalog: null,
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({ reference, runtime: { self_restart: true }, jobs: [], incoming: [] }),
  })));
});

function mount(admin = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><ReferenceSidebar admin={admin} /></QueryClientProvider>);
}

it("показывает встроенную подписанную справку только для чтения", async () => {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Общая справка: подключена" }));
  const dialog = screen.getByRole("dialog", { name: "Общая справка" });
  expect(within(dialog).getByText("Проверена")).toBeInTheDocument();
  expect(within(dialog).getByText(/изменяется только вместе с релизом/)).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: /Загрузить|Удалить|Перезапустить/ })).not.toBeInTheDocument();
});

it("не предлагает запись справки читателю", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => reference })));
  mount(false);
  fireEvent.click(await screen.findByRole("button", { name: "Общая справка: подключена" }));
  const dialog = screen.getByRole("dialog", { name: "Общая справка" });
  expect(within(dialog).getByText(/Только чтение/)).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: /Загрузить|Удалить/ })).not.toBeInTheDocument();
});
