import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { type ReferenceAdminState, uploadReference } from "../shared/api/sourceAdmin";
import { ReferenceSidebar } from "./ReferenceSidebar";

vi.mock("../shared/api/sourceAdmin", async (importOriginal) => ({
  ...await importOriginal<typeof import("../shared/api/sourceAdmin")>(),
  uploadReference: vi.fn(),
}));

let reference: ReferenceAdminState;
let selfRestart: boolean;

beforeEach(() => {
  selfRestart = true;
  reference = {
    api_version: "v1",
    active: { state: "ready", ready: true, message: "Каноническая база подключена.", items: 445, signature: "ed25519", index_cache: "hit" },
    pending: null, catalog: null, managed_upload: true, managed_file_present: true,
    limits: { upload_bytes: 32 * 1024 * 1024 },
  };
  vi.mocked(uploadReference).mockReset();
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => ({
    ok: true,
    json: async () => String(input) === "/api/v1/reference" ? reference : { reference, runtime: { self_restart: selfRestart }, jobs: [], incoming: [] },
  })));
});

function mount(admin = true) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><button>Фоновое действие</button><ReferenceSidebar admin={admin} /></QueryClientProvider>);
  return client;
}

async function open(status = "подключена") {
  const trigger = await screen.findByRole("button", { name: `Общая справка: ${status}` });
  fireEvent.click(trigger);
  return screen.getByRole("dialog", { name: "Общая справка" });
}

it("открывает сведения из строки и возвращает фокус по Escape", async () => {
  mount();
  const dialog = await open();
  expect(within(dialog).getByText("Проверена")).toBeInTheDocument();
  expect(dialog).toHaveFocus();
  fireEvent.keyDown(dialog, { key: "Tab" });
  expect(within(dialog).getByRole("button", { name: "Закрыть" })).toHaveFocus();
  fireEvent.keyDown(document.activeElement!, { key: "Tab", shiftKey: true });
  expect(screen.getByText("Дополнительные действия")).toHaveFocus();
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Общая справка/ })).toHaveFocus();
});

it("без админских прав показывает сведения через публичный API без действий записи", async () => {
  reference.active = { state: "ready", ready: true, message: "Каноническая база подключена." };
  mount(false);
  const dialog = await open();
  expect(within(dialog).getByText(/Только чтение/)).toBeInTheDocument();
  expect(within(dialog).queryByText("Проверена")).not.toBeInTheDocument();
  expect(within(dialog).queryByText("Дополнительные действия")).not.toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: /Загрузить|Удалить|Перезапустить/ })).not.toBeInTheDocument();
  expect(vi.mocked(fetch).mock.calls.map(([path]) => String(path))).toEqual(["/api/v1/reference"]);
});

it("для внешнего артефакта не предлагает загрузку и удаление", async () => {
  reference.managed_upload = false;
  mount();
  const dialog = await open();
  expect(within(dialog).getByText(/оператор сервера/)).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: /Загрузить|Удалить/ })).not.toBeInTheDocument();
});

it("пустая справка сразу открывает выбор файла и отклоняет неверный формат и размер", async () => {
  reference.active = { state: "missing", ready: false, message: "Справка не загружена." };
  reference.managed_file_present = false;
  reference.limits.upload_bytes = 10;
  mount();
  const dialog = await open("не загружена");
  const input = within(dialog).getByLabelText("Файл общей справки");
  fireEvent.change(input, { target: { files: [new File(["test"], "source.zip")] } });
  expect(within(dialog).getByRole("status")).toHaveTextContent("Выберите подписанный файл .mcp1cref");
  fireEvent.change(input, { target: { files: [new File(["too large file"], "reference.mcp1cref")] } });
  expect(within(dialog).getByRole("status")).toHaveTextContent("Файл больше лимита");
  expect(within(dialog).getByRole("button", { name: "Проверить и сохранить" })).toBeDisabled();
  expect(uploadReference).not.toHaveBeenCalled();
});

it("передаёт файл отдельному адаптеру, блокирует закрытие и обновляет pending в строке", async () => {
  let finish!: (value: { reference: ReferenceAdminState; pending: NonNullable<ReferenceAdminState["pending"]> }) => void;
  vi.mocked(uploadReference).mockImplementation((_file, progress) => { progress(45); return new Promise((resolve) => { finish = resolve; }); });
  mount();
  const dialog = await open();
  fireEvent.click(within(dialog).getByRole("button", { name: "Загрузить обновление" }));
  const file = new File(["synthetic"], "reference.mcp1cref");
  fireEvent.change(within(dialog).getByLabelText("Файл общей справки"), { target: { files: [file] } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Проверить и сохранить" }));
  expect(uploadReference).toHaveBeenCalledWith(file, expect.any(Function));
  expect(within(dialog).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "45");
  expect(within(dialog).getByRole("button", { name: "Закрыть" })).toBeDisabled();
  fireEvent.keyDown(dialog, { key: "Escape" });
  expect(dialog).toBeInTheDocument();
  reference = { ...reference, pending: { state: "pending_restart", ready: false, message: "Изменение ожидает перезапуска.", items: 446, action: "activate" } };
  await act(async () => finish({ reference, pending: reference.pending! }));
  expect(await screen.findByRole("button", { name: "Общая справка: ожидает перезапуска" })).toHaveTextContent("446 материалов");
  expect(within(dialog).getByText(/До перезапуска работает предыдущий снимок/)).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "Перезапустить и применить" })).toBeEnabled();
});

it("при отказе проверки сохраняет активную справку и позволяет повторить загрузку", async () => {
  vi.mocked(uploadReference).mockRejectedValue(new Error("Подпись не прошла проверку."));
  mount();
  const dialog = await open();
  fireEvent.click(within(dialog).getByRole("button", { name: "Загрузить обновление" }));
  fireEvent.change(within(dialog).getByLabelText("Файл общей справки"), { target: { files: [new File(["test"], "reference.mcp1cref")] } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Проверить и сохранить" }));
  expect(await within(dialog).findByRole("status")).toHaveTextContent("Подпись не прошла проверку");
  expect(screen.getByRole("button", { name: "Общая справка: подключена" })).toHaveTextContent("445 материалов");
  expect(within(dialog).getByRole("button", { name: "Проверить и сохранить" })).toBeEnabled();
});

it("ожидающее удаление не выглядит как уже отключённая справка и учитывает запрет рестарта", async () => {
  reference.pending = { state: "pending_restart", ready: false, message: "Справка будет отключена после перезапуска.", action: "remove" };
  reference.managed_file_present = false;
  selfRestart = false;
  mount();
  const dialog = await open("ожидает перезапуска");
  expect(within(dialog).getByText(/предыдущий снимок справки/)).toBeInTheDocument();
  expect(within(dialog).getByText(/Перезапуск из дашборда выключен/)).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: "Перезапустить и применить" })).not.toBeInTheDocument();
});

it("при недоступном API предлагает повторить чтение статуса", async () => {
  vi.mocked(fetch).mockRejectedValueOnce(new Error("offline"));
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "Повторить" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Общая справка: подключена" })).toBeEnabled());
});
