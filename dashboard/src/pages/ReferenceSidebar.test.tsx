import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  expect(within(dialog).getByText("Только чтение. Пакет общей справки проверен подписью.")).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: /Загрузить|Удалить/ })).not.toBeInTheDocument();
});

it("удерживает клавиатурный фокус в диалоге и возвращает его после Escape", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => reference })));
  mount(false);
  const trigger = await screen.findByRole("button", { name: "Общая справка: подключена" });
  trigger.focus();
  fireEvent.click(trigger);

  const dialog = screen.getByRole("dialog", { name: "Общая справка" });
  const close = within(dialog).getByRole("button", { name: "Закрыть" });
  await waitFor(() => expect(dialog).toHaveFocus());

  fireEvent.keyDown(dialog, { key: "Tab" });
  expect(close).toHaveFocus();
  fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
  expect(close).toHaveFocus();

  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(screen.queryByRole("dialog", { name: "Общая справка" })).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it.each([
  ["missing", "не загружена", "Каноническая база не загружена.", "not-checked"],
  ["untrusted", "не доверена", "Проверку подписи не удалось выполнить.", "verification-error"],
  ["corrupt", "повреждена", "Каноническая база не читается.", "ed25519"],
  ["incompatible", "несовместима", "Версия формата не поддерживается.", "ed25519"],
])("не объявляет состояние %s доверенным", async (state, label, message, signature) => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({
      ...reference,
      active: { state, ready: false, message, signature },
    }),
  })));
  mount(false);

  const trigger = await screen.findByRole("button", { name: `Общая справка: ${label}` });
  expect(trigger).not.toHaveClass("is-success");
  fireEvent.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "Общая справка" });
  expect(within(dialog).getByText(message)).toBeInTheDocument();
  expect(within(dialog).queryByText(/Пакет общей справки проверен подписью/)).not.toBeInTheDocument();
});

it("показывает причину отказа status API", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: false,
    status: 503,
    json: async () => ({ error: "Статус справки временно недоступен." }),
  })));
  mount(false);

  expect(await screen.findByRole("button", { name: "Общая справка: Статус недоступен" })).toHaveClass("is-danger");
  expect(screen.getByText("Статус справки временно недоступен.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Повторить" })).toBeInTheDocument();
});

it("не утверждает готовность отсутствующего пакета в admin-диалоге", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => ({
    ok: true,
    json: async () => ({
      reference: {
        ...reference,
        active: { ...reference.active, state: "missing", ready: false, signature: "not-checked" },
      },
      runtime: { self_restart: true }, jobs: [], incoming: [],
    }),
  })));
  mount(true);

  fireEvent.click(await screen.findByRole("button", { name: "Общая справка: не загружена" }));
  const dialog = screen.getByRole("dialog", { name: "Общая справка" });
  expect(within(dialog).getByText("Пакет общей справки не готов к использованию.")).toBeInTheDocument();
  expect(within(dialog).queryByText(/Пакет включён в поставляемый образ/)).not.toBeInTheDocument();
});
