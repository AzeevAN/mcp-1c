import { expect, it, vi } from "vitest";
import { createElement } from "react";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { AppProviders } from "../../app/AppProviders";
import { useCard } from "./cards";
import { useGraph } from "./graph";
import { useRoleObjects } from "./roles";
import { refreshSourceDependents } from "./sourceFreshness";

import { SourceAdminApiError, waitForServerRestart, useRemoveSource } from "./sourceAdmin";

it("удаление источника сбрасывает зависимые данные даже внутри staleTime", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: 10_000, retry: false } } });
  const keys = [["card", "object", "Synthetic"], ["roles", "catalog", "Synthetic"], ["graph", "Synthetic"], ["queries-setup"]];
  for (const key of keys) client.setQueryData(key, "старые данные");
  client.setQueryData(["reference", "item", "independent"], "общая справка");
  vi.stubGlobal("fetch", vi.fn(async () => response({ removed: "Synthetic" })));
  const hook = renderHook(() => useRemoveSource(), {
    wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children),
  });
  try {
    await act(async () => { await hook.result.current.mutateAsync("Synthetic"); });
    for (const key of keys) {
      const fresh = vi.fn(async () => "новые данные");
      expect(await client.fetchQuery({ queryKey: key, queryFn: fresh })).toBe("новые данные");
      expect(fresh).toHaveBeenCalledOnce();
    }
    expect(client.getQueryData(["reference", "item", "independent"])).toBe("общая справка");
  } finally {
    hook.unmount();
    client.clear();
    vi.unstubAllGlobals();
  }
});

it("поздний ответ отменённой карточки не возвращает старый кэш", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const key = ["card", "object", "Synthetic"];
  let finish!: (value: string) => void;
  const pending = client.fetchQuery({ queryKey: key, queryFn: () => new Promise<string>((resolve) => { finish = resolve; }) }).catch(() => undefined);
  await refreshSourceDependents(client);
  finish("старое поколение");
  await pending;
  expect(client.getQueryData(key)).toBeUndefined();
  client.clear();
});

it("placeholder графа и ролей не показывает прежнее поколение при обновлении", async () => {
  let updating = false;
  const finish: Array<(value: Response) => void> = [];
  vi.stubGlobal("fetch", vi.fn(async () => updating
    ? new Promise<Response>((resolve) => finish.push(resolve))
    : response({ marker: "old" })));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const hook = renderHook(() => ({ graph: useGraph("Synthetic", "", 10), roles: useRoleObjects({ config: "Synthetic", role: "Reader" }) }), {
    wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children),
  });
  let refresh: Promise<void> | undefined;
  try {
    await waitFor(() => expect(hook.result.current.graph.data).toBeDefined());
    await waitFor(() => expect(hook.result.current.roles.data).toBeDefined());
    updating = true;
    act(() => { refresh = refreshSourceDependents(client); });
    await waitFor(() => expect(finish).toHaveLength(2));
    await waitFor(() => {
      expect(hook.result.current.graph.data).toBeUndefined();
      expect(hook.result.current.roles.data).toBeUndefined();
    });
  } finally {
    await act(async () => {
      finish.forEach((resolve) => resolve(response({ marker: "new" })));
      await refresh;
    });
    hook.unmount();
    client.clear();
    vi.unstubAllGlobals();
  }
});

it("завершение upload обновляет открытую карточку без страницы Источники", async () => {
  let ready = false;
  vi.stubGlobal("fetch", vi.fn(async (input) => {
    const path = String(input);
    if (path.includes("bootstrap")) return response({ permissions: { admin: true } });
    if (path.endsWith("/sources/admin")) return response({ jobs: [{ name: "synthetic.zip", size: 1, state: ready ? "готово" : "разбирается", error: "" }], incoming: [] });
    if (path.includes("/cards/")) return response({ markdown: ready ? "новая карточка" : "старая карточка" });
    return response({});
  }));
  const hook = renderHook(() => ({ card: useCard("object", "Synthetic", "Справочник.Тест", "full"), client: useQueryClient() }), { wrapper: AppProviders });
  try {
    await waitFor(() => expect(hook.result.current.card.data?.markdown).toBe("старая карточка"));
    await waitFor(() => expect(hook.result.current.client.getQueryData(["sources", "admin"])).toBeDefined());
    ready = true;
    // Реальный глобальный observer сам опрашивает running job раз в 2 секунды.
    await waitFor(() => expect(hook.result.current.card.data?.markdown).toBe("новая карточка"), { timeout: 3500 });
  } finally {
    const client = hook.result.current.client;
    hook.unmount();
    client.clear();
    vi.unstubAllGlobals();
  }
});

function response(payload: unknown): Response {
  return {
    ok: true,
    json: async () => payload,
  } as Response;
}

it("ждёт именно новый runtime_id, а не первый живой health", async () => {
  const request = vi.fn()
    .mockRejectedValueOnce(new TypeError("server unavailable"))
    .mockResolvedValueOnce(response({ status: "ok", runtime_id: "old" }))
    .mockResolvedValueOnce(response({ status: "ok", runtime_id: "new" }));

  const runtimeId = await waitForServerRestart("old", {
    request: request as unknown as typeof fetch,
    intervalMs: 0,
    timeoutMs: 1_000,
  });

  expect(runtimeId).toBe("new");
  expect(request).toHaveBeenCalledTimes(3);
});

it("возвращает понятную ошибку по таймауту рестарта", async () => {
  await expect(waitForServerRestart("old", { timeoutMs: 0 })).rejects.toEqual(
    expect.objectContaining<Partial<SourceAdminApiError>>({
      message: "Сервер не подтвердил новый запуск. Проверьте состояние контейнера.",
      status: 0,
    }),
  );
});
