import { KeyRound, ShieldCheck } from "lucide-react";
import { type FormEvent, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";

function safeNext(value: string | null) {
  return value && /^\/(?!\/)/.test(value) ? value : "/";
}

export function LoginPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    setPending(true);
    const form = new FormData(event.currentTarget);
    const body = new URLSearchParams();
    body.set("token", String(form.get("token") ?? ""));
    try {
      const response = await fetch("/login", {
        method: "POST",
        body,
        credentials: "same-origin",
        redirect: "follow",
        headers: { "content-type": "application/x-www-form-urlencoded" },
      });
      if (!response.ok) {
        setError(
          response.status === 403
            ? "Неверный токен. Проверьте значение и повторите вход."
            : "Вход не выполнен. Проверьте настройку токенов на сервере.",
        );
        return;
      }
      queryClient.clear();
      navigate(safeNext(searchParams.get("next")), { replace: true });
    } catch {
      setError("Сервер входа недоступен. Проверьте соединение и повторите попытку.");
    } finally {
      setPending(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-mark" aria-hidden="true">
          <ShieldCheck />
        </div>
        <span className="eyebrow">Защищённый контур</span>
        <h1 id="login-title">Вход в дашборд</h1>
        <p>
          Токен чтения открывает просмотр. Административный токен дополнительно
          разрешает загрузку, удаление и изменение словаря.
        </p>
        <form method="post" action="/login" onSubmit={submit}>
          <label htmlFor="token">Токен доступа</label>
          <div className="token-field">
            <KeyRound size={18} aria-hidden="true" />
            <input id="token" name="token" type="password" autoComplete="off" required />
          </div>
          {error && <div className="login-error" role="alert">{error}</div>}
          <button type="submit" disabled={pending}>
            {pending ? "Проверяем…" : "Войти"}
          </button>
        </form>
        <small>Токен остаётся на сервере; браузер получает защищённую сессионную cookie.</small>
      </section>
    </main>
  );
}
