import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";

import { LoginPage } from "./LoginPage";

beforeEach(() => {
  vi.restoreAllMocks();
});

function renderLogin(initialEntry = "/login?next=/sources") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/" element={<h1>Безопасный обзор</h1>} />
          <Route path="/sources" element={<h1>Источники открыты</h1>} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

it("оставляет неверный токен на форме и показывает понятную ошибку", async () => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 403 });
  vi.stubGlobal("fetch", fetchMock);
  renderLogin();

  fireEvent.change(screen.getByLabelText("Токен доступа"), {
    target: { value: "wrong-token" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("Неверный токен");
  expect(screen.getByLabelText("Токен доступа")).toHaveValue("wrong-token");
  expect(fetchMock).toHaveBeenCalledWith(
    "/login",
    expect.objectContaining({ method: "POST", credentials: "same-origin" }),
  );
});

it("после успешного входа возвращает на безопасную прямую ссылку", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200 }));
  renderLogin("/login?next=/sources");

  fireEvent.change(screen.getByLabelText("Токен доступа"), {
    target: { value: "reader-token" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));

  await waitFor(() => {
    expect(screen.getByRole("heading", { name: "Источники открыты" })).toBeInTheDocument();
  });
});

it("не принимает внешний адрес из параметра возврата", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200 }));
  renderLogin("/login?next=//example.test/steal");

  fireEvent.change(screen.getByLabelText("Токен доступа"), {
    target: { value: "reader-token" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Войти" }));

  expect(await screen.findByRole("heading", { name: "Безопасный обзор" })).toBeInTheDocument();
});
