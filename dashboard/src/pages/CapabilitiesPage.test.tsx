import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { requestServerRestart, waitForServerRestart } from "../shared/api/sourceAdmin";
import { CapabilitiesPage } from "./CapabilitiesPage";

vi.mock("../shared/api/sourceAdmin", async (importOriginal) => ({
  ...await importOriginal<typeof import("../shared/api/sourceAdmin")>(),
  requestServerRestart: vi.fn(),
  waitForServerRestart: vi.fn(),
}));

const ready = {
  available: ["diagnostics", "forms"],
  active: [],
  desired: [],
  pending_restart: false,
  runtime: { self_restart: true },
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.mocked(requestServerRestart).mockReset();
  vi.mocked(waitForServerRestart).mockReset();
});

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CapabilitiesPage />
    </QueryClientProvider>,
  );
  return client;
}

it("явно показывает loading до ответа API", () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise(() => {})));
  mount();

  expect(screen.getByText("Читаем настройки дополнительных модулей…")).toBeInTheDocument();
});

it.each([360, 1440])(
  "сохраняет основные controls при viewport %i px",
  async (width) => {
    Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
    window.dispatchEvent(new Event("resize"));
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ready }));
    mount();

    expect(await screen.findByRole("heading", { name: "Дополнительные модули" })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /diagnostics/i })).toBeVisible();
    expect(screen.getByRole("checkbox", { name: /forms/i })).toBeVisible();
    expect(screen.getByRole("button", { name: "Сохранить выбор" })).toBeVisible();
  },
);

it("показывает active отдельно от несохранённого desired", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ready }));
  mount();

  expect(await screen.findByRole("heading", { name: "Дополнительные модули" })).toBeInTheDocument();
  const diagnostics = screen.getByRole("checkbox", { name: /diagnostics/i });
  expect(diagnostics).not.toBeChecked();
  expect(within(diagnostics.closest("label")!).getByText("В текущем процессе: выключен")).toBeInTheDocument();

  fireEvent.click(diagnostics);

  expect(diagnostics).toBeChecked();
  expect(screen.getByText("Выбор ещё не сохранён.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Сохранить выбор" })).toBeEnabled();
});

it("объясняет границу Forms до включения", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ready }));
  mount();

  const forms = await screen.findByRole("checkbox", { name: /forms/i });
  const card = forms.closest("label")!;
  expect(within(card).getByText("Управляемые формы")).toBeInTheDocument();
  expect(within(card).getByText(/4 инструмента/)).toBeInTheDocument();
  expect(within(card).getByText(/конфигурацию 1С не изменяет/)).toBeInTheDocument();
  expect(within(card).getByText(/3 679.*o200k_base/)).toBeInTheDocument();
  expect(forms).not.toBeChecked();
});

it("не теряет несохранённый выбор при фоновом обновлении статуса", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ready }));
  const client = mount();
  const diagnostics = await screen.findByRole("checkbox", { name: /diagnostics/i });
  fireEvent.click(diagnostics);

  client.setQueryData(["capabilities"], {
    ...ready,
    active: ["diagnostics"],
  });

  await waitFor(() => expect(within(diagnostics.closest("label")!).getByText("В текущем процессе: включён")).toBeInTheDocument());
  expect(diagnostics).toBeChecked();
  expect(screen.getByText("Выбор ещё не сохранён.")).toBeInTheDocument();
});

it("сохраняет весь desired-набор и показывает pending", async () => {
  const pending = { ...ready, desired: ["diagnostics"], pending_restart: true };
  vi.stubGlobal("fetch", vi.fn(async (_path, options?: RequestInit) => ({
    ok: true,
    json: async () => options?.method === "PUT" ? pending : ready,
  })));
  mount();
  fireEvent.click(await screen.findByRole("checkbox", { name: /diagnostics/i }));
  fireEvent.click(screen.getByRole("button", { name: "Сохранить выбор" }));

  expect(await screen.findByText("Изменение сохранено и ожидает перезапуска.")).toBeInTheDocument();
  const diagnostics = screen.getByRole("checkbox", { name: /diagnostics/i });
  expect(within(diagnostics.closest("label")!).getByText("В текущем процессе: выключен")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Перезапустить и применить" })).toBeEnabled();
});

it("при выключенном self-restart оставляет операторскую инструкцию вместо кнопки", async () => {
  const pending = {
    ...ready,
    desired: ["diagnostics"],
    pending_restart: true,
    runtime: { self_restart: false },
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => pending }));
  mount();

  expect(await screen.findByText(/изменение должен применить оператор сервера/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Перезапустить и применить" })).not.toBeInTheDocument();
});

it("требует отдельного подтверждения restart и возвращает фокус по Escape", async () => {
  const pending = { ...ready, desired: ["diagnostics"], pending_restart: true };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => pending }));
  mount();
  const trigger = await screen.findByRole("button", { name: "Перезапустить и применить" });

  fireEvent.click(trigger);

  const dialog = screen.getByRole("dialog", { name: "Перезапустить сервер?" });
  expect(dialog).toHaveFocus();
  fireEvent.keyDown(dialog, { key: "Tab" });
  expect(within(dialog).getByRole("button", { name: "Закрыть" })).toHaveFocus();
  fireEvent.keyDown(document.activeElement!, { key: "Tab", shiftKey: true });
  expect(within(dialog).getByRole("button", { name: "Да, перезапустить" })).toHaveFocus();
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();

  fireEvent.click(trigger);
  vi.mocked(requestServerRestart).mockResolvedValue({ state: "restarting", runtime_id: "old", reasons: ["capabilities"] });
  vi.mocked(waitForServerRestart).mockImplementation(() => new Promise(() => {}));
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Да, перезапустить" }));

  await waitFor(() => expect(requestServerRestart).toHaveBeenCalledTimes(1));
});

it("при ошибке settings не предлагает заведомо неуспешный restart", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: false,
    status: 409,
    json: async () => ({ error: "server settings повреждён" }),
  }));
  mount();

  expect(await screen.findByRole("alert")).toHaveTextContent("server settings повреждён");
  expect(screen.queryByRole("button", { name: /restart|перезапуст/i })).not.toBeInTheDocument();
});
