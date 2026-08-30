import { expect, it, vi } from "vitest";

import { SourceAdminApiError, waitForServerRestart } from "./sourceAdmin";

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
