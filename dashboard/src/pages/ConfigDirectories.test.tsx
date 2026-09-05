import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ConfigDirectories } from "./ConfigDirectories";
import type { DirectorySources } from "../shared/api/configIntake";

const sources: DirectorySources = {
  roots: ["config-a", "config-b"], configuration_names: ["DemoA", "DemoB"],
  bindings: { DemoA: "config-a" },
};
function show(onCandidate = vi.fn(), configuration = "DemoA", state = sources) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}>
    <ConfigDirectories sources={state} configuration={configuration} busy={false} onCandidate={onCandidate} />
  </QueryClientProvider>);
  return onCandidate;
}
afterEach(() => vi.unstubAllGlobals());

it("обновляет только привязанный корень по кнопке и не запускает публикацию", async () => {
  const candidate = { id: "directory-candidate", internal_name: "DemoA" };
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ candidate }) });
  vi.stubGlobal("fetch", fetcher);
  const accept = show();
  expect(fetcher).not.toHaveBeenCalled();
  expect(screen.queryByText("DemoB")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Обновить из каталога" }));
  await waitFor(() => expect(accept).toHaveBeenCalledWith(candidate));
  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher).toHaveBeenCalledWith("/api/v1/sources/directories/refresh", expect.objectContaining({
    method: "POST", body: JSON.stringify({ configuration: "DemoA" }),
  }));
});

it("выбор отправляет только ID корня, ошибка соответствия видна пользователю", async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 409,
    json: async () => ({ error: "Каталог не соответствует выбранной конфигурации." }) });
  vi.stubGlobal("fetch", fetcher);
  show(vi.fn(), "DemoB");
  expect(screen.queryByRole("button", { name: "Обновить из каталога" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Выбрать каталог" }));
  const group = within(screen.getByRole("dialog", { name: "Каталог основной конфигурации" }));
  fireEvent.change(group.getByRole("combobox"), { target: { value: "config-a" } });
  fireEvent.click(group.getByRole("button", { name: "Подключить" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("не соответствует");
  expect(fetcher).toHaveBeenCalledWith("/api/v1/sources/directories/bind", expect.objectContaining({
    body: JSON.stringify({ configuration: "DemoB", source_id: "config-a" }),
  }));
  expect(group.queryByRole("button", { name: "Обновить из каталога" })).toBeNull();
});

it("не предлагает подключение без доступных корней контейнера", () => {
  show(vi.fn(), "DemoB", { ...sources, roots: [] });
  expect(screen.queryByRole("button", { name: "Выбрать каталог" })).toBeNull();
});
