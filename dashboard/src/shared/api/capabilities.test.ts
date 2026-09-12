import { beforeEach, expect, it, vi } from "vitest";

import { getCapabilities, saveCapabilities } from "./capabilities";

beforeEach(() => {
  vi.restoreAllMocks();
});

it("читает capability-статус с same-origin credentials", async () => {
  const payload = {
    available: ["diagnostics"],
    active: [],
    desired: ["diagnostics"],
    pending_restart: true,
    runtime: { self_restart: true },
  };
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    json: async () => payload,
  }));

  await expect(getCapabilities()).resolves.toEqual(payload);
  expect(fetch).toHaveBeenCalledWith("/api/v1/capabilities", {
    method: "GET",
    headers: { accept: "application/json" },
    credentials: "same-origin",
  });
});
it("передаёт полный desired-массив через PUT", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      available: ["diagnostics"],
      active: [],
      desired: ["diagnostics"],
      pending_restart: true,
      runtime: { self_restart: true },
    }),
  }));

  await saveCapabilities(["diagnostics"]);

  expect(fetch).toHaveBeenCalledWith("/api/v1/capabilities", {
    method: "PUT",
    headers: { accept: "application/json", "content-type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ enabled: ["diagnostics"] }),
  });
});
