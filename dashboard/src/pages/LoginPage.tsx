import { KeyRound, ShieldCheck } from "lucide-react";

export function LoginPage() {
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
        <form method="post" action="/login">
          <label htmlFor="token">Токен доступа</label>
          <div className="token-field">
            <KeyRound size={18} aria-hidden="true" />
            <input id="token" name="token" type="password" autoComplete="current-password" required />
          </div>
          <button type="submit">Войти</button>
        </form>
        <small>Токен остаётся на сервере; браузер получает защищённую сессионную cookie.</small>
      </section>
    </main>
  );
}
